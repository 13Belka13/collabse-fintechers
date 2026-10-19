# Tatum webhook + RPC polling fallback (SPEC §11, L7) on the in-memory chain: HMAC over the raw body (and the
# compact-JSON fallback), fail-closed without a secret, dedupe by txId, the payload is only a pointer — the amount
# and the recipient come from the chain receipt, a claimed transfer that is not on chain is rejected — and the
# poller and the webhook count one tx once. The same faults run end to end in scripts/scenarios.py (F04, F05).
import json
import secrets

import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import chain, config, db, journal, tczk, webhooks
from tests.unit.test_tczk import ALFA, BETA, token  # noqa: F401  (fixture)

pytestmark = pytest.mark.chain
SECRET = "unit-test-hmac-secret"


@pytest.fixture
def hook(seeded, token, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TATUM_HMAC_SECRET", SECRET)
    config.reload()
    monkeypatch.setattr(webhooks, "RECEIPT_TRIES", 1)
    app = FastAPI()
    app.include_router(webhooks.router)
    return TestClient(app)  # no `with`: the lifespan (poller thread) does not run in unit tests


def mint_to_beta(amount=250) -> str:
    return tczk.mint(BETA, amount, f"wh-{secrets.token_hex(4)}")["tx_hash"]


def payload(tx, to=BETA, contract=None, value="9999.00"):
    """Enriched template; value deliberately wrong: the bank must take the amount from the chain."""
    return {"subscriptionType": "INCOMING_FUNGIBLE_TX", "chain": "base-sepolia",
            "data": {"txId": tx, "from": "0x0000000000000000000000000000000000000000", "to": to, "value": value,
                     "contractAddress": contract or config.TCZK, "tokenMetadata": {"decimals": 2, "symbol": "tCZK"}}}


def compact(body) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def post(client, body=None, raw=None, secret=SECRET, signed=True, sign_over=None):
    raw = compact(body) if raw is None else raw
    headers = {"content-type": "application/json"}
    if signed:
        headers["x-payload-hash"] = webhooks.sign(secret, sign_over if sign_over is not None else raw)
    return client.post("/webhooks/tatum", content=raw, headers=headers)


def incoming(tx=None):
    return [e for e in journal.entries(limit=500) if e["event"] == "incoming_transfer"
            and (tx is None or e["payload"]["tx_hash"].lower() == tx.lower())]


def rows(tx):
    return db.rows("SELECT * FROM notifications WHERE tx_id=?", (tx.lower(),))


def test_signed_webhook_records_the_amount_from_the_chain_not_the_payload(hook):
    tx = mint_to_beta(250)
    r = post(hook, payload(tx))
    assert r.status_code == 200 and r.json() == {"ok": True, "accepted": True, "duplicate": False, "tx_id": tx}
    [row] = rows(tx)
    assert row["verified"] == 1
    [e] = incoming(tx)
    p = e["payload"]
    assert (p["source"], p["asset"], p["amount"], p["to"], p["company_id"]) == ("tatum", "tCZK", 250, BETA, "beta")
    assert p["link"].endswith(tx)
    assert db.one("SELECT COUNT(*) n FROM timings WHERE stage='block_to_feed:tatum'")["n"] == 1
    assert journal.verify() == (True, None)


def test_bad_signature_is_401_with_no_row_and_a_rejected_entry(hook):
    tx = mint_to_beta()
    r = post(hook, payload(tx), secret="not-the-secret")
    assert r.status_code == 401
    assert rows(tx) == [] and incoming() == []
    [e] = [e for e in journal.entries() if e["event"] == "webhook_rejected"]
    assert e["payload"]["reason"] == "bad_signature" and tx not in json.dumps(e["payload"])  # no payload trust


def test_missing_signature_is_401(hook):
    tx = mint_to_beta()
    r = post(hook, payload(tx), signed=False)
    assert r.status_code == 401 and rows(tx) == []
    assert journal.entries()[0]["payload"]["reason"] == "missing_signature"


def test_no_secret_configured_fails_closed(hook, monkeypatch):
    monkeypatch.setenv("TATUM_HMAC_SECRET", "")
    config.reload()
    tx = mint_to_beta()
    r = post(hook, payload(tx), secret="")
    assert r.status_code == 401 and rows(tx) == []
    assert journal.entries()[0]["payload"]["reason"] == "no_secret_configured"


def test_raw_body_and_reformatted_json(hook):
    tx = mint_to_beta()
    pretty = json.dumps(payload(tx), indent=2).encode()
    # signed over the exact raw bytes (whatever their formatting)
    assert post(hook, raw=pretty).json()["accepted"] is True
    tx2 = mint_to_beta()
    body2 = payload(tx2)
    # a proxy re-formatted the body: the signature over Tatum's compact JSON still matches (SPEC §11 fallback)
    assert post(hook, raw=json.dumps(body2, indent=2).encode(), sign_over=compact(body2)).json()["accepted"] is True
    # the body was changed after signing: rejected
    tampered = compact(payload(mint_to_beta()))
    assert post(hook, raw=tampered, sign_over=compact(body2)).status_code == 401
    assert webhooks.signature_ok(b"not json", webhooks.sign(SECRET, b"not json"), SECRET)
    assert not webhooks.signature_ok(b"not json", webhooks.sign(SECRET, b"not json!"), SECRET)


def test_duplicate_delivery_is_counted_once(hook):
    tx = mint_to_beta()
    first, second = post(hook, payload(tx)), post(hook, payload(tx))
    assert first.json()["duplicate"] is False
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert len(rows(tx)) == 1 and len(incoming(tx)) == 1
    assert webhooks.record(tx, [{"asset": "tCZK", "token": config.TCZK, "from": ALFA, "to": BETA, "company_id": "beta",
                                 "amount": 1}], 1, "rpc_poll") is False  # the poller racing in: still once


def test_claimed_transfer_that_is_not_on_chain_is_rejected(hook):
    fake = "0x" + secrets.token_hex(32)
    r = post(hook, payload(fake))
    assert r.status_code == 200 and r.json()["accepted"] is False and r.json()["reason"] == "not_on_chain"
    assert incoming() == []
    [row] = rows(fake)
    assert row["verified"] == 0  # kept for the audit, never counted
    assert journal.entries()[0]["event"] == "webhook_unverified"
    assert post(hook, payload(fake)).json()["accepted"] is False  # a replay is not "duplicate accepted"


def _tester_keys() -> list[str]:
    return [k.to_hex() for k in chain.w3().provider.ethereum_tester.backend.account_keys]


def test_tx_without_a_transfer_to_a_client_or_naming_another_recipient_is_rejected(hook):
    keys = _tester_keys()
    a, b = Account.from_key(keys[1]).address, Account.from_key(keys[2]).address
    eth_only = chain.w3().eth.send_transaction({"from": a, "to": b, "value": 1}).to_0x_hex()
    assert post(hook, payload(eth_only)).json()["reason"] == "no_matching_transfer"
    tx = mint_to_beta()
    assert post(hook, payload(tx, to=ALFA)).json()["reason"] == "no_matching_transfer"  # the mint went to Beta
    assert post(hook, payload(tx, contract=config.EURC)).json()["reason"] == "no_matching_transfer"
    assert post(hook, {"data": {"txId": "nope"}}).json() == {"ok": True, "accepted": False, "reason": "no_tx_id"}
    assert incoming() == []


def test_poller_records_once_and_the_webhook_then_sees_a_duplicate(hook):
    tx = mint_to_beta(300)
    nxt = webhooks.poll_once(0)
    assert nxt == chain.w3().eth.block_number + 1
    [e] = incoming(tx)
    assert (e["payload"]["source"], e["payload"]["amount"]) == ("rpc_poll", 300)
    assert db.one("SELECT COUNT(*) n FROM timings WHERE stage='block_to_feed'")["n"] == 1
    assert post(hook, payload(tx)).json()["duplicate"] is True
    webhooks.poll_once(0)  # re-reading the same range adds nothing
    assert len(incoming(tx)) == 1 and len(rows(tx)) == 1
    assert webhooks.poll_once(nxt) == nxt  # nothing new
    assert all(e["payload"]["to"] != ALFA for e in incoming())  # the poller watches seller wallets only


def test_catch_up_on_start_records_but_does_not_count_as_latency(hook):
    tx = mint_to_beta()
    webhooks.poll_once(None)  # first tick: re-reads the last LOOKBACK blocks
    assert len(incoming(tx)) == 1
    assert db.one("SELECT COUNT(*) n FROM timings WHERE stage LIKE 'block_to_feed%'")["n"] == 0


def test_webhook_first_then_poller_adds_nothing(hook):
    tx = mint_to_beta()
    assert post(hook, payload(tx)).json()["duplicate"] is False
    webhooks.poll_once(0)
    assert len(incoming(tx)) == 1


def test_unverified_claim_is_upgraded_once_the_chain_confirms(hook):
    tx = mint_to_beta()
    assert post(hook, payload(tx, to=ALFA)).json()["accepted"] is False  # wrong recipient claimed
    assert rows(tx)[0]["verified"] == 0
    webhooks.poll_once(0)  # the chain shows the real transfer to Beta
    assert rows(tx)[0]["verified"] == 1 and len(incoming(tx)) == 1


def test_poller_runs_only_without_a_public_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.trycloudflare.com")
    config.reload()
    assert webhooks.start() is False
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    monkeypatch.setenv("BANK_POLLER", "0")
    config.reload()
    assert webhooks.start() is False


def test_parse_reads_the_enriched_and_the_flat_template():
    tx = "0x" + "ab" * 32
    assert webhooks.parse(payload(tx))["tx_id"] == tx
    flat = webhooks.parse({"txId": tx, "address": BETA, "counterAddress": ALFA, "asset": "0x" + "11" * 20,
                           "amount": "2.5"})
    assert (flat["to"], flat["from"], flat["value"]) == (BETA, ALFA, "2.5")
    assert webhooks.parse([1, 2]) == {} and webhooks.parse({"data": {"txId": 5}})["tx_id"] is None
