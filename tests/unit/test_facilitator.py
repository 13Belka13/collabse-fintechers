# Bank facilitator, no chain: the screening hook (money-refusing) and the refusal shape over HTTP.
# A refusal must be HTTP 200 {"isValid": false, "invalidReason": code} and must send no transaction.
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from x402.schemas import PaymentPayload, PaymentRequirements

from bank import config, db, facilitator, journal, tczk

ALFA = "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140"
BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"
STRANGER = "0x000000000000000000000000000000000000dEaD"
TCZK_ADDR = "0x6a99C9e95DD220172d07D5f730f2e82B466D4B67"
# public anvil dev key #0 — never holds real funds; the facilitator never reaches the chain in these tests
DUMMY_RELAYER = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


@pytest.fixture(autouse=True)
def bank(seeded, monkeypatch):
    monkeypatch.setenv("BANK_RELAYER_PK", DUMMY_RELAYER)
    monkeypatch.setenv("TCZK", TCZK_ADDR)
    monkeypatch.setenv("TCZK_NAME", "tCZK Deposit Token")
    config.reload()
    facilitator.reset()
    yield
    facilitator.reset()


def req(asset="EURC", amount="5000000", pay_to=BETA, extra=None, network="eip155:84532", address=None):
    meta = config.ASSETS[asset]
    return PaymentRequirements(scheme="exact", network=network, asset=address or meta["address"], amount=amount,
                               pay_to=pay_to, max_timeout_seconds=60,
                               extra=extra if extra is not None else {"name": meta["name"], "version": meta["version"]})


def pay(r, frm=ALFA, to=None, value=None):
    auth = {"from": frm, "to": to or r.pay_to, "value": value or r.amount, "validAfter": "0",
            "validBefore": str(int(time.time()) + 60), "nonce": "0x" + "11" * 32}
    return PaymentPayload(x402_version=2, payload={"signature": "0x" + "22" * 65, "authorization": auth}, accepted=r)


def body(p, r):
    return {"x402Version": 2, "paymentPayload": p.model_dump(by_alias=True, exclude_none=True),
            "paymentRequirements": r.model_dump(by_alias=True, exclude_none=True)}


# ---- screen(): every refusal code it can return ----

def test_clean_eurc_payment_passes():
    r = req()
    assert facilitator.screen(pay(r), r) is None


@pytest.mark.parametrize("kw, p_kw, code", [
    ({"pay_to": STRANGER}, {}, "COUNTERPARTY_NOT_IN_SCOPE"),                       # payee not a bank client
    ({}, {"to": STRANGER}, "COUNTERPARTY_NOT_IN_SCOPE"),                           # signed to someone else
    ({"address": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"}, {}, "QUOTE_MISMATCH"),  # USDC: not a bank asset
    ({"extra": {"name": "EURC", "version": "1"}}, {}, "QUOTE_MISMATCH"),           # wrong EIP-712 domain
    ({"extra": {}}, {}, "QUOTE_MISMATCH"),
    ({"amount": "0"}, {}, "QUOTE_MISMATCH"),
    ({"amount": "5.00"}, {}, "QUOTE_MISMATCH"),                                    # decimals, not atomic units
    ({}, {"value": "4999999"}, "QUOTE_MISMATCH"),
    ({"network": "eip155:8453"}, {}, "QUOTE_MISMATCH"),                            # mainnet: never
    ({"amount": "10000001"}, {}, "AMOUNT_ABOVE_LIMIT"),                            # 10.000001 € > 10 € cap
    ({"asset": "tCZK", "amount": "25001"}, {}, "AMOUNT_ABOVE_LIMIT"),              # 250.01 Kč > 10 € at 25 Kč/€
])
def test_screen_refusals(kw, p_kw, code):
    r = req(**kw)
    assert facilitator.screen(pay(r, **p_kw), r) == code


def test_cap_is_inclusive():
    r = req(amount="10000000")
    assert facilitator.screen(pay(r), r) is None


def test_frozen_payee_or_payer_is_under_review(monkeypatch):
    monkeypatch.setattr(tczk, "_send", lambda *a, **k: None)  # bank-side freeze only, no chain here
    r = req()
    tczk.freeze(BETA, case_ref="case-1")
    assert facilitator.screen(pay(r), r) == "UNDER_REVIEW"
    tczk.unfreeze(BETA)
    assert facilitator.screen(pay(r), r) is None
    tczk.freeze(ALFA, case_ref="case-2")
    assert facilitator.screen(pay(r), r) == "UNDER_REVIEW"


def test_tczk_payee_must_be_allowlisted_and_not_frozen(monkeypatch):
    state = {"allowed": True, "frozen": set()}
    monkeypatch.setattr(tczk, "is_allowed", lambda a, *_: state["allowed"])
    monkeypatch.setattr(tczk, "is_frozen", lambda a, *_: a.lower() in state["frozen"])
    r = req("tCZK", amount="12500")
    assert facilitator.screen(pay(r), r) is None
    state["allowed"] = False
    assert facilitator.screen(pay(r), r) == "COUNTERPARTY_NOT_IN_SCOPE"  # N09
    state["allowed"], state["frozen"] = True, {BETA.lower()}
    assert facilitator.screen(pay(r), r) == "UNDER_REVIEW"


def test_eurc_screening_never_reads_tczk_state(monkeypatch):
    monkeypatch.setattr(tczk, "is_allowed", lambda *_: pytest.fail("EURC must not read the tCZK allowlist"))
    r = req()
    assert facilitator.screen(pay(r), r) is None


# ---- HTTP shape ----

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(facilitator.router)
    return TestClient(app)


def test_verify_refusal_is_http_200_is_valid_false(client):
    r = req(pay_to=STRANGER)
    res = client.post("/x402/verify", json=body(pay(r), r))
    assert res.status_code == 200
    assert res.json() == {"isValid": False, "invalidReason": "COUNTERPARTY_NOT_IN_SCOPE", "payer": ALFA}
    entry = journal.entries()[0]  # newest first
    assert entry["event"] == "x402_refused" and entry["payload"]["reason"] == "payee_not_a_bank_client"


def test_settle_refusal_is_http_200_and_sends_nothing(client):
    r = req(amount="99000000")
    res = client.post("/x402/settle", json=body(pay(r), r))
    assert res.status_code == 200
    out = res.json()
    assert out["isValid"] is False and out["invalidReason"] == "AMOUNT_ABOVE_LIMIT"
    assert out["success"] is False and out["errorReason"] == "AMOUNT_ABOVE_LIMIT" and out["transaction"] == ""
    assert db.rows("SELECT * FROM onchain_txs") == []  # no money moved, not even an attempt
    assert journal.entries()[0]["payload"]["stage"] == "settle"


def test_refusal_parses_as_sdk_responses(client):
    """The resource server parses our answers with the SDK models: the refusal must validate as both."""
    from x402.schemas import SettleResponse, VerifyResponse
    r = req(pay_to=STRANGER)
    v = VerifyResponse.model_validate(client.post("/x402/verify", json=body(pay(r), r)).json())
    s = SettleResponse.model_validate(client.post("/x402/settle", json=body(pay(r), r)).json())
    assert (v.is_valid, v.invalid_reason) == (False, "COUNTERPARTY_NOT_IN_SCOPE")
    assert (s.success, s.error_reason) == (False, "COUNTERPARTY_NOT_IN_SCOPE")


def test_malformed_body_is_a_refusal_not_500(client):
    res = client.post("/x402/verify", json={"x402Version": 2, "paymentPayload": {"nonsense": 1}})
    assert res.status_code == 200 and res.json()["isValid"] is False
    assert res.json()["invalidReason"] == "invalid_payload"


def test_supported_lists_base_sepolia_and_both_assets(client):
    out = client.get("/x402/supported").json()
    assert out["kinds"] == [{"x402Version": 2, "scheme": "exact", "network": "eip155:84532"}]
    assets = {a["asset"]: a for a in out["assets"]}
    assert assets["EURC"]["extra"] == {"name": "EURC", "version": "2"}
    assert assets["tCZK"]["address"] == TCZK_ADDR and assets["tCZK"]["extra"]["name"] == "tCZK Deposit Token"
    assert out["signers"]["eip155:*"] == ["0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"]


def test_bank_app_mounts_the_facilitator():
    from bank.app import app
    assert TestClient(app).get("/x402/supported").json()["kinds"][0]["network"] == "eip155:84532"
