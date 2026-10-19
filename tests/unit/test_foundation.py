# Foundation: money conversions, journal hash chain, timings/cost, seed + core lookups, chain sender.
import threading

import pytest
from eth_account import Account

from bank import chain, config, core, db, journal, money, timing

REVERTER_INITCODE = "0x6005600c60003960056000f360006000fd"  # runtime: PUSH1 0 PUSH1 0 REVERT


# --- money -------------------------------------------------------------------------------------

def test_eurc_and_tczk_conversions_match_demo_rate():
    assert money.to_atomic("EURC", "5.00") == 5_000_000
    assert money.to_atomic("tCZK", "125.00") == 12_500
    assert money.to_eur_cents("EURC", 5_000_000) == 500
    assert money.to_eur_cents("tCZK", 12_500) == 500  # 125,00 Kč = 5 € at 25 Kč/€
    assert money.from_eur_cents("tCZK", 500) == 12_500
    assert money.from_eur_cents("EURC", 1000) == 10_000_000


def test_eur_cents_round_up_so_limits_are_never_undercounted():
    assert money.to_eur_cents("EURC", 1) == 1
    assert money.to_eur_cents("tCZK", 26) == 2
    assert money.to_eur_cents("EURC", 0) == 0


def test_to_atomic_rejects_extra_precision():
    with pytest.raises(ValueError):
        money.to_atomic("tCZK", "1.005")


def test_unknown_asset_rejected():
    with pytest.raises(ValueError):
        money.to_eur_cents("USDT", 1)
    with pytest.raises(ValueError):
        money.from_eur_cents("USDT", 1)


def test_fmt_uses_czech_comma():
    assert money.fmt("EURC", 5_000_000) == "5,00 €"
    assert money.fmt("EURC", 225_000) == "0,225 €"
    assert money.fmt("tCZK", 12_500) == "125,00 Kč"


def test_per_item_split_18_of_20():
    assert money.per_item_split(5_000_000, 20, 18) == (4_500_000, 500_000)
    to_seller, back = money.per_item_split(1_000_001, 3, 2)
    assert to_seller + back == 1_000_001 and to_seller == 666_667
    assert money.per_item_split(1_000_000, 3, 3) == (1_000_000, 0)  # fully accepted: no dust withheld
    assert money.per_item_split(1_000_000, 3, 0) == (0, 1_000_000)
    with pytest.raises(ValueError):
        money.per_item_split(100, 2, 3)
    with pytest.raises(ValueError):
        money.per_item_split(-100, 2, 1)


def test_negative_or_zero_amounts_rejected():
    for bad in ("-5", "0"):
        with pytest.raises(ValueError):
            money.to_atomic("EURC", bad)
    with pytest.raises(ValueError):
        money.to_eur_cents("EURC", -1)


def test_bedrock_inference_profile_prefixes_priced_right():
    for model in ("anthropic.claude-haiku-5-5", "us.anthropic.claude-haiku-5-5", "global.anthropic.claude-haiku-5-5",
                  "eu.anthropic.claude-haiku-5-5"):
        assert timing.price_of(model) == (0.10, 0.50), model
    assert timing.price_of("claude-opus-5-5") == (4.00, 20.00)


def test_config_reload_reads_tczk_name(monkeypatch):
    monkeypatch.setenv("TCZK_NAME", "Demo Bank tCZK")
    config.reload()
    assert config.ASSETS["tCZK"]["name"] == "Demo Bank tCZK"


# --- journal -----------------------------------------------------------------------------------

def test_journal_chain_verifies_and_detects_tamper():
    journal.append("bank", "decision", {"code": "AMOUNT_ABOVE_LIMIT"}, deal_id="d1")
    journal.append("agent:buyer", "tool_call", {"tool": "pay", "source": "untrusted:seller"}, deal_id="d1")
    journal.append("timer", "capture", {"amount": 250000}, deal_id="d1")
    assert journal.verify() == (True, None)
    assert [e["event"] for e in journal.entries("d1")] == ["capture", "tool_call", "decision"]
    db.run("UPDATE journal SET payload_json=? WHERE id=2", (db.dumps({"tool": "pay"}),))
    assert journal.verify() == (False, 2)


def test_journal_first_entry_links_to_genesis():
    e = journal.append("bank", "start")
    assert e["prev_hash"] == journal.GENESIS


def test_journal_append_joins_an_outer_transaction():
    with db.immediate() as c:
        c.execute("INSERT INTO reservations (mandate_id, deal_id, amount, state, created_at) VALUES ('m','d',5,'reserved',0)")
        journal.append("bank", "reserved", {"amount": 5}, deal_id="d")
    assert journal.verify() == (True, None) and db.one("SELECT COUNT(*) n FROM reservations")["n"] == 1
    with pytest.raises(RuntimeError):
        with db.immediate() as c:
            c.execute("INSERT INTO reservations (mandate_id, deal_id, amount, state, created_at)"
                      " VALUES ('m','d2',5,'reserved',0)")
            journal.append("bank", "reserved", {"amount": 5}, deal_id="d2")
            raise RuntimeError("abort")
    assert db.one("SELECT COUNT(*) n FROM reservations")["n"] == 1  # inner write rolled back with the outer
    assert len(journal.entries()) == 1


def test_one_reservation_per_deal():
    import sqlite3
    db.run("INSERT INTO reservations (mandate_id, deal_id, amount, state, created_at) VALUES ('m','d',5,'reserved',0)")
    with pytest.raises(sqlite3.IntegrityError):
        db.run("INSERT INTO reservations (mandate_id, deal_id, amount, state, created_at) VALUES ('m','d',5,'reserved',0)")


# --- timing ------------------------------------------------------------------------------------

def test_timed_records_ok_and_failure():
    with timing.timed("decision", "d1"):
        pass
    with pytest.raises(RuntimeError):
        with timing.timed("decision", "d1"):
            raise RuntimeError("boom")
    rows = db.rows("SELECT ok FROM timings WHERE stage='decision' ORDER BY id")
    assert [r["ok"] for r in rows] == [1, 0]
    assert timing.stage_stats("decision")["n"] == 1


def test_llm_cost_logging_and_totals():
    cost = timing.log_llm("negotiate", "d1", "anthropic.claude-opus-5-5",
                          {"input_tokens": 1_000_000, "output_tokens": 100_000}, ms=900)
    assert cost == pytest.approx(4.00 + 2.00)
    timing.log_llm("judge", "d1", "anthropic.claude-haiku-4-5",
                   {"input_tokens": 1000, "output_tokens": 0, "cache_read_input_tokens": 1000}, ms=100)
    totals = timing.llm_totals()
    assert totals["calls"] == 2 and totals["input_tokens"] == 1_001_000
    assert timing.cost_usd("some-unknown-model", 1_000_000) == 5.00  # unknown counts high, not free


def test_percentile_nearest_rank():
    assert timing.percentile([], 95) is None
    assert timing.percentile([5.0], 95) == 5.0
    assert timing.percentile([float(i) for i in range(1, 101)], 95) == 95.0
    assert timing.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0


# --- seed + core -------------------------------------------------------------------------------

def _ico_ok(ico):
    s = sum(int(d) * w for d, w in zip(ico[:7], [8, 7, 6, 5, 4, 3, 2]))
    return (11 - s % 11) % 10 == int(ico[7])


def test_seed_is_idempotent_and_core_lookups(seeded):
    from scripts.seed import seed
    assert seed() == seeded
    assert db.one("SELECT COUNT(*) n FROM companies")["n"] == 4
    beta = core.seller("s-beta")
    assert beta["ico"] == "30181020" and beta["account_number"] == "5638829718/9999"
    assert core.bound_account(beta["wallet_address"].lower()) == "5638829718/9999"
    assert core.company_by_ico("64146103")["id"] == "alfa"
    assert [s["seller_id"] for s in core.sellers("data")] == ["s-beta", "s-delta", "s-gama"]
    assert core.bound_account("0x0000000000000000000000000000000000000001") is None


def test_fixture_icos_and_accounts_are_valid():
    for c in db.loads((config.ROOT / "fixtures" / "companies.json").read_text())["companies"]:
        assert _ico_ok(c["ico"]), c["ico"]
        number = c["account_number"].split("/")[0].rjust(10, "0")
        assert sum(int(d) * w for d, w in zip(number, [6, 3, 7, 9, 10, 5, 8, 4, 2, 1])) % 11 == 0
        bban = c["iban"][4:]
        assert int(bban + "1235" + c["iban"][2:4]) % 97 == 1  # IBAN mod-97 check


def test_wallet_key_missing_is_a_clear_error(seeded, monkeypatch):
    monkeypatch.delenv("ALFA_PK", raising=False)
    with pytest.raises(RuntimeError, match="TODO\\(credential\\)"):
        core.wallet_key("alfa")
    monkeypatch.setenv("ALFA_PK", "0x" + "11" * 32)
    assert core.wallet_key("alfa") == "0x" + "11" * 32


# --- chain sender (in-memory chain) ------------------------------------------------------------

def _eth(to, value=1):
    return {"to": to, "value": value, "gas": 21_000}


@pytest.mark.chain
def test_sender_sends_sequential_nonces(local_chain):
    w3, keys = local_chain
    s = chain.sender(keys[0])
    dest = Account.from_key(keys[1]).address
    results = [s.send(_eth(dest), kind="eth") for _ in range(3)]
    assert [r["status"] for r in results] == ["mined"] * 3
    nonces = [w3.eth.get_transaction(r["tx_hash"])["nonce"] for r in results]
    assert nonces == sorted(nonces) and len(set(nonces)) == 3


@pytest.mark.chain
def test_sender_is_safe_under_parallel_calls(local_chain):
    w3, keys = local_chain
    s = chain.sender(keys[0])
    dest = Account.from_key(keys[2]).address
    out, errors = [], []

    def go():
        try:
            out.append(s.send(_eth(dest), kind="eth"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len({r["tx_hash"] for r in out}) == 6


@pytest.mark.chain
def test_idempotency_key_never_broadcasts_twice(local_chain):
    w3, keys = local_chain
    s = chain.sender(keys[0])
    dest = Account.from_key(keys[1]).address
    before = w3.eth.get_balance(dest)
    first = s.send(_eth(dest, 7), kind="capture", deal_id="d1", idem_key="d1:capture:1")
    second = s.send(_eth(dest, 7), kind="capture", deal_id="d1", idem_key="d1:capture:1")
    assert second["tx_hash"] == first["tx_hash"] and second["reused"]
    assert w3.eth.get_balance(dest) - before == 7
    assert db.one("SELECT COUNT(*) n FROM onchain_txs WHERE idem_key='d1:capture:1'")["n"] == 1


@pytest.mark.chain
def test_nonce_clash_is_retried(local_chain, monkeypatch):
    w3, keys = local_chain
    s = chain.sender(keys[0])
    real = w3.eth.send_raw_transaction
    calls = {"n": 0}
    acct = Account.from_key(keys[0])

    def flaky(raw):
        calls["n"] += 1
        if calls["n"] == 1:  # someone else already used this nonce (stale pending count on a balanced RPC)
            other = acct.sign_transaction({"to": acct.address, "value": 0, "gas": 21_000, "gasPrice": w3.eth.gas_price,
                                           "nonce": w3.eth.get_transaction_count(acct.address), "chainId": w3.eth.chain_id})
            real(other.raw_transaction)
            raise ValueError({"message": "replacement transaction underpriced"})
        return real(raw)

    monkeypatch.setattr(w3.eth, "send_raw_transaction", flaky)
    monkeypatch.setattr(chain.time, "sleep", lambda _: None)
    r = s.send(_eth(Account.from_key(keys[1]).address), kind="eth", idem_key="k1")
    assert r["status"] == "mined" and calls["n"] == 2
    statuses = sorted(x["status"] for x in db.rows("SELECT status FROM onchain_txs"))
    assert statuses == ["failed", "mined"]


@pytest.mark.chain
def test_stale_local_nonce_recovers(local_chain):
    w3, keys = local_chain
    s = chain.sender(keys[0])
    dest = Account.from_key(keys[1]).address
    s.send(_eth(dest), kind="eth")
    s.nonce = 0  # pretend our counter went stale (e.g. another process sent with this key)
    r = s.send(_eth(dest), kind="eth")
    assert r["status"] == "mined"


@pytest.mark.chain
def test_unexpected_revert_raises_expected_revert_returns_receipt(local_chain):
    w3, keys = local_chain
    s = chain.sender(keys[0])
    deployed = s.send({"data": REVERTER_INITCODE, "gas": 100_000}, kind="deploy")
    addr = deployed["receipt"]["contractAddress"]
    r = s.send({"to": addr, "data": "0x"}, kind="n10_direct_transfer", expect_revert=True)
    assert r["status"] == "reverted"
    with pytest.raises(chain.TxReverted):
        s.send({"to": addr, "data": "0x", "gas": 50_000}, kind="should_not_revert")


@pytest.mark.chain
def test_reverted_tx_reused_by_key_still_raises(local_chain):
    w3, keys = local_chain
    s = chain.sender(keys[0])
    addr = s.send({"data": REVERTER_INITCODE, "gas": 100_000}, kind="deploy")["receipt"]["contractAddress"]
    with pytest.raises(chain.TxReverted):
        s.send({"to": addr, "data": "0x", "gas": 50_000}, kind="capture", idem_key="d9:capture:1")
    with pytest.raises(chain.TxReverted):
        s.send({"to": addr, "data": "0x", "gas": 50_000}, kind="capture", idem_key="d9:capture:1")


@pytest.mark.chain
def test_ambiguous_rpc_error_after_node_accepted_never_double_pays(local_chain, monkeypatch):
    """Review HIGH-1: the node took the tx but the client saw a connection error; the retry must not pay twice."""
    w3, keys = local_chain
    s = chain.sender(keys[0])
    dest = Account.from_key(keys[3]).address
    before = w3.eth.get_balance(dest)
    real = w3.eth.send_raw_transaction
    calls = {"n": 0}

    def accepted_then_lost(raw):
        calls["n"] += 1
        real(raw)
        if calls["n"] == 1:
            raise ConnectionError("connection reset by peer")

    monkeypatch.setattr(w3.eth, "send_raw_transaction", accepted_then_lost)
    with pytest.raises(ConnectionError):
        s.send(_eth(dest, 5), kind="capture", idem_key="d1:capture:1")
    r = s.send(_eth(dest, 5), kind="capture", idem_key="d1:capture:1")
    assert r["status"] == "mined" and w3.eth.get_balance(dest) - before == 5
    assert w3.eth.get_transaction_count(Account.from_key(keys[0]).address) == 1


@pytest.mark.chain
def test_ambiguous_error_before_node_saw_it_rebroadcasts_same_nonce(local_chain, monkeypatch):
    """Review HIGH-2: signed + stored but never reached the node -> the retry re-sends the same bytes (no gap)."""
    w3, keys = local_chain
    s = chain.sender(keys[0])
    dest = Account.from_key(keys[3]).address
    real = w3.eth.send_raw_transaction
    calls = {"n": 0}

    def lost(raw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("read timeout")
        return real(raw)

    monkeypatch.setattr(w3.eth, "send_raw_transaction", lost)
    with pytest.raises(TimeoutError):
        s.send(_eth(dest, 5), kind="authorize", idem_key="d2:authorize")
    row = db.one("SELECT status, nonce FROM onchain_txs WHERE idem_key='d2:authorize'")
    assert row == {"status": "signed", "nonce": 0}
    s2 = chain.Sender(Account.from_key(keys[0]))  # a fresh process: no local counter
    r = s2.send(_eth(dest, 5), kind="authorize", idem_key="d2:authorize")
    assert r["status"] == "mined" and w3.eth.get_transaction(r["tx_hash"])["nonce"] == 0
    assert s2.send(_eth(dest, 1), kind="eth")["status"] == "mined"  # key not wedged afterwards


@pytest.mark.chain
def test_dead_signed_tx_frees_its_key(local_chain, monkeypatch):
    """A stored tx whose nonce was consumed by another tx can never land: build a new one."""
    w3, keys = local_chain
    acct = Account.from_key(keys[0])
    s = chain.sender(keys[0])
    dest = Account.from_key(keys[3]).address
    real = w3.eth.send_raw_transaction
    monkeypatch.setattr(w3.eth, "send_raw_transaction", lambda raw: (_ for _ in ()).throw(TimeoutError("timeout")))
    with pytest.raises(TimeoutError):
        s.send(_eth(dest, 5), kind="capture", idem_key="d3:capture:1")
    monkeypatch.setattr(w3.eth, "send_raw_transaction", real)
    monkeypatch.setattr(chain.time, "sleep", lambda _: None)
    other = acct.sign_transaction({"to": acct.address, "value": 0, "gas": 21_000, "gasPrice": w3.eth.gas_price,
                                   "nonce": 0, "chainId": w3.eth.chain_id})
    real(other.raw_transaction)  # nonce 0 now taken by a different tx
    before = w3.eth.get_balance(dest)
    r = s.send(_eth(dest, 5), kind="capture", idem_key="d3:capture:1")
    assert r["status"] == "mined" and w3.eth.get_balance(dest) - before == 5
    assert db.one("SELECT COUNT(*) n FROM onchain_txs WHERE status='failed'")["n"] == 1


@pytest.mark.chain
def test_transfers_and_chunked_logs_on_empty_range(local_chain):
    w3, keys = local_chain
    s = chain.sender(keys[0])
    deployed = s.send({"data": REVERTER_INITCODE, "gas": 100_000}, kind="deploy")
    addr = deployed["receipt"]["contractAddress"]
    assert chain.transfers(addr, 0, frm=Account.from_key(keys[0]).address) == []
    assert chain.get_logs(addr, [], 0, step=1) == []


def test_sender_without_key_is_a_clear_error():
    with pytest.raises(RuntimeError, match="TODO\\(credential\\)"):
        chain.sender("")


def test_read_retry_returns_when_ok(monkeypatch):
    monkeypatch.setattr(chain.time, "sleep", lambda _: None)
    seq = iter([0, 0, 5])
    assert chain.read_retry(lambda: next(seq), ok=lambda v: v > 0) == 5


def test_tx_link():
    assert chain.tx_link("abc").endswith("/tx/0xabc")


@pytest.mark.chain
def test_revert_at_gas_estimation_is_a_tx_would_revert(local_chain):
    w3, keys = local_chain
    s = chain.sender(keys[0])
    addr = s.send({"data": REVERTER_INITCODE, "gas": 100_000}, kind="deploy")["receipt"]["contractAddress"]
    with pytest.raises(chain.TxWouldRevert):
        s.send({"to": addr, "data": "0x"}, kind="capture", idem_key="d7:capture:1")  # no gas given -> estimated
    assert db.one("SELECT COUNT(*) n FROM onchain_txs WHERE idem_key='d7:capture:1'")["n"] == 0  # nothing signed
    assert issubclass(chain.TxWouldRevert, chain.TxReverted)
