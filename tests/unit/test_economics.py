# Deal economics panel (bank/economics.py): gas from onchain_txs priced at the Base mainnet gas price, the fee the
# captures sent to the bank's clearing address, the margin; typical gas on the simulated chain; ramp mint/burn when
# the ramp table exists; the gas price never blocks a page (no network -> the .env fallback at once).
import threading
import time
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import config, db, economics, journal, ui
from tests.unit import dealkit as k


@pytest.fixture(autouse=True)
def fresh_price(monkeypatch):
    monkeypatch.setattr(economics, "_price", {"wei": None, "at": None, "tried": 0.0, "busy": False})


def deal(deal_id="d-econ", asset="EURC", amount=5_000_000, terms=None):
    terms = {"rule": "B", "tier": "T1", "fee_bps": 120} if terms is None else terms
    db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, mandate_id, asset, amount, terms_json, state, created_at)"
           " VALUES (?,?,?,?,?,?,?,?,?)", (deal_id, "alfa", "s-beta", "m-alfa-1", asset, amount,
                                           db.dumps(terms) if terms else None, "authorized", time.time()))
    return deal_id


def tx(deal_id, kind, gas=None, status="mined", h=None):
    h = h or "0x%064x" % (db.one("SELECT count(*) n FROM onchain_txs")["n"] + 1)
    db.run("INSERT INTO onchain_txs (tx_hash, kind, deal_id, status, gas_used, created_at) VALUES (?,?,?,?,?,?)",
           (h, kind, deal_id, status, gas, time.time()))
    return h


def captured(deal_id, amount, fee):
    journal.append("bank", "escrow_captured", {"amount": amount, "fee": fee, "n": 1, "tx_hash": "0x1"}, deal_id=deal_id)


FALLBACK = {"wei": 6_000_000, "live": False, "at": None}


def test_cost_cents_at_the_fallback_price():
    # 292k gas · 0.006 gwei = 1.752e-6 ETH · 3000 € = 0.5256 ¢
    assert config.BASE_MAINNET_GAS_PRICE_WEI == 6_000_000 and config.ETH_EUR_DEMO == 3000
    assert economics.cost_cents(292_000, 6_000_000) == Decimal("0.5256")
    assert economics.fmt_cents(Decimal("0.5256")) == "0,53 ¢" and economics.fmt_cents(Decimal("0.001")) == "< 0,01 ¢"
    assert economics.fmt_cents(Decimal(150)) == "1,50 €"


def test_eurc_deal_cost_fee_and_margin_from_receipts():
    d = deal()
    tx(d, "authorize", 150_000), tx(d, "capture", 71_000), tx(d, "capture", 71_000)
    tx(d, "capture", None, status="failed")  # never landed: no gas
    tx(d, "void", None, status="sent")  # no receipt yet: not counted
    captured(d, 4_500_000, 54_000), captured(d, 500_000, 6_000)  # rule B: 90 % then 10 %, 1.2 % each
    e = economics.deal(d, FALLBACK)
    assert (e["tx_count"], e["gas"], e["typical"]) == (3, 292_000, False)
    assert e["kinds"] == {"authorize": 1, "capture": 2}
    assert e["cost_cents"] == Decimal("0.5256") and e["fee_cents"] == 6 and e["fee_source"] == "captured"
    assert e["margin_text"] == "91 %" and e["fee_text"] == "6,00 ¢" and e["cost_text"] == "0,53 ¢"
    assert "captured on testnet" in e["fee_note"] and "so far" not in e["fee_note"]
    assert e["caption"].startswith("Testnet deal; cost estimated") and "fallback constant" in e["caption"]


def test_tczk_fee_in_euro_at_the_demo_rate_and_so_far_while_money_waits():
    d = deal("d-tczk", asset="tCZK", amount=12_500)  # 125,00 Kč = 5 €
    tx(d, "authorize", 150_000), tx(d, "capture", 71_000)
    captured(d, 11_250, 135)  # 90 % captured, 1,35 Kč fee; 10 % still in the safe
    e = economics.deal(d, FALLBACK)
    assert e["fee_cents"] == Decimal("5.4") and e["fee_native"] == "1,35 Kč" and e["amount_cents"] == 500
    assert e["fee_note"].endswith("so far")
    assert e["margin_pct"] == (Decimal("5.4") - economics.cost_cents(221_000, 6_000_000)) / Decimal("5.4") * 100


def test_simulated_chain_uses_typical_gas():
    d = deal()
    for kind in ("authorize", "capture", "void"):
        tx(d, kind, None, status="simulated")
    captured(d, 4_000_000, 48_000)
    e = economics.deal(d, FALLBACK)
    assert (e["gas"], e["typical"]) == (150_000 + 71_000 + 64_000, True)
    assert e["caption"].startswith("Simulated chain, typical gas; cost estimated")
    assert "simulated chain" in e["fee_note"]


def test_ramp_mint_and_burn_count_once_when_the_table_exists():
    d = deal("d-ramp", asset="tCZK", amount=12_500)
    tx(d, "authorize", 150_000)
    assert economics.deal(d, FALLBACK)["gas"] == 150_000  # no ramp table in this build: still works
    db.run("CREATE TABLE ramp_ops (ref TEXT PRIMARY KEY, company_id TEXT, direction TEXT, amount INTEGER,"
           " deal_id TEXT, state TEXT, tx_hash TEXT, created_at REAL, updated_at REAL)")
    burn = tx(None, "tczk_burn", 48_000)
    db.run("INSERT INTO ramp_ops VALUES ('payout:d-ramp:1','beta','payout',12000,?,'done',?,0,0)", (d, burn))
    db.run("INSERT INTO ramp_ops VALUES ('payout:d-ramp:2','beta','payout',500,?,'done','0xsim',0,0)", (d,))
    db.run("INSERT INTO ramp_ops VALUES ('payout:d-ramp:3','beta','payout',500,?,'failed','0xdead',0,0)", (d,))
    e = economics.deal(d, FALLBACK)
    assert e["gas"] == 150_000 + 48_000 + economics.TYPICAL_GAS["tczk_burn"] and e["kinds"]["tczk_burn"] == 2
    db.run("UPDATE onchain_txs SET deal_id=? WHERE tx_hash=?", (d, burn))  # the same tx also tagged with the deal
    assert economics.deal(d, FALLBACK)["tx_count"] == 3


def test_fee_before_capture_t0_and_refusals():
    d = deal("d-pending")
    tx(d, "authorize", 150_000)
    e = economics.deal(d, FALLBACK)
    assert e["fee_source"] == "pending" and e["fee_cents"] == 6 and "not captured yet" in e["fee_note"]
    t0 = deal("d-t0", amount=200_000, terms={"rule": "A", "tier": "T0", "fee_bps": 0})
    tx(t0, "authorize", 150_000), tx(t0, "capture", 71_000)
    captured(t0, 200_000, 0)
    e = economics.deal(t0, FALLBACK)
    assert e["fee_source"] == "proposed" and e["fee_atomic"] == 400 and "billed monthly" in e["fee_note"]
    assert "not charged on testnet" in e["caption"]
    refused = deal("d-refused", terms={})
    e = economics.deal(refused, FALLBACK)
    assert (e["tx_count"], e["cost_cents"], e["fee_cents"], e["margin_text"]) == (0, 0, 0, "—")
    assert "no money moved" in e["fee_note"]
    back = deal("d-void")
    tx(back, "authorize", 150_000), tx(back, "void", 64_000)
    journal.append("bank", "escrow_voided", {"tx_hash": "0x2"}, deal_id=back)
    e = economics.deal(back, FALLBACK)
    assert e["fee_cents"] == 0 and e["margin_pct"] is None and "no capture" in e["fee_note"]
    s = economics.summary([{"deal_id": x} for x in (d, t0, refused, back)])
    assert (s["deals"], s["free"], s["txs"], s["fees_cents"]) == (3, 1, 5, 0)
    assert s["avg_cost_cents"] == economics.cost_cents(150_000 + 221_000 + 214_000, 6_000_000) / 3


def test_gas_price_never_waits_for_the_network(monkeypatch):
    monkeypatch.setattr(config, "BASE_MAINNET_RPC", "https://mainnet.example")
    gate, calls = threading.Event(), []

    def slow(rpc):
        calls.append(rpc)
        gate.wait(5)
        return 9_000_000

    monkeypatch.setattr(economics, "fetch_gas_price", slow)
    t0 = time.perf_counter()
    assert economics.gas_price() == FALLBACK and economics.gas_price() == FALLBACK  # one read in flight, not two
    assert time.perf_counter() - t0 < 0.1 and calls == ["https://mainnet.example"]
    gate.set()
    for _ in range(50):
        if not economics._price["busy"]:
            break
        time.sleep(0.02)
    p = economics.gas_price()
    assert p["wei"] == 9_000_000 and p["live"] and len(calls) == 1  # cached for 10 minutes


def test_gas_price_falls_back_when_the_rpc_fails(monkeypatch):
    monkeypatch.setattr(config, "BASE_MAINNET_RPC", "https://mainnet.example")

    def down(*a, **kw):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "post", down)
    assert economics.gas_price() == FALLBACK
    for _ in range(50):
        if not economics._price["busy"]:
            break
        time.sleep(0.02)
    assert economics.gas_price() == FALLBACK and economics._price["tried"] > 0  # retried only after a minute
    with pytest.raises(ValueError):
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(
            200, json={"result": hex(10**13)}, request=httpx.Request("POST", "https://x")))
        economics.fetch_gas_price("https://x")


def test_pages_render_without_network_or_rpc(monkeypatch, seeded):
    w = k.world(monkeypatch)
    monkeypatch.setattr(config, "BASE_MAINNET_RPC", "https://mainnet.example")

    def down(*a, **kw):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "post", down)
    monkeypatch.setattr(ui.escrow, "read_state", down)
    d = k.make_deal(w, deal_id="d-page")
    journal.append("bank", "escrow_authorized", {"amount": k.PRICE, "tx_hash": tx(d, "authorize", 150_000)}, deal_id=d)
    db.run("UPDATE deals SET terms_json=? WHERE deal_id=?", (db.dumps({**k.deals.get(d)["terms"], "fee_bps": 120}), d))
    k.deliver(w, d)  # rule B: 90 % captured at acceptance
    tx(d, "capture", 71_000)
    app = FastAPI()
    app.include_router(ui.router)
    c = TestClient(app)
    c.get("/story")  # template compile
    t0 = time.perf_counter()
    story, page, statement = c.get("/story/panel").text, c.get(f"/deals/{d}").text, c.get("/statement").text
    assert time.perf_counter() - t0 < 1.5
    for html in (story, page):
        assert "What this deal cost and what the bank earned" in html and "Network cost" in html
        assert "0,40 ¢" in html  # 221k gas at the fallback price
        assert "Testnet deal; cost estimated at today&#39;s Base mainnet gas price" in html
    assert "Unit economics:" in statement and "average network cost per deal" in statement
