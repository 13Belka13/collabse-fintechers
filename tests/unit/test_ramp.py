# Ramp tCZK <-> CZK (bank/ramp.py, P1): deposit = debit then mint, withdraw = burn then credit, payout to the seller's
# account after each escrow capture of a tCZK deal, one ref moves money once, and the bank's books reconcile with the
# token's totalSupply. Real mint/burn on the in-memory chain (like test_tczk.py); payouts on the dealkit fake sender
# (like test_disputes.py); the DEMO_SIM chain for simulated mint/burn without explorer links.
import time

import pytest
from eth_account import Account

from bank import chain, config, db, escrow, journal, ramp, tczk, ui
from scripts import demo_sim
from scripts.seed import seed
from tests.unit import dealkit as k

ALFA = "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140"
ALFA_START = 1_000_000  # 10 000,00 Kč, scripts/seed.py


def events(name):
    return [e for e in journal.entries(limit=500) if e["event"] == name]


def balance(company_id):
    return ramp.account(company_id)["balance"]


@pytest.fixture
def bank_chain(local_chain, monkeypatch):
    """tCZK deployed on the in-memory chain (setup mints 1 000,00 Kč to Alfa), then the seed records that supply."""
    w3, keys = local_chain
    monkeypatch.setenv("BANK_ADMIN_PK", keys[0])
    config.reload()
    addr = tczk.deploy(store=Account.from_key(keys[9]).address, mint_alfa=100_000)
    monkeypatch.setenv("TCZK", addr)
    monkeypatch.setenv("TCZK_NAME", "tCZK Deposit Token")
    config.reload()
    seed()
    return tczk.token()


def supply(t):
    return t.functions.totalSupply().call()


# ---- deposit / withdraw on the in-memory chain ----

@pytest.mark.chain
def test_seed_opens_accounts_and_records_the_initial_supply_once(bank_chain):
    assert balance("alfa") == ALFA_START and balance("beta") == 0
    assert ramp.account("beta")["payout_mode"] == "account" and ramp.account("alfa")["payout_mode"] == "wallet"
    assert ramp.books()["initial"] == 100_000
    ramp.deposit("alfa", 5_000, "human:alfa")
    seed()  # a restart re-seeds: neither the balance nor the initial issuance is reset
    assert balance("alfa") == ALFA_START - 5_000 and ramp.books()["initial"] == 100_000


@pytest.mark.chain
def test_deposit_debits_then_mints_and_journals_a_masked_account(bank_chain):
    o = ramp.deposit("alfa", 10_000, "human:alfa", ref="dep-1")
    assert o["state"] == "done" and o["tx_hash"]
    assert balance("alfa") == ALFA_START - 10_000
    assert bank_chain.functions.balanceOf(ALFA).call() == 110_000
    e = events("ramp_deposit")[0]
    assert e["payload"]["account"] == "CZ…9684" and "1803729684" not in db.dumps(e["payload"])
    assert e["payload"]["tx_hash"] == o["tx_hash"] and e["payload"]["amount_text"] == "100,00 Kč"


@pytest.mark.chain
def test_deposit_with_the_same_ref_moves_money_once(bank_chain):
    first = ramp.deposit("alfa", 2_500, "human:alfa", ref="dep-7")
    again = ramp.deposit("alfa", 2_500, "human:alfa", ref="dep-7")
    assert again["tx_hash"] == first["tx_hash"]
    assert balance("alfa") == ALFA_START - 2_500  # one debit
    assert bank_chain.functions.balanceOf(ALFA).call() == 102_500  # one mint
    assert len(events("ramp_deposit")) == 1


@pytest.mark.chain
def test_failed_mint_gives_the_debit_back(bank_chain, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("missing private key in .env (TODO(credential))")  # nothing was signed
    monkeypatch.setattr(tczk, "mint", broken)
    o = ramp.deposit("alfa", 10_000, "human:alfa")
    assert o["state"] == "failed" and balance("alfa") == ALFA_START
    assert supply(bank_chain) == 100_000 and events("ramp_failed")


@pytest.mark.chain
def test_mint_without_a_receipt_stays_pending_and_never_refunds(bank_chain, monkeypatch):
    """The tx was signed and sent but the answer was lost: it may still land, so the debit stays; a retry resends."""
    def lost(to, amount, ref):
        db.run("INSERT INTO onchain_txs (tx_hash, kind, status, idem_key, created_at) VALUES (?,?,?,?,?)",
               ("0x" + "ab" * 32, "tczk_mint", "signed", f"tczk:mint:{ref}", time.time()))
        raise TimeoutError("RPC timeout")
    monkeypatch.setattr(tczk, "mint", lost)
    o = ramp.deposit("alfa", 10_000, "human:alfa", ref="dep-lost")
    assert o["state"] == "pending" and balance("alfa") == ALFA_START - 10_000
    monkeypatch.setattr(tczk, "mint", lambda to, amount, ref: {"tx_hash": "0x" + "ab" * 32})
    assert ramp.retry("dep-lost", "human:bank-officer")["state"] == "done"
    assert balance("alfa") == ALFA_START - 10_000 and len(events("ramp_deposit")) == 1


@pytest.mark.chain
def test_deposit_above_the_balance_is_refused_before_anything_moves(bank_chain):
    with pytest.raises(ramp.RampError) as e:
        ramp.deposit("alfa", ALFA_START + 1, "human:alfa")
    assert e.value.status == 409 and balance("alfa") == ALFA_START
    assert db.one("SELECT COUNT(*) n FROM ramp_ops WHERE direction='deposit'")["n"] == 0
    with pytest.raises(ramp.RampError):
        ramp.deposit("alfa", 0, "human:alfa")


@pytest.mark.chain
def test_withdraw_burns_then_credits_once_per_ref(bank_chain):
    o = ramp.withdraw("alfa", 40_000, "human:alfa", ref="wd-1")
    again = ramp.withdraw("alfa", 40_000, "human:alfa", ref="wd-1")
    assert o["state"] == "done" and again["tx_hash"] == o["tx_hash"]
    assert bank_chain.functions.balanceOf(ALFA).call() == 60_000
    assert balance("alfa") == ALFA_START + 40_000 and len(events("ramp_withdraw")) == 1


@pytest.mark.chain
def test_failed_burn_never_credits(bank_chain):
    o = ramp.withdraw("alfa", 999_999, "human:alfa")  # more than the wallet holds: the burn would revert
    assert o["state"] == "failed" and balance("alfa") == ALFA_START
    assert bank_chain.functions.balanceOf(ALFA).call() == 100_000


@pytest.mark.chain
def test_payout_burn_that_reverted_is_retried_with_a_fresh_tx(bank_chain):
    """A payout burn is sent with fixed gas (no estimate on a lagging node): if it reverts, nothing moved and the
    retry builds a new tx under the same ref once the tokens are there."""
    beta = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"
    db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, asset, amount, state, created_at) VALUES"
           " ('d-chain','alfa','s-beta','tCZK',12500,'settled',0)")
    o = ramp.payout("d-chain", 12_500, 1)  # Beta's wallet is still empty: the burn reverts on chain
    assert o["state"] == "failed" and balance("beta") == 0
    assert db.one("SELECT status FROM onchain_txs WHERE idem_key='tczk:burn:payout:d-chain:1'")["status"] == "reverted"
    tczk.mint(beta, 12_500, "capture-stand-in")
    assert ramp.retry("payout:d-chain:1", "human:bank-officer")["state"] == "done"
    assert bank_chain.functions.balanceOf(beta).call() == 0 and balance("beta") == 12_500
    assert ramp.payout("d-chain", 12_500, 1)["state"] == "done" and balance("beta") == 12_500  # once per ref


@pytest.mark.chain
def test_reconcile_books_equal_chain_after_a_series(bank_chain):
    ramp.deposit("alfa", 30_000, "human:alfa")
    ramp.withdraw("alfa", 12_345, "human:alfa")
    ramp.withdraw("alfa", 10**9, "human:alfa")  # failed: counts nowhere
    ramp.deposit("alfa", 1, "human:alfa")
    r = ramp.reconcile()
    assert r["ok"] and r["diff"] == 0 and r["chain"] == supply(bank_chain) == 100_000 + 30_001 - 12_345
    # a mint outside the ramp shows up as a difference
    chain.sender(config.BANK_ADMIN_PK).send(bank_chain.functions.mint(ALFA, 500, tczk.bytes32("side")), kind="tczk_mint")
    r = ramp.reconcile()
    assert r["ok"] is False and r["diff"] == 500


def test_prep_token_turns_the_ramp_off(monkeypatch, seeded):
    monkeypatch.setenv("TCZK", "0x6a99C9e95DD220172d07D5f730f2e82B466D4B67")
    monkeypatch.setenv("TCZK_NAME", tczk.PREP_NAME)
    config.reload()
    assert "no burn" in ramp.off_reason()
    with pytest.raises(ramp.RampError) as e:
        ramp.deposit("alfa", 100, "human:alfa")
    assert e.value.status == 409 and balance("alfa") == ALFA_START
    assert ramp.reconcile()["off"]


# ---- payout after each capture (dealkit fake sender: the real escrow and dispute code) ----

@pytest.fixture
def world(monkeypatch, seeded):
    monkeypatch.setenv("TCZK", demo_sim.SIM_TCZK)
    monkeypatch.setenv("TCZK_NAME", "tCZK Deposit Token")
    config.reload()
    return k.world(monkeypatch)


def burns(w):
    return [key for fn, _, key, _ in w.sender.sent if fn == "burn"]


def test_payout_is_net_of_the_bank_fee(world):
    deal_id = k.make_deal(world, rule="A", deal_id="d-fee", asset="tCZK", amount=12_500)
    pi = escrow.stored_payment_info(deal_id)
    db.run("UPDATE deals SET payment_info_json=? WHERE deal_id=?", (db.dumps({**pi, "maxFeeBps": 80}), deal_id))
    ramp.after_capture(deal_id, 12_500, 1)
    assert ramp.op("payout:d-fee:1")["amount"] == 12_500 - 100  # 0,8 % stays with the bank


# ---- DEMO_SIM: simulated mint/burn, never an explorer link ----

def test_sim_chain_mints_and_burns_and_reconciles(monkeypatch, seeded):
    sim = demo_sim.SimChain()
    monkeypatch.setenv("TCZK", demo_sim.SIM_TCZK)
    monkeypatch.setenv("TCZK_NAME", "tCZK Deposit Token")
    config.reload()
    monkeypatch.setattr(chain, "sender", lambda pk: sim)
    monkeypatch.setattr(ramp, "total_supply", sim.total_supply)
    seed()
    assert ramp.books()["initial"] == demo_sim.SIM_SUPPLY
    o = ramp.deposit("alfa", 20_000, "human:alfa", ref="dep-sim")
    assert ramp.deposit("alfa", 20_000, "human:alfa", ref="dep-sim")["tx_hash"] == o["tx_hash"]
    ramp.withdraw("alfa", 5_000, "human:alfa")
    assert sim.total_supply() == demo_sim.SIM_SUPPLY + 15_000
    r = ramp.reconcile()
    assert r["ok"] and r["diff"] == 0
    assert db.one("SELECT status FROM onchain_txs WHERE tx_hash=?", (o["tx_hash"],))["status"] == "simulated"
    monkeypatch.setattr(demo_sim, "ACTIVE", True)
    assert ui._tx(o["tx_hash"])["url"] is None  # never an explorer link for a simulated tx
    fresh = demo_sim.SimChain()  # after a restart the simulator rebuilds its supply from the bank's books
    assert fresh.total_supply() == sim.total_supply()
