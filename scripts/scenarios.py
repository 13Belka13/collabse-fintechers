# P0 scenario matrix (SPEC §19.2) end to end against the real bank and seller processes and a real chain.
# Each scenario asserts three things: the decision (code or ok), the money on chain (balances, escrow
# paymentState, Transfer events read from the chain — never only our DB) and the journal (entries present,
# hash chain intact). Negative scenarios assert "no money moved": balances equal and no tx from our service
# (the bank's signer nonces unchanged, no onchain_txs row for the deal).
#   python -m scripts.scenarios --runs 3                 # anvil fork of Base Sepolia (default)
#   python -m scripts.scenarios --only N05 --runs 1
#   python -m scripts.scenarios --p0 --target sepolia    # real Base Sepolia, keys from .env
# Writes reports/scenarios-<target>.json (raw results); scripts/report.py turns it into reports/validation.md.
import argparse
import asyncio
import importlib.util
import json
import secrets
import sqlite3
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from eth_account import Account
from web3 import Web3

from bank import chain, config, core, db, escrow, journal, mandate, money
from scripts.stack import AGENT_TOKEN, HUMAN_TOKEN, Stack

REPORTS = config.ROOT / "reports"
EVIL = "0x000000000000000000000000000000000000dEaD"  # the address the injected/swapped 402 asks to pay
TASK_TEXT = "20 warehouses in Brno with rating above 4.0 and a website"
CRITERIA = {"count": 20, "city": "Brno", "min_rating_exclusive": 4.0, "category_keywords": ["sklad", "warehouse"],
            "require_website": True}
DEFAULT_MODES = {"s-beta": "honest", "s-gama": "fake_data", "s-delta": "honest"}
SETTLED = ("settled", "closed", "verified", "disputed")
ERRORS = {Web3.keccak(text=s)[:4].hex(): s.split("(")[0] for s in (
    "TransferViaBankOnly(address)", "ERC3009UsedAuthorization(address,bytes32)", "NotAllowlisted(address)",
    "AccountFrozen(address)")}

# Pending fixes (named) that make a scenario fail today; a failing scenario listed here is reported as xfail.
DEFECTS = {
    "D3": "Tatum webhook /webhooks/tatum not built (L7): no HMAC check, no dedupe by txId",
}
KNOWN = {"F04": ["D3"], "F05": ["D3"]}


class Check(Exception):
    pass


def transfers_in(tx_hash: str, asset_or_token: str) -> list[dict]:
    """Decoded ERC-20 Transfer events of one token inside one tx receipt (read from the chain)."""
    token = config.ASSETS[asset_or_token]["address"] if asset_or_token in config.ASSETS else asset_or_token
    rcpt = chain.w3().eth.get_transaction_receipt(tx_hash)
    out = []
    for log in rcpt["logs"]:
        if log["address"].lower() == token.lower() and log["topics"] and log["topics"][0].to_0x_hex() == chain.TRANSFER_TOPIC:
            out.append({"from": Web3.to_checksum_address("0x" + log["topics"][1].hex()[-40:]),
                        "to": Web3.to_checksum_address("0x" + log["topics"][2].hex()[-40:]),
                        "value": int(log["data"].hex() or "0", 16)})
    return out


class Ctx:
    """One scenario run: bank/seller clients, chain snapshots, collected txs and checks."""

    def __init__(self, stack: Stack, sid: str, run: int):
        self.s, self.sid, self.run = stack, sid, run
        self.bank = httpx.Client(base_url=stack.bank_url, timeout=90, headers={"x-agent-token": AGENT_TOKEN})
        self.human = httpx.Client(base_url=stack.bank_url, timeout=90, headers={"x-human-token": HUMAN_TOKEN})
        self.seller = httpx.Client(base_url=stack.seller_url, timeout=60)
        self.checks: list[dict] = []
        self.deals: list[str] = []
        self.extra_txs: list[dict] = []
        self.expected_debits: list[dict] = []  # buyer debits this scenario is allowed to cause
        self.decisions: list[dict] = []
        self.notes: list[str] = []
        self.amounts: dict = {}
        self.t0 = time.time()
        self.start_block = chain.w3().eth.block_number

    # ---- checks ----
    def check(self, name: str, ok: bool, detail=None) -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def require(self, name: str, ok: bool, detail=None):
        if not self.check(name, ok, detail):
            raise Check(name)

    # ---- addresses and chain reads ----
    def wallet(self, seller_id: str) -> str:
        return core.seller(seller_id)["wallet_address"]

    def watched(self) -> dict:
        store = escrow.contract().functions.getTokenStore(self.s.addr["operator"]).call()
        return {"buyer": self.s.addr["alfa"], "beta": self.wallet("s-beta"), "gama": self.wallet("s-gama"),
                "delta": self.wallet("s-delta"), "evil": EVIL, "escrow_store": store, "collector": config.COLLECTOR}

    def snapshot(self) -> dict:
        w3 = chain.w3()
        out = {"block": w3.eth.block_number, "balances": {}, "nonces": {}}
        for asset in ("EURC", "tCZK"):
            for name, a in self.watched().items():
                out["balances"][f"{asset}:{name}"] = chain.balance(asset, a)
        for role in ("relayer", "operator", "admin"):
            out["nonces"][role] = w3.eth.get_transaction_count(self.s.addr[role])
        return out

    @staticmethod
    def delta(before: dict, after: dict) -> dict:
        return {k: after["balances"][k] - v for k, v in before["balances"].items() if after["balances"][k] != v}

    def no_money_moved(self, before: dict, label: str = "no money moved"):
        after = self.snapshot()
        moved = self.delta(before, after)
        self.check(f"{label}: balances equal (chain)", not moved, moved or None)
        self.check(f"{label}: no tx from the bank's signers (nonces equal)", after["nonces"] == before["nonces"],
                   {"before": before["nonces"], "after": after["nonces"]})
        rows = db.rows(f"SELECT kind, tx_hash FROM onchain_txs WHERE deal_id IN ({','.join('?' * len(self.deals))})",
                       self.deals) if self.deals else []
        self.check(f"{label}: no onchain_txs row for the deal", not rows, rows or None)

    def deal_tx(self, deal_id: str, kind_prefix: str) -> list[dict]:
        return db.rows("SELECT * FROM onchain_txs WHERE deal_id=? AND kind LIKE ? AND status='mined' ORDER BY created_at",
                       (deal_id, kind_prefix + "%"))

    def journal_has(self, deal_id: str, *events) -> bool:
        have = {e["event"] for e in journal.entries(deal_id, limit=500)}
        missing = [e for e in events if e not in have]
        self.check(f"journal: {', '.join(events)}", not missing, {"missing": missing} if missing else None)
        return not missing

    def journal_chain(self):
        ok, bad = journal.verify()
        self.check("journal hash chain verifies", ok, None if ok else {"first_bad_id": bad})

    # ---- bank and seller calls (the agent's API: ids only) ----
    def mandate(self, **policy) -> str:
        mid = f"m-q-{self.sid.lower()}-r{self.run}-{secrets.token_hex(2)}"
        mandate.create(mid, "alfa", policy, by="human:scenarios")
        return mid

    def mode(self, seller_id: str, mode: str):
        self.seller.post("/mode", json={"seller_id": seller_id, "mode": mode}).raise_for_status()

    def open_deal(self, seller_id: str, mandate_id: str, asset: str = "EURC", criteria: dict | None = None,
                  budget_cents: int = 500, wait_screening: bool = True) -> tuple[str, dict]:
        task = {"text": TASK_TEXT, "category": "data", "asset": asset,
                "budget": {"value": str(budget_cents), "currency": "EUR"}, "criteria": criteria or CRITERIA}
        r = self.bank.post("/api/deals", json={"buyer_id": "alfa", "seller_id": seller_id, "mandate_id": mandate_id,
                                               "task": task, "asset": asset})
        r.raise_for_status()
        deal_id = r.json()["deal_id"]
        self.deals.append(deal_id)
        neg = self.bank.post(f"/api/deals/{deal_id}/negotiate", json={"message": "We need the dataset in our task. Price?"})
        neg.raise_for_status()
        if wait_screening:
            self.wait_screening(seller_id)
        offer = (neg.json() or {}).get("offer") or {}
        stored = db.one("SELECT amount FROM deals WHERE deal_id=?", (deal_id,))
        self.amounts[deal_id] = {"asset": asset, "amount": stored and stored["amount"], "price": offer.get("price")}
        return deal_id, neg.json()

    def wait_screening(self, seller_id: str, timeout: float = 15):
        from bank import screening
        ico = core.seller(seller_id)["ico"]
        end = time.time() + timeout
        while time.time() < end and screening.cached(ico) is None:
            time.sleep(0.2)

    def pay(self, deal_id: str, client: httpx.Client | None = None) -> dict:
        r = (client or self.bank).post(f"/api/deals/{deal_id}/pay", json={})
        out = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}
        if r.status_code != 200:
            out = {"status": "http_error", "http_status": r.status_code, "detail": out.get("detail", out)}
        self.decisions.append({"deal_id": deal_id, **{k: out.get(k) for k in ("status", "code", "explanation",
                                                                                 "http_status", "detail")}})
        return out

    def wait_state(self, deal_id: str, states=SETTLED, timeout: float = 90) -> str | None:
        end = time.time() + timeout
        while time.time() < end:
            row = db.one("SELECT state FROM deals WHERE deal_id=?", (deal_id,))
            if row and row["state"] in states:
                return row["state"]
            time.sleep(0.3)
        row = db.one("SELECT state FROM deals WHERE deal_id=?", (deal_id,))
        return row and row["state"]

    def escrow_state(self, deal_id: str, ok=None) -> dict | None:
        """paymentState from the chain; with ok=, retried while the public RPC still serves a stale read."""
        row = db.one("SELECT payment_hash FROM deals WHERE deal_id=?", (deal_id,))
        if not row or not row["payment_hash"]:
            return None
        return chain.read_retry(lambda: escrow.read_state(row["payment_hash"]), ok=ok or (lambda st: True), tries=8,
                                delay=1.5)

    def delta_after(self, before: dict, ok) -> dict:
        """Balance changes since `before`, retried until ok(delta) or the reads stop lagging (load-balanced RPC)."""
        return chain.read_retry(lambda: Ctx.delta(before, self.snapshot()), ok=ok, tries=8, delay=1.5)

    def asset_of(self, deal_id: str) -> str:
        return db.one("SELECT asset FROM deals WHERE deal_id=?", (deal_id,))["asset"]

    def deal_amount(self, deal_id: str) -> int:
        return int(db.one("SELECT amount FROM deals WHERE deal_id=?", (deal_id,))["amount"])

    def expect_lock(self, deal_id: str, asset: str):
        for tx in self.deal_tx(deal_id, "authorize"):
            self.expected_debits.append({"asset": asset, "tx_hash": tx["tx_hash"], "amount": self.deal_amount(deal_id)})

    def txs(self) -> list[dict]:
        rows = db.rows(f"SELECT kind, tx_hash, status, deal_id FROM onchain_txs WHERE deal_id IN "
                       f"({','.join('?' * len(self.deals))}) ORDER BY created_at", self.deals) if self.deals else []
        return rows + self.extra_txs

    def close(self):
        for c in (self.bank, self.human, self.seller):
            c.close()


# ---- shared flows ----

def captured(ctx: Ctx, deal_id: str, asset: str, seller_id: str) -> int:
    """Σ Transfer escrow → seller in the deal's capture txs (read from the chain)."""
    seller = ctx.wallet(seller_id)
    return sum(t["value"] for c in ctx.deal_tx(deal_id, "capture") for t in transfers_in(c["tx_hash"], asset)
               if t["to"] == seller)


def voided(ctx: Ctx, deal_id: str, asset: str) -> int:
    return sum(t["value"] for v in ctx.deal_tx(deal_id, "void") for t in transfers_in(v["tx_hash"], asset)
               if t["to"] == ctx.s.addr["alfa"])


def timer(ctx: Ctx, seconds: float) -> list[dict]:
    """Drive the dispute timer (bank/deadlines.py) to now + seconds instead of sleeping through the windows."""
    from bank import deadlines
    done = deadlines.tick(now=time.time() + seconds)
    ctx.notes.append(f"deadlines.tick(now + {seconds:.0f} s): {len(done)} step(s)")
    return done


def dispute_of(ctx: Ctx, deal_id: str, timeout: float = 60) -> dict:
    """The dispute record once the bank's own settlement is done: no tx pending and the booked capture on chain.
    (The deal state changes before the first capture is sent; ticking the timer from this process while the bank
    still sends would race it.)"""
    from bank import disputes
    end, d = time.time() + timeout, None
    while time.time() < end:
        d = disputes.get(deal_id)
        if d and not d["pending"] and d["captured"] == sum(
                t["value"] for c in ctx.deal_tx(deal_id, "capture") for t in transfers_in(c["tx_hash"], ctx.asset_of(deal_id))
                if t["from"] != ctx.s.addr["alfa"]):
            break
        time.sleep(0.3)
    ctx.require("dispute record opened and the bank's settlement finished", d is not None and not d["pending"],
                d and {"pending": d["pending"], "captured": d["captured"]})
    return d


def timer_journal(ctx: Ctx, deal_id: str, event: str, rule_prefix: str = "") -> bool:
    hits = [e for e in journal.entries(deal_id, limit=500) if e["event"] == event and e["actor"] == "timer"
            and str(e["payload"].get("rule", "")).startswith(rule_prefix)]
    return ctx.check(f"journal: {event} by actor timer" + (f" ({rule_prefix}…)" if rule_prefix else ""), bool(hits))


def settled_escrow(ctx: Ctx, deal_id: str, asset: str, seller_id: str, to_seller: int, before: dict):
    """After an ok pay (SPEC §9.2-9.3): the accepted part minus the 5 % holdback is captured at once, the holdback
    and the items in question wait in escrow; after the windows the timer captures the holdback and voids the rest.
    Every amount is read from Transfer events and paymentState."""
    amount = ctx.deal_amount(deal_id)
    ctx.expect_lock(deal_id, asset)
    state = ctx.wait_state(deal_id, ("settled", "disputed", "closed"))
    ctx.check("settlement ran (deal settled or disputed)", state in ("settled", "disputed", "closed"), state)
    d = dispute_of(ctx, deal_id)
    terms = d["terms"]
    holdback = to_seller * int(terms["holdback_bps"]) // 10_000
    lock = ctx.deal_tx(deal_id, "authorize")
    ctx.require("authorize tx mined", len(lock) == 1, lock)
    moved = [t for t in transfers_in(lock[0]["tx_hash"], asset) if t["from"] == ctx.s.addr["alfa"]]
    ctx.check("authorize: buyer → escrow = price (Transfer event)", sum(t["value"] for t in moved) == amount,
              {"transfers": moved, "price": amount})
    first = captured(ctx, deal_id, asset, seller_id)
    ctx.check("at once: accepted items minus the 5 % holdback captured (Transfer events)", first == to_seller - holdback,
              {"captured": first, "expected": to_seller - holdback, "holdback": holdback})
    st = ctx.escrow_state(deal_id, ok=lambda st: st["capturable"] == amount - first)
    ctx.check("paymentState: holdback + items in question still in escrow", st is not None
              and st["capturable"] == amount - first, {"state": st, "expected": amount - first})
    timer(ctx, max(int(terms[k]) for k in ("review_window_s", "objection_window_s", "holdback_s")) + 1)
    ctx.check("deal closed after the timer", ctx.wait_state(deal_id, ("closed",), timeout=30) == "closed")
    total = captured(ctx, deal_id, asset, seller_id)
    ctx.check("capture to seller in total = accepted items (Transfer events)", total == to_seller,
              {"captured": total, "expected": to_seller})
    back = voided(ctx, deal_id, asset)
    ctx.check("void back to buyer = rest (Transfer events)", back == amount - to_seller,
              {"voided": back, "expected": amount - to_seller})
    st = ctx.escrow_state(deal_id, ok=lambda st: st["capturable"] == 0)
    ctx.check("paymentState: nothing left to capture", st is not None and st["capturable"] == 0, st)
    if holdback:
        timer_journal(ctx, deal_id, "dispute.holdback_released", "holdback_s")
    name = seller_id.removeprefix("s-")
    d = ctx.delta_after(before, lambda d: d.get(f"{asset}:{name}", 0) == to_seller and d.get(f"{asset}:buyer", 0) == -to_seller)
    ctx.check("seller balance +captured (chain)", d.get(f"{asset}:{name}", 0) == to_seller, d)
    ctx.check("buyer balance −captured (chain)", d.get(f"{asset}:buyer", 0) == -to_seller, d)
    ctx.check("escrow store back to its start (chain)", d.get(f"{asset}:escrow_store", 0) == 0, d)


def ok_decision(ctx: Ctx, d: dict) -> bool:
    return ctx.check("decision ok", d.get("status") == "ok", {k: d.get(k) for k in ("status", "code", "explanation",
                                                                                    "http_status", "detail")})


def refused(ctx: Ctx, d: dict, *codes_: str) -> bool:
    return ctx.check(f"decision {' or '.join(codes_)}", d.get("code") in codes_,
                     {k: d.get(k) for k in ("status", "code", "explanation", "http_status", "detail")})


def sdk_payload(ctx: Ctx, asset: str, amount: int, pay_to: str):
    """An EIP-3009 authorization signed by the x402 SDK for the buyer — what the seller middleware forwards."""
    from x402 import x402ClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.schemas import PaymentRequired, PaymentRequirements
    m = config.ASSETS[asset]
    req = PaymentRequirements(scheme="exact", network=config.NETWORK, asset=m["address"], amount=str(amount),
                              pay_to=pay_to, max_timeout_seconds=60, extra={"name": m["name"], "version": m["version"]})
    client = x402ClientSync().set_spend_controls({"max_amount_per_payment": False, "allowed_assets": [
        {"network": config.NETWORK, "asset": m["address"], "max_amount_per_payment": str(amount)}]})
    client.register(config.NETWORK, ExactEvmScheme(EthAccountSigner(Account.from_key(ctx.s.keys["alfa"]))))
    return client.create_payment_payload(PaymentRequired(accepts=[req])), req


def facilitator_client(ctx: Ctx):
    from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
    return HTTPFacilitatorClientSync(FacilitatorConfig(url=f"{ctx.s.bank_url}/x402"))


def revert_name(tx_hash: str) -> str | None:
    """Replay a reverted tx as eth_call at the tx's own block and decode the custom error selector.
    Not at "latest": a lagging node behind the load balancer may not have that block yet and would answer with a
    different (earlier-state) error — the N11 replay once decoded as TransferViaBankOnly that way."""
    w3 = chain.w3()
    tx = w3.eth.get_transaction(tx_hash)
    block = hex(w3.eth.get_transaction_receipt(tx_hash)["blockNumber"])
    data = ""
    for _ in range(8):
        resp = w3.provider.make_request("eth_call", [{"from": tx["from"], "to": tx["to"], "data": tx["input"].to_0x_hex(),
                                                      "gas": hex(tx["gas"])}, block])
        err = resp.get("error") or {}
        data = err.get("data") or ""
        data = data if isinstance(data, str) else data.get("data", "")
        if data.startswith("0x"):
            break
        time.sleep(1)  # the node does not have the block yet ("header not found"): ask again
    return ERRORS.get(data[2:10]) if data.startswith("0x") else None


# ---- P0 scenarios ----

def s01(ctx: Ctx):
    """x402 EURC through the bank facilitator: 402 → pay → hash in PAYMENT-RESPONSE; seller +price."""
    from bank import x402_buyer
    offer = ctx.seller.post("/negotiate", json={"deal_id": f"q-s01-{ctx.run}-{secrets.token_hex(3)}", "seller_id": "s-beta",
                                                 "task": {"criteria": {"count": 1}}}).json()["offer"]
    amount = int(offer["prices"]["EURC"])
    ctx.amounts["x402"] = {"asset": "EURC", "amount": amount}
    url = f"{ctx.s.seller_url}/data/{offer['offer_id']}"
    ctx.require("unpaid request → 402", ctx.seller.get(url).status_code == 402)
    before = ctx.snapshot()
    r, settle = asyncio.run(x402_buyer.get(url, ctx.s.keys["alfa"], "EURC"))
    ctx.require("paid request → 200 with PAYMENT-RESPONSE tx", r.status_code == 200 and bool(settle)
                and bool(settle.get("transaction")), {"status": r.status_code, "settle": settle})
    tx = settle["transaction"]
    ctx.extra_txs.append({"kind": "x402_settle", "tx_hash": tx, "status": "mined", "deal_id": None})
    ctx.expected_debits.append({"asset": "EURC", "tx_hash": tx, "amount": amount})
    moved = transfers_in(tx, "EURC")
    ctx.check("one Transfer buyer → seller = price in the tx (chain)",
              moved == [{"from": ctx.s.addr["alfa"], "to": ctx.wallet("s-beta"), "value": amount}], moved)
    d = ctx.delta_after(before, lambda d: d == {"EURC:beta": amount, "EURC:buyer": -amount})
    ctx.check("seller +price, buyer −price (chain)", d == {"EURC:beta": amount, "EURC:buyer": -amount}, d)
    settled = [e for e in journal.entries(limit=50) if e["event"] == "x402_settled"
               and e["payload"].get("transaction") == tx]
    ctx.check("journal: x402_settled with the tx", len(settled) == 1)
    ctx.journal_chain()


def s02(ctx: Ctx):
    """Escrow EURC, all accepted: authorize → capture in full; escrow empty."""
    ctx.mode("s-gama", "honest")
    m = ctx.mandate()
    deal, _ = ctx.open_deal("s-gama", m)
    before = ctx.snapshot()
    if ok_decision(ctx, ctx.pay(deal)):
        settled_escrow(ctx, deal, "EURC", "s-gama", ctx.deal_amount(deal), before)
        ctx.journal_has(deal, "escrow_authorized", "escrow_captured", "dispute.opened", "dispute.closed")
    ctx.journal_chain()


def s03(ctx: Ctx, seller_id: str = "s-gama", asset: str = "EURC"):
    """Escrow, 18 of 20 accepted: capture 90 % + void 10 %, to the cent."""
    ctx.mode(seller_id, "fake_data")
    m = ctx.mandate()
    deal, _ = ctx.open_deal(seller_id, m, asset=asset)
    before = ctx.snapshot()
    if ok_decision(ctx, ctx.pay(deal)):
        to_seller, _ = money.per_item_split(ctx.deal_amount(deal), 20, 18)
        settled_escrow(ctx, deal, asset, seller_id, to_seller, before)
        ctx.journal_has(deal, "escrow_authorized", "escrow_captured", "escrow_voided", "dispute.opened",
                        "dispute.closed")
    ctx.journal_chain()


def s04(ctx: Ctx):
    """The same deal in tCZK through escrow; the payee is on the tCZK allowlist (checked on chain) before the lock."""
    from bank import tczk
    beta = ctx.wallet("s-beta")
    ctx.check("payee on the tCZK allowlist and not frozen (chain)", tczk.is_allowed(beta) and not tczk.is_frozen(beta))
    try:
        s03(ctx, "s-beta", "tCZK")
    finally:
        ctx.mode("s-beta", "honest")


def fake_data_deal(ctx: Ctx) -> tuple[str, int, int, dict, dict] | None:
    """Gama fake_data, EURC: 18 accepted by the rules, 2 rejected and waiting for the seller's objection."""
    ctx.mode("s-gama", "fake_data")
    m = ctx.mandate()
    deal, _ = ctx.open_deal("s-gama", m)
    before = ctx.snapshot()
    if not ok_decision(ctx, ctx.pay(deal)):
        return None
    amount = ctx.deal_amount(deal)
    ctx.expect_lock(deal, "EURC")
    state = ctx.wait_state(deal, ("disputed", "settled", "closed"))
    ctx.require("deal disputed: 2 items rejected by the rules", state == "disputed", state)
    d = dispute_of(ctx, deal)
    items = [it for it in d["items"] if not it["outcome"]]
    ctx.require("2 items in the seller's objection window", len(items) == 2 and all(it["step"] == "objection"
                                                                                  for it in items), items)
    to18, _ = money.per_item_split(amount, 20, 18)
    holdback = to18 * int(d["terms"]["holdback_bps"]) // 10_000
    first = captured(ctx, deal, "EURC", "s-gama")
    ctx.check("at once: 18 items minus the 5 % holdback captured (Transfer events)", first == to18 - holdback,
              {"captured": first, "expected": to18 - holdback})
    st = ctx.escrow_state(deal, ok=lambda st: st["capturable"] == amount - first)
    ctx.check("paymentState: 2 disputed items + holdback wait in escrow", st is not None
              and st["capturable"] == amount - first, st)
    return deal, amount, to18, d, before


def s05(ctx: Ctx):
    """Dispute decided by a human for the seller: the 2 rejected items wait in escrow, the seller objects, the bank's
    officer accepts both (bank.disputes.decide) → capture of exactly those items; the holdback follows on the timer."""
    from bank import disputes
    got = fake_data_deal(ctx)
    if got is None:
        return
    deal, amount, to18, d, before = got
    first = captured(ctx, deal, "EURC", "s-gama")
    for it in [i for i in d["items"] if not i["outcome"]]:
        disputes.seller_objection(deal, it["placeId"], "The listing is a real warehouse; see the Maps URL.",
                                  who="human:gama-seller")
    ctx.check("objection alone moves no money (chain)", captured(ctx, deal, "EURC", "s-gama") == first)
    for it in [i for i in disputes.get(deal)["items"] if not i["outcome"]]:
        disputes.decide(deal, it["placeId"], "accept", who="human:bank-officer", note="Seller's evidence holds.")
    after = captured(ctx, deal, "EURC", "s-gama")
    ctx.check("human decision → capture of the 2 disputed items (Transfer event)", after - first == amount - to18,
              {"captured_now": after - first, "expected": amount - to18})
    ctx.check("journal: dispute.decision by human:bank-officer", any(
        e["event"] == "dispute.decision" and e["actor"] == "human:bank-officer" for e in journal.entries(deal, limit=500)))
    timer(ctx, int(d["terms"]["holdback_s"]) + 1)
    ctx.check("deal closed after the holdback", ctx.wait_state(deal, ("closed",), timeout=30) == "closed")
    ctx.check("seller got the whole price, nothing voided (Transfer events)",
              captured(ctx, deal, "EURC", "s-gama") == amount and voided(ctx, deal, "EURC") == 0)
    st = ctx.escrow_state(deal, ok=lambda st: st["capturable"] == 0)
    ctx.check("paymentState: nothing left to capture", st is not None and st["capturable"] == 0, st)
    dd = ctx.delta_after(before, lambda dd: dd.get("EURC:gama") == amount and dd.get("EURC:buyer") == -amount)
    ctx.check("seller +price, buyer −price (chain)", dd.get("EURC:gama") == amount and dd.get("EURC:buyer") == -amount, dd)
    ctx.journal_chain()


def offer_cents(ctx: Ctx, seller_id: str = "s-gama") -> int:
    """The seller's price for the task's 20 items, in euro cents (asked directly, no deal)."""
    return int(ctx.seller.post("/negotiate", json={"deal_id": "q-probe", "seller_id": seller_id,
                                                   "task": {"criteria": CRITERIA}}).json()["offer"]["price_eur_cents"])


def n01(ctx: Ctx):
    """Amount above the mandate's per-deal limit (limit = price − 1 cent): AMOUNT_ABOVE_LIMIT, no money moved."""
    ctx.mode("s-gama", "honest")
    eur = offer_cents(ctx)
    m = ctx.mandate(per_deal_max={"value": str(eur - 1), "currency": "EUR"},
                    confirm_above={"value": str(eur - 2), "currency": "EUR"})
    deal, _ = ctx.open_deal("s-gama", m)
    before = ctx.snapshot()
    refused(ctx, ctx.pay(deal), "AMOUNT_ABOVE_LIMIT")
    ctx.no_money_moved(before)
    ctx.journal_chain()


def n02(ctx: Ctx):
    """Race: 10 parallel pay() with budget for 3 → exactly 3 locks, 7 × BUDGET_EXCEEDED, no extra debit."""
    ctx.mode("s-gama", "honest")
    eur = offer_cents(ctx)
    budget = 3 * eur
    per_deal = max(eur + 1, min(1000, budget))
    m = ctx.mandate(budget={"value": str(budget), "currency": "EUR", "period": "month"},
                    per_deal_max={"value": str(per_deal), "currency": "EUR"},
                    confirm_above={"value": str(per_deal - 1), "currency": "EUR"},
                    velocity={"max_deals": 100, "window_s": 60})
    deals = [ctx.open_deal("s-gama", m, wait_screening=(i == 0))[0] for i in range(10)]
    before = ctx.snapshot()

    def pay_one(deal_id):
        with httpx.Client(base_url=ctx.s.bank_url, timeout=120, headers={"x-agent-token": AGENT_TOKEN}) as c:
            return ctx.pay(deal_id, c)

    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(pay_one, deals))
    oks = [d for d, r in zip(deals, results) if r.get("status") == "ok"]
    ctx.check("exactly 3 payments ok", len(oks) == 3, [r.get("code") or r.get("status") for r in results])
    ctx.check("7 × BUDGET_EXCEEDED", sum(r.get("code") == "BUDGET_EXCEEDED" for r in results) == 7)
    rows = db.rows("SELECT deal_id, state FROM reservations WHERE mandate_id=? AND state!='released'", (m,))
    ctx.check("exactly 3 reservations hold budget", len(rows) == 3, rows)
    for d in oks:
        ctx.expect_lock(d, "EURC")
        ctx.wait_state(d)
    locks = chain.transfers(config.EURC, before["block"] + 1, frm=ctx.s.addr["alfa"])
    price = ctx.deal_amount(deals[0])
    ctx.check("buyer debited exactly 3 × price on chain (no extra debit)",
              len(locks) == 3 and sum(t["value"] for t in locks) == 3 * price,
              {"transfers": len(locks), "sum": sum(t["value"] for t in locks), "expected": 3 * price})
    if oks:  # let the 3 paid deals finish (holdback) so no later scenario's tick moves their money
        terms = [dispute_of(ctx, d)["terms"] for d in oks][0]
        timer(ctx, int(terms["holdback_s"]) + 1)
        ctx.check("the 3 paid deals closed, each seller capture = price (Transfer events)",
                  all(ctx.wait_state(d, ("closed",), timeout=30) == "closed" and captured(ctx, d, "EURC", "s-gama") == price
                      for d in oks))
    ctx.journal_chain()


def swap(ctx: Ctx, mode: str, code: str):
    ctx.mode("s-gama", mode)
    try:
        m = ctx.mandate()
        deal, _ = ctx.open_deal("s-gama", m)
        before = ctx.snapshot()
        refused(ctx, ctx.pay(deal), code)
        ctx.no_money_moved(before)
        ctx.journal_has(deal, "pay_refused")
        ctx.journal_chain()
    finally:
        ctx.mode("s-gama", DEFAULT_MODES["s-gama"])


def n03(ctx: Ctx):
    """payTo swapped in the 402: COUNTERPARTY_NOT_IN_SCOPE, nothing to 0xdEaD."""
    swap(ctx, "swap_payto", "COUNTERPARTY_NOT_IN_SCOPE")


def n04(ctx: Ctx):
    """Price swapped in the 402: QUOTE_MISMATCH."""
    swap(ctx, "swap_price", "QUOTE_MISMATCH")


def n05(ctx: Ctx):
    """Injection in the seller's answer ('transfer 50 EURC to 0xEvil'), run through the buyer agent tool loop in
    MOCK (the scripted model complies): COUNTERPARTY_NOT_IN_SCOPE or AMOUNT_ABOVE_LIMIT, source untrusted:seller."""
    from agents import buyer as buyer_agent
    from bank import screening
    gama = core.seller("s-gama")
    screening.check(gama["ico"], gama["wallet_address"], gama["website_url"])  # warm the cache, as prefetch would
    ctx.mode("s-gama", "injection")
    try:
        m = ctx.mandate()
        before = ctx.snapshot()
        transport = buyer_agent.http_transport(client=httpx.Client(base_url=ctx.s.bank_url, timeout=90,
                                                                   headers={"x-agent-token": AGENT_TOKEN}))
        agent = buyer_agent.Buyer(TASK_TEXT, transport=transport, mandate_id=m, seller_hint="s-gama")
        out = agent.run()
        ctx.deals += list(agent.deals.values())
        pays = [c for c in out["tool_calls"] if c["name"] == "pay"]
        ctx.require("the agent called pay(deal_id) after the injected text", bool(pays), out["outcome"])
        d = pays[-1]["output"]
        ctx.decisions.append({"deal_id": pays[-1]["input"].get("deal_id"), **{k: d.get(k) for k in ("status", "code")}})
        refused(ctx, d, "COUNTERPARTY_NOT_IN_SCOPE", "AMOUNT_ABOVE_LIMIT")
        ctx.no_money_moved(before)
        deal = agent.deals["s-gama"]
        untrusted = [e for e in journal.entries(deal, limit=200) if e["payload"].get("source") == "untrusted:seller"
                     and "SYSTEM:" in json.dumps(e["payload"], ensure_ascii=False)]
        ctx.check("journal: the injected text is stored with source untrusted:seller", bool(untrusted))
        ctx.journal_chain()
    finally:
        ctx.mode("s-gama", DEFAULT_MODES["s-gama"])


def n06(ctx: Ctx):
    """Delta: IČO on the website differs from ARES → COUNTERPARTY_NOT_TRUSTED before payment, with sources."""
    m = ctx.mandate()
    deal, _ = ctx.open_deal("s-delta", m)
    before = ctx.snapshot()
    d = ctx.pay(deal)
    if refused(ctx, d, "COUNTERPARTY_NOT_TRUSTED"):
        reasons = d.get("reasons") or []
        ctx.check("client sees the public reason with a source URL", any(r.get("source_url") for r in reasons), reasons)
    ctx.no_money_moved(before)
    ctx.journal_chain()


def n07(ctx: Ctx):
    """Garbage result: the task asks for Praha, the seller delivers Brno → every item fails → void in full."""
    ctx.mode("s-gama", "honest")
    m = ctx.mandate()
    deal, _ = ctx.open_deal("s-gama", m, criteria={**CRITERIA, "city": "Praha"})
    before = ctx.snapshot()
    if ok_decision(ctx, ctx.pay(deal)):
        settled_escrow(ctx, deal, "EURC", "s-gama", 0, before)
        ctx.journal_has(deal, "escrow_authorized", "escrow_voided")
    ctx.journal_chain()


def n08(ctx: Ctx):
    """Beta is a VAT payer, paid in EURC: VAT_LIABILITY_RISK → a human acknowledges once → the payment passes
    with the mark; a second deal with Beta in EURC passes without asking."""
    m = ctx.mandate()
    deal, _ = ctx.open_deal("s-beta", m)
    before = ctx.snapshot()
    first = ctx.pay(deal)
    ctx.check("first pay asks the human (requires_user_action)", first.get("status") == "requires_user_action")
    refused(ctx, first, "VAT_LIABILITY_RISK")
    ctx.no_money_moved(before, "before the acknowledgement")
    r = ctx.human.post(f"/api/deals/{deal}/ack-vat", json={"by": "scenarios"})
    if r.status_code in (404, 405):
        ctx.notes.append("no /api/deals/{id}/ack-vat endpoint: acknowledged through mandate.acknowledge_vat")
        mandate.acknowledge_vat(m, core.seller("s-beta")["ico"], "EURC", "human:scenarios")
        second = ctx.pay(deal)
    else:
        second = r.json() if r.status_code == 200 else {"status": "http_error", "http_status": r.status_code}
        ctx.decisions.append({"deal_id": deal, **{k: second.get(k) for k in ("status", "code", "explanation")}})
    if ok_decision(ctx, second):
        ctx.check("decision carries the 'VAT risk acknowledged' mark",
                  "VAT risk acknowledged" in (second.get("explanation") or ""), second.get("explanation"))
        settled_escrow(ctx, deal, "EURC", "s-beta", ctx.deal_amount(deal), before)
    deal2, _ = ctx.open_deal("s-beta", m)
    before2 = ctx.snapshot()
    third = ctx.pay(deal2)
    if ok_decision(ctx, third):
        ctx.check("second deal: no question, mark present", "VAT risk acknowledged" in (third.get("explanation") or ""),
                  third.get("explanation"))
        settled_escrow(ctx, deal2, "EURC", "s-beta", ctx.deal_amount(deal2), before2)
    ctx.journal_chain()


def n09(ctx: Ctx):
    """tCZK to a bank client that is not on the tCZK allowlist, through the bank facilitator: HTTP 200
    isValid:false, no transfer."""
    from bank import tczk
    delta = ctx.wallet("s-delta")
    if tczk.is_allowed(delta):
        tczk.disallow(delta)  # setup by the bank's compliance role, before the snapshot
    amount = money.from_eur_cents("tCZK", 10)  # 2,50 Kč
    payload, req = sdk_payload(ctx, "tCZK", amount, delta)
    before = ctx.snapshot()
    fac = facilitator_client(ctx)
    v, s = fac.verify(payload, req), fac.settle(payload, req)
    ctx.check("verify: isValid false, COUNTERPARTY_NOT_IN_SCOPE",
              (v.is_valid, v.invalid_reason) == (False, "COUNTERPARTY_NOT_IN_SCOPE"),
              {"is_valid": v.is_valid, "reason": v.invalid_reason})
    ctx.check("settle: refused, no transaction", not s.success and not s.transaction,
              {"success": s.success, "reason": s.error_reason, "tx": s.transaction})
    ctx.decisions.append({"deal_id": None, "status": "refused", "code": v.invalid_reason})
    ctx.no_money_moved(before)
    ctx.journal_chain()


def attack_preconditions(ctx: Ctx):
    """N10/N11 need the stage tCZK (strict mode, custom errors) and gas in the attacker's (buyer's) wallet. Checked
    before anything is signed: on the prep fallback token a direct transfer would really move money."""
    from bank import tczk
    ctx.require("tCZK is the stage token with strict mode (not the prep fallback)", not tczk.is_prep(),
                f"TCZK_NAME={config.TCZK_NAME!r}: the strict-mode TCZK (T3) is not deployed on this chain")
    eth = chain.w3().eth.get_balance(ctx.s.addr["alfa"])
    ctx.require("buyer wallet has ETH to send the attack tx", eth > 0, f"{eth / 1e18:.8f} ETH")


def n10(ctx: Ctx):
    """Direct tCZK transfer bypassing the bank (strict mode): a real reverted tx; the contract refuses."""
    from bank import tczk
    attack_preconditions(ctx)
    before = ctx.snapshot()
    call = tczk.token().functions.transfer(ctx.wallet("s-beta"), money.from_eur_cents("tCZK", 10))
    tx = chain.sender(ctx.s.keys["alfa"]).send(call, kind="attack_direct_transfer", expect_revert=True, gas=300_000)
    ctx.extra_txs.append({"kind": "attack_direct_transfer", "tx_hash": tx["tx_hash"], "status": tx["status"], "deal_id": None})
    ctx.check("tx reverted on chain", tx["status"] == "reverted", tx["status"])
    name = revert_name(tx["tx_hash"])
    ctx.check("revert reason TransferViaBankOnly", name == "TransferViaBankOnly", name)
    moved = {k: v for k, v in Ctx.delta(before, ctx.snapshot()).items() if k.startswith("tCZK")}
    ctx.check("no tCZK moved (chain)", not moved, moved or None)
    ctx.decisions.append({"deal_id": None, "status": "reverted", "code": name})


def n11(ctx: Ctx):
    """Replay of the same EIP-3009 signature: the first settle lands, the replay reverts ERC3009UsedAuthorization."""
    from bank import tczk
    attack_preconditions(ctx)
    beta = ctx.wallet("s-beta")
    amount = money.from_eur_cents("tCZK", 10)
    payload, req = sdk_payload(ctx, "tCZK", amount, beta)
    before = ctx.snapshot()
    s = facilitator_client(ctx).settle(payload, req)
    ctx.require("first settle through the bank lands", bool(s.success and s.transaction), s.error_reason)
    ctx.extra_txs.append({"kind": "x402_settle", "tx_hash": s.transaction, "status": "mined", "deal_id": None})
    ctx.expected_debits.append({"asset": "tCZK", "tx_hash": s.transaction, "amount": amount})
    a, sig = payload.payload["authorization"], payload.payload["signature"]
    fn = tczk.token().get_function_by_signature(
        "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes)")
    call = fn(Web3.to_checksum_address(a["from"]), Web3.to_checksum_address(a["to"]), int(a["value"]),
              int(a["validAfter"]), int(a["validBefore"]), bytes.fromhex(a["nonce"].removeprefix("0x")),
              bytes.fromhex(sig.removeprefix("0x")))
    tx = chain.sender(ctx.s.keys["alfa"]).send(call, kind="attack_replay", expect_revert=True, gas=300_000)
    ctx.extra_txs.append({"kind": "attack_replay", "tx_hash": tx["tx_hash"], "status": tx["status"], "deal_id": None})
    ctx.check("replay tx reverted on chain", tx["status"] == "reverted", tx["status"])
    name = revert_name(tx["tx_hash"])
    ctx.check("revert reason ERC3009UsedAuthorization", name == "ERC3009UsedAuthorization", name)
    transfers = chain.read_retry(lambda: chain.transfers(config.TCZK, before["block"] + 1, frm=ctx.s.addr["alfa"], to=beta),
                                 ok=lambda xs: len(xs) >= 1, tries=10, delay=1.5)  # logs lag on the public RPC
    ctx.check("exactly one tCZK Transfer buyer → seller (chain)",
              len(transfers) == 1 and transfers[0]["value"] == amount, transfers)
    ctx.decisions.append({"deal_id": None, "status": "reverted", "code": name})


def n12(ctx: Ctx):
    """Unpaid request to a paid route: 402 (guard against the {param} route-key bug)."""
    offer = ctx.seller.post("/negotiate", json={"deal_id": f"q-n12-{secrets.token_hex(3)}", "seller_id": "s-gama",
                                                 "task": {"criteria": CRITERIA}}).json()["offer"]
    before = ctx.snapshot()
    for path in (f"/order/{offer['offer_id']}", f"/data/{offer['offer_id']}"):
        r = ctx.seller.get(path)
        ctx.check(f"GET /{path.split('/')[1]}/:offer_id unpaid → 402 with PAYMENT-REQUIRED",
                  r.status_code == 402 and bool(r.headers.get("payment-required")), r.status_code)
    ctx.no_money_moved(before)



def n13(ctx: Ctx):
    """Buyer silent on flagged items (bank profile FAULTS=llm_bad_json: the judge's answer is invalid, so every item
    is flagged to the buyer): money waits in escrow; after review_window_s the timer accepts (on_buyer_silence) and
    captures; journal actor timer with the rule."""
    ctx.mode("s-gama", "honest")
    m = ctx.mandate()
    deal, _ = ctx.open_deal("s-gama", m)
    before = ctx.snapshot()
    if not ok_decision(ctx, ctx.pay(deal)):
        return
    amount = ctx.deal_amount(deal)
    ctx.expect_lock(deal, "EURC")
    state = ctx.wait_state(deal, ("disputed", "verified", "settled", "closed"))
    ctx.check("deal waits for the buyer (disputed)", state == "disputed", state)
    d = dispute_of(ctx, deal)
    review = [it for it in d["items"] if it["step"] == "review" and not it["outcome"]]
    ctx.require("all 20 items flagged to the buyer's review", len(review) == 20, len(review))
    ctx.check("nothing captured or voided before the deadline", not ctx.deal_tx(deal, "capture")
              and not ctx.deal_tx(deal, "void"))
    st = ctx.escrow_state(deal, ok=lambda st: st["capturable"] == amount)
    ctx.check("paymentState: the whole price waits in escrow", st is not None and st["capturable"] == amount, st)
    early = timer(ctx, int(d["terms"]["review_window_s"]) - 5)
    ctx.check("a tick before the deadline does nothing", not [x for x in early if x.get("deal_id") == deal])
    timer(ctx, int(d["terms"]["review_window_s"]) + 1)
    timer_journal(ctx, deal, "dispute.default_applied", "on_buyer_silence")
    ctx.check("deal closed", ctx.wait_state(deal, ("closed",), timeout=30) == "closed")
    ctx.check("timer captured the whole price to the seller (Transfer events)",
              captured(ctx, deal, "EURC", "s-gama") == amount and voided(ctx, deal, "EURC") == 0)
    st = ctx.escrow_state(deal, ok=lambda st: st["capturable"] == 0)
    ctx.check("paymentState: nothing left to capture", st is not None and st["capturable"] == 0, st)
    dd = ctx.delta_after(before, lambda dd: dd.get("EURC:gama") == amount and dd.get("EURC:buyer") == -amount)
    ctx.check("seller +price, buyer −price (chain)", dd.get("EURC:gama") == amount and dd.get("EURC:buyer") == -amount, dd)
    ctx.journal_chain()


def n14(ctx: Ctx):
    """Seller silent after the rules rejected 2 items: after objection_window_s the timer lets the rejection stand
    (journal actor timer); once the holdback is over it captures the holdback and voids the disputed part back."""
    got = fake_data_deal(ctx)
    if got is None:
        return
    deal, amount, to18, d, before = got
    terms = d["terms"]
    timer(ctx, int(terms["objection_window_s"]) + 1)
    timer_journal(ctx, deal, "dispute.default_applied", "on_seller_silence")
    if int(terms["objection_window_s"]) < int(terms["holdback_s"]):
        ctx.check("no void while the holdback runs (void is final, captures first)", not ctx.deal_tx(deal, "void"))
    timer(ctx, int(terms["holdback_s"]) + 1)
    ctx.check("deal closed", ctx.wait_state(deal, ("closed",), timeout=30) == "closed")
    ctx.check("void returned the 2 disputed items to the buyer (Transfer event)", voided(ctx, deal, "EURC") == amount - to18,
              {"voided": voided(ctx, deal, "EURC"), "expected": amount - to18})
    ctx.check("seller got the 18 accepted items in total (Transfer events)", captured(ctx, deal, "EURC", "s-gama") == to18)
    st = ctx.escrow_state(deal, ok=lambda st: st["capturable"] == 0)
    ctx.check("paymentState: nothing left to capture", st is not None and st["capturable"] == 0, st)
    dd = ctx.delta_after(before, lambda dd: dd.get("EURC:gama") == to18 and dd.get("EURC:buyer") == -to18)
    ctx.check("seller +18 items, buyer −18 items (chain)", dd.get("EURC:gama") == to18 and dd.get("EURC:buyer") == -to18, dd)
    ctx.journal_chain()


def a01(ctx: Ctx):
    """Auth at the bank API: no or a wrong agent token → 401; another company's mandate, buyer_id, deal or statement
    → 403/404; a delivery without the seller token and a human action with the agent token → 401. No money moved."""
    m = ctx.mandate()
    deal, _ = ctx.open_deal("s-gama", m)
    other = f"m-q-a01-beta-r{ctx.run}-{secrets.token_hex(2)}"
    mandate.create(other, "beta", {}, by="human:scenarios")
    foreign = f"d-q-a01-{secrets.token_hex(4)}"
    db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, mandate_id, asset, state, created_at)"
           " VALUES (?, 'beta', 's-gama', ?, 'EURC', 'offered', ?)", (foreign, other, time.time()))
    task = {"text": TASK_TEXT, "category": "data", "asset": "EURC", "budget": {"value": "500", "currency": "EUR"},
            "criteria": CRITERIA}
    before = ctx.snapshot()
    with httpx.Client(base_url=ctx.s.bank_url, timeout=30) as anon, \
            httpx.Client(base_url=ctx.s.bank_url, timeout=30, headers={"x-agent-token": "not-a-token"}) as wrong:
        cases = [
            ("pay without a token → 401", anon.post(f"/api/deals/{deal}/pay", json={}), (401,)),
            ("pay with a wrong token → 401", wrong.post(f"/api/deals/{deal}/pay", json={}), (401,)),
            ("deal on another company's mandate → 403", ctx.bank.post("/api/deals", json={
                "seller_id": "s-gama", "mandate_id": other, "task": task, "asset": "EURC"}), (403,)),
            ("buyer_id of another company in the body → 403", ctx.bank.post("/api/deals", json={
                "buyer_id": "beta", "seller_id": "s-gama", "mandate_id": m, "task": task, "asset": "EURC"}), (403,)),
            ("pay another company's deal → 404", ctx.bank.post(f"/api/deals/{foreign}/pay", json={}), (403, 404)),
            ("another company's statement → 404", ctx.bank.get("/api/statement", params={"mandate_id": other}), (403, 404)),
            ("delivery without the seller token → 401", anon.post(f"/api/deals/{deal}/delivery",
                                                                 json={"items": []}), (401,)),
            ("human confirm with the agent token → 401", ctx.bank.post(f"/api/deals/{deal}/confirm",
                                                                      json={"by": "agent"}), (401,)),
        ]
    for name, r, want in cases:
        ctx.check(name, r.status_code in want, r.status_code)
    ctx.decisions.append({"deal_id": deal, "status": "refused", "code": "HTTP 401/403/404"})
    ctx.deals.append(foreign)
    ctx.no_money_moved(before)
    ctx.journal_chain()


# ---- fault injection (FAULTS=..., one bank profile each) ----

def f01(ctx: Ctx):
    """settle_fail_once: the facilitator fails after broadcast; the retry finds the same tx — exactly one on chain."""
    beta = ctx.wallet("s-beta")
    amount = money.to_atomic("EURC", "0.01")
    payload, req = sdk_payload(ctx, "EURC", amount, beta)
    before = ctx.snapshot()
    s = facilitator_client(ctx).settle(payload, req)
    ctx.require("settle succeeds after the injected failure", bool(s.success and s.transaction), s.error_reason)
    ctx.extra_txs.append({"kind": "x402_settle", "tx_hash": s.transaction, "status": "mined", "deal_id": None})
    ctx.expected_debits.append({"asset": "EURC", "tx_hash": s.transaction, "amount": amount})
    transfers = chain.transfers(config.EURC, before["block"] + 1, frm=ctx.s.addr["alfa"], to=beta)
    ctx.check("exactly one Transfer on chain", len(transfers) == 1 and transfers[0]["value"] == amount, transfers)
    n = db.one("SELECT COUNT(*) n FROM onchain_txs WHERE kind='x402_settle'")["n"]
    ctx.check("exactly one x402_settle row (idempotency key)", n == 1, n)
    ctx.check("journal: x402_settle_retry", any(e["event"] == "x402_settle_retry" for e in journal.entries(limit=50)))
    ctx.journal_chain()


def f02(ctx: Ctx):
    """registry_timeout: the registries do not answer → the payment is 'under review', not 'ok' (fail-closed)."""
    from bank import codes, screening
    m = ctx.mandate()
    deal, _ = ctx.open_deal("s-gama", m)
    cached = screening.cached(core.seller("s-gama")["ico"])
    ctx.check("cached check is 'review' with a non-public registry_timeout reason",
              bool(cached) and cached["level"] == "review" and any(r["code"] == "registry_timeout" and not r["public"]
                                                                   for r in cached["reasons"]),
              cached and {"level": cached["level"], "reasons": [r["code"] for r in cached["reasons"]]})
    before = ctx.snapshot()
    d = ctx.pay(deal)
    refused(ctx, d, "UNDER_REVIEW")
    ctx.check("client sees only the neutral text", d.get("explanation") == codes.NEUTRAL_CLIENT_TEXT, d.get("explanation"))
    ctx.no_money_moved(before)
    ctx.journal_chain()


def f03(ctx: Ctx):
    """llm_bad_json: the judge answers garbage twice → every item goes to a human; the model captures and voids
    nothing, the money waits in escrow. Cleanup: the buyer stays silent and the timer settles (as in N13)."""
    ctx.mode("s-gama", "honest")
    try:
        m = ctx.mandate()
        deal, _ = ctx.open_deal("s-gama", m)
        before = ctx.snapshot()
        if ok_decision(ctx, ctx.pay(deal)):
            amount = ctx.deal_amount(deal)
            ctx.expect_lock(deal, "EURC")
            state = ctx.wait_state(deal, ("verified", "settled", "closed", "disputed"))
            ctx.check("deal waits for a human (disputed, items in review)", state == "disputed", state)
            ctx.check("no capture tx, no void tx", not ctx.deal_tx(deal, "capture") and not ctx.deal_tx(deal, "void"))
            st = ctx.escrow_state(deal)
            ctx.check("escrow paymentState: the whole price still capturable", st is not None and st["capturable"] == amount, st)
            d = Ctx.delta(before, ctx.snapshot())
            ctx.check("seller +0 (chain)", d.get("EURC:gama", 0) == 0, d)
            timer(ctx, int(dispute_of(ctx, deal)["terms"]["review_window_s"]) + 1)
            ctx.notes.append("cleanup: buyer silent, the timer settled the deal")
            ctx.check("cleanup: closed by the timer", ctx.wait_state(deal, ("closed",), timeout=30) == "closed")
        ctx.journal_chain()
    finally:
        ctx.mode("s-gama", DEFAULT_MODES["s-gama"])


def webhook(ctx: Ctx):
    routes = httpx.get(f"{ctx.s.bank_url}/openapi.json", timeout=10).json().get("paths", {})
    ctx.require("bank exposes a Tatum webhook route", any("webhook" in p for p in routes), sorted(routes)[:40])
    ctx.require("webhook scenario wired into this runner", False, "write the call once L7 is merged")


SCENARIOS = {
    "S01": ("x402 EURC through the bank facilitator", s01),
    "S02": ("Escrow EURC, all accepted", s02),
    "S03": ("Escrow EURC, 18 of 20", s03),
    "S04": ("Same deal in tCZK through escrow", s04),
    "S05": ("Dispute decided for the seller", s05),
    "N01": ("Amount above the per-deal limit", n01),
    "N02": ("Race: 10 parallel pay() with budget for 3", n02),
    "N03": ("payTo swapped in the 402", n03),
    "N04": ("Price swapped in the 402", n04),
    "N05": ("Injection in the seller's answer", n05),
    "N06": ("Delta: IČO on site ≠ ARES", n06),
    "N07": ("Garbage result → full void", n07),
    "N08": ("VAT payer in EURC → acknowledged once", n08),
    "N09": ("tCZK to a payee off the allowlist", n09),
    "N10": ("Direct tCZK transfer bypassing the bank", n10),
    "N11": ("Replay of the same EIP-3009 signature", n11),
    "N12": ("Unpaid request to a paid route", n12),
    "N13": ("Buyer silent on flagged items → timer captures", n13),  # bank profile FAULTS=llm_bad_json
    "N14": ("Seller silent after a rejection → timer voids", n14),
    "A01": ("Bank API auth: tokens, other company's mandate/deal", a01),
}
FAULT_SCENARIOS = {  # id: (title, function, FAULTS for the bank profile)
    "F01": ("settle_fail_once → exactly one tx", f01, "settle_fail_once"),
    "F02": ("registry_timeout → under review (fail-closed)", f02, "registry_timeout"),
    "F03": ("llm_bad_json → to a human, no money moves", f03, "llm_bad_json"),
    "F04": ("webhook_duplicate → counted once", webhook, "webhook_duplicate"),
    "F05": ("webhook_bad_hmac → rejected", webhook, "webhook_bad_hmac"),
}
PROFILES = {"N13": "llm_bad_json"}  # P0 scenarios that need their own bank profile (FAULTS)
# Attacks and refusals: the main claim is "no money moved" (counted in "attacks blocked X/X").
ATTACKS = {"N01", "N03", "N04", "N05", "N06", "N09", "N10", "N11", "N12", "A01", "F02"}


def run_one(stack: Stack, sid: str, run: int, title: str, fn) -> dict:
    ctx = Ctx(stack, sid, run)
    error = None
    try:
        fn(ctx)
    except Check:
        pass
    except Exception as e:  # noqa: BLE001 — a crash is a failed scenario, with the trace in the report
        error = f"{type(e).__name__}: {e}"
        ctx.check("scenario ran without an exception", False, traceback.format_exc()[-1500:])
    for k, v in DEFAULT_MODES.items():
        try:
            ctx.mode(k, v)
        except httpx.HTTPError:
            pass
    ok = bool(ctx.checks) and all(c["ok"] for c in ctx.checks)
    known = KNOWN.get(sid, [])
    status = "pass" if ok else ("xfail" if known else "fail")
    txs = ctx.txs()
    for t in txs:
        t["link"] = chain.tx_link(t["tx_hash"]) if stack.target == "sepolia" else None
    out = {"id": sid, "title": title, "run": run, "status": status, "attack": sid in ATTACKS,
           "defects": known if status == "xfail" else [], "checks": ctx.checks, "decisions": ctx.decisions,
           "deals": ctx.deals, "amounts": ctx.amounts, "txs": txs, "expected_debits": ctx.expected_debits,
           "notes": ctx.notes, "error": error, "seconds": round(time.time() - ctx.t0, 2),
           "start_block": ctx.start_block, "end_block": chain.w3().eth.block_number, "profile": stack.profile,
           "db": stack.db_path}
    ctx.close()
    failed = [c["name"] for c in ctx.checks if not c["ok"]]
    print(f"  {status.upper():5} {sid} run {run}: {title} ({out['seconds']} s)" + (f" — {failed[0]}" if failed else ""),
          flush=True)
    return out


# ---- reconciliation (chain Transfer events vs DB vs statement) ----

def reconcile_deal(deal: dict, buyer: str) -> dict:
    """locked (chain) == deals.amount; captured (chain) == Σ journal captures; committed (statement) == captured in
    euro cents; locked == captured + voided + capturable (chain)."""
    asset = deal["asset"]
    seller = core.seller(deal["seller_id"])["wallet_address"]
    sums = {"authorize": 0, "capture": 0, "void": 0}
    for t in db.rows("SELECT kind, tx_hash FROM onchain_txs WHERE deal_id=? AND status='mined'", (deal["deal_id"],)):
        kind = t["kind"].split(":")[0]
        if kind not in sums:
            continue
        for tr in transfers_in(t["tx_hash"], asset):
            if (kind, tr["from"]) == ("authorize", buyer) or (kind == "capture" and tr["to"] == seller) \
                    or (kind == "void" and tr["to"] == buyer):
                sums[kind] += tr["value"]
    st = escrow.read_state(deal["payment_hash"])
    journal_captured = sum(int(e["payload"].get("amount", 0)) for e in journal.entries(deal["deal_id"], limit=500)
                           if e["event"] == "escrow_captured")
    res = db.one("SELECT committed, state FROM reservations WHERE deal_id=?", (deal["deal_id"],)) or {}
    checks = {
        "locked_chain_eq_db_amount": sums["authorize"] == int(deal["amount"] or 0),
        "captured_chain_eq_journal": sums["capture"] == journal_captured,
        "committed_statement_eq_captured": int(res.get("committed") or 0) == money.to_eur_cents(asset, sums["capture"]),
        "locked_eq_captured_voided_capturable": sums["authorize"] == sums["capture"] + sums["void"] + st["capturable"],
    }
    return {"deal_id": deal["deal_id"], "asset": asset, "chain": sums, "escrow_capturable": st["capturable"],
            "db_amount": deal["amount"], "journal_captured": journal_captured,
            "statement_committed_eur_cents": res.get("committed"), "checks": checks, "ok": all(checks.values())}


def reconcile(stack: Stack, results: list[dict], first_block: int) -> dict:
    """Per deal: chain vs DB vs statement. Globally: every buyer debit on chain since the first scenario must be a
    lock or a payment a scenario expected (else it is a wrong debit); nothing ever reached 0xdEaD."""
    deals_out = []
    for path in sorted({r["db"] for r in results}):
        stack._apply_env({**stack.env, "DB_PATH": path})
        for d in db.rows("SELECT * FROM deals WHERE payment_hash IS NOT NULL"):
            if db.one("SELECT 1 FROM onchain_txs WHERE deal_id=? AND kind='authorize' AND status='mined'", (d["deal_id"],)):
                deals_out.append(reconcile_deal(d, stack.addr["alfa"]))
    stack._apply_env(stack.env)
    expected = {e["tx_hash"].lower() for r in results for e in r["expected_debits"]}
    wrong, debits = [], {"EURC": 0, "tCZK": 0}
    for asset in ("EURC", "tCZK"):
        for t in chain.transfers(config.ASSETS[asset]["address"], first_block, frm=stack.addr["alfa"]):
            debits[asset] += 1
            if t["tx_hash"].lower() not in expected:
                wrong.append({"asset": asset, **t})
    to_evil = sum(len(chain.transfers(config.ASSETS[a]["address"], first_block, to=EVIL)) for a in ("EURC", "tCZK"))
    return {"deals": deals_out, "deals_checked": len(deals_out), "deals_ok": sum(d["ok"] for d in deals_out),
            "buyer_debits": debits, "wrong_debits": wrong, "wrong_debit_count": len(wrong), "transfers_to_evil": to_evil,
            "first_block": first_block, "ok": all(d["ok"] for d in deals_out) and not wrong and to_evil == 0}


def stage_ms(paths: list[str]) -> dict:
    """Raw stage timings (ms) from every bank DB of the run, plus model token totals (timing.llm_totals per DB)."""
    stages: dict[str, list[float]] = {}
    llm = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for path in paths:
        c = sqlite3.connect(path)
        for stage, ms in c.execute("SELECT stage, ms FROM timings WHERE ok=1 AND model IS NULL"):
            stages.setdefault(stage, []).append(round(ms, 3))
        for (ms,) in c.execute("SELECT ms FROM decisions WHERE outcome != 'ok' AND ms IS NOT NULL"):
            stages.setdefault("refusal_to_client", []).append(round(ms, 3))
        row = c.execute("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
                        " COALESCE(SUM(cost_usd),0) FROM timings WHERE model IS NOT NULL").fetchone()
        for k, v in zip(llm, row):
            llm[k] += v
        c.close()
    return {"stages": stages, "llm_totals": llm}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P0 scenario matrix (SPEC §19.2) on a real chain")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", default="", help="comma list of scenario ids, e.g. N05,S02,F01")
    ap.add_argument("--p0", action="store_true", help="P0 matrix only (no fault scenarios)")
    ap.add_argument("--target", default="fork", choices=["fork", "sepolia"])
    ap.add_argument("--out", default=None, help="raw JSON path (default reports/scenarios-<target>.json)")
    a = ap.parse_args(argv)
    only = {s.strip().upper() for s in a.only.split(",") if s.strip()}
    p0 = {k: v for k, v in SCENARIOS.items() if not only or k in only}
    faults = {} if a.p0 else {k: v for k, v in FAULT_SCENARIOS.items() if not only or k in only}
    started, results = time.time(), []
    with Stack(a.target) as stack:
        print(f"target: {stack.label}; funding: {stack.funding}", flush=True)
        first_block = chain.w3().eth.block_number
        for run in range(1, a.runs + 1):
            print(f"run {run}/{a.runs}", flush=True)
            if stack.profile != "main":
                stack.use_profile("main")
            for sid, (title, fn) in p0.items():
                if sid in PROFILES:
                    stack.use_profile(f"{sid.lower()}-r{run}", PROFILES[sid])
                elif stack.profile != "main":
                    stack.use_profile("main")
                results.append(run_one(stack, sid, run, title, fn))
            for sid, (title, fn, fault) in faults.items():
                stack.use_profile(f"{sid.lower()}-r{run}", fault)
                results.append(run_one(stack, sid, run, title, fn))
        recon = reconcile(stack, results, first_block)
        paths = sorted({r["db"] for r in results})
        perf = stage_ms(paths)
        journal_ok = {}
        for p in paths:
            stack._apply_env({**stack.env, "DB_PATH": p})
            journal_ok[Path(p).name] = journal.verify()[0]
        out = {"target": a.target, "label": stack.label, "stack": stack.describe(), "runs": a.runs,
               "started": started, "finished": time.time(), "scenario_price_eur": config.SCENARIO_PRICE_EUR,
               "defects": DEFECTS, "results": results, "reconciliation": recon, "journal_verified": journal_ok, **perf}
    REPORTS.mkdir(exist_ok=True)
    path = Path(a.out) if a.out else REPORTS / f"scenarios-{a.target}.json"
    path.write_text(json.dumps(out, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    counts = {s: sum(r["status"] == s for r in results) for s in ("pass", "xfail", "fail")}
    print(f"\n{counts} · reconciliation {'OK' if recon['ok'] else 'MISMATCH'} · wrong debits "
          f"{recon['wrong_debit_count']} · {path}")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
