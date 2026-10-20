# What a deal cost on chain and what the bank earned on it (MODEL_V2 §10, the "half a cent vs the fee" panel).
# Gas: onchain_txs.gas_used of the deal's txs (authorize, captures, void, C fee txs) plus the ramp's mint/burn when a
# ramp_ops table exists. The simulated chain has no receipts: typical gas per kind, labelled as such.
# Price: today's Base mainnet eth_gasPrice, read in a background thread (2 s timeout, 10-min cache) so a page never
# waits; until the first answer, and with no network, BASE_MAINNET_GAS_PRICE_WEI. ETH -> EUR at ETH_EUR_DEMO.
# Fee: what the deal's captures sent to the bank's clearing address (escrow_captured.fee, MODEL_V2 §8.1); a deal with
# nothing captured yet shows its tariff, a T0 deal the proposed monthly 0.2 % (not charged on testnet).
# Read-only: nothing here moves money.
import threading
import time
from datetime import datetime
from decimal import Decimal

import httpx

from bank import config, db, journal, money

TYPICAL_GAS = {"authorize": 150_000, "capture": 71_000, "void": 64_000, "tczk_mint": 50_000, "tczk_burn": 50_000,
               "transfer": 50_000}
OTHER_GAS = 60_000  # a tx kind with no typical value above
T0_MONTHLY_BPS = 20  # §10: T0 pays 0.2 % a month from the seller's account, nothing at capture
CACHE_S, RETRY_S, TIMEOUT_S = 600, 60, 2.0

_price = {"wei": None, "at": None, "tried": 0.0, "busy": False}
_lock = threading.Lock()


# ---- gas price: never on the request path ----

def fetch_gas_price(rpc: str) -> int:
    r = httpx.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []}, timeout=TIMEOUT_S)
    wei = int(r.raise_for_status().json()["result"], 16)
    if not 0 < wei < 10**12:  # above 1000 gwei is a broken answer, not a Base price
        raise ValueError(f"implausible gas price {wei}")
    return wei


def _refresh(rpc: str):
    try:
        wei = fetch_gas_price(rpc)
        with _lock:
            _price.update(wei=wei, at=time.time())
    except Exception:  # noqa: BLE001 — no network at the venue: the last price or the fallback stays
        pass
    finally:
        with _lock:
            _price["busy"] = False


def gas_price() -> dict:
    """{wei, live, at} at once: the last Base mainnet price read, else the .env fallback. A price older than 10 min
    starts one background read (a failed read is retried after a minute); the caller never waits for it."""
    now = time.time()
    with _lock:
        stale = _price["at"] is None or now - _price["at"] >= CACHE_S
        if stale and config.BASE_MAINNET_RPC and not _price["busy"] and now - _price["tried"] >= RETRY_S:
            _price.update(busy=True, tried=now)
            threading.Thread(target=_refresh, args=(config.BASE_MAINNET_RPC,), daemon=True).start()
        if _price["wei"]:
            return {"wei": _price["wei"], "live": True, "at": _price["at"]}
    return {"wei": config.BASE_MAINNET_GAS_PRICE_WEI, "live": False, "at": None}


def cost_cents(gas: int, wei: int) -> Decimal:
    """gas · price in euro cents: wei -> ETH (1e-18) -> € (ETH_EUR_DEMO) -> cents (100)."""
    return Decimal(int(gas) * int(wei)) * config.ETH_EUR_DEMO / Decimal(10**16)


# ---- gas of a deal ----

def _typical(kind: str) -> int:
    return TYPICAL_GAS.get((kind or "").removeprefix("fee_"), OTHER_GAS)


def _gas(row: dict, kind: str) -> dict | None:
    """Receipt gas of a landed tx; typical gas for a simulated one; None for a dead or still unconfirmed tx."""
    if row.get("gas_used"):
        return {"tx_hash": row["tx_hash"], "kind": kind, "gas": int(row["gas_used"]), "typical": False}
    if row.get("status") == "simulated":
        return {"tx_hash": row["tx_hash"], "kind": kind, "gas": _typical(kind), "typical": True}
    return None


def _ramp_ops(deal_id: str) -> list[dict]:
    """The ramp's mint/burn for this deal (payout to the seller's account), when that table exists in this build."""
    if not db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ramp_ops'"):
        return []
    cols = {r["name"] for r in db.rows("PRAGMA table_info(ramp_ops)")}
    if not {"deal_id", "tx_hash"} <= cols:
        return []
    ops = db.rows("SELECT * FROM ramp_ops WHERE deal_id=? AND tx_hash IS NOT NULL", (deal_id,))
    return [op for op in ops if op.get("state") != "failed"]


def deal_txs(deal_id: str) -> list[dict]:
    """[{tx_hash, kind, gas, typical}] for every landed tx of the deal, ramp mint/burn included, each once."""
    out, seen = [], set()
    for r in db.rows("SELECT tx_hash, kind, status, gas_used FROM onchain_txs WHERE deal_id=? ORDER BY created_at",
                     (deal_id,)):
        g = _gas(r, r["kind"])
        if g:
            out.append(g)
            seen.add(r["tx_hash"].lower())
    for op in _ramp_ops(deal_id):
        if op["tx_hash"].lower() in seen:
            continue
        kind = "tczk_mint" if op.get("direction") == "deposit" else "tczk_burn"
        row = db.one("SELECT tx_hash, status, gas_used FROM onchain_txs WHERE tx_hash=?", (op["tx_hash"],)) \
            or {"tx_hash": op["tx_hash"], "status": "simulated"}  # not in our tx table: count it at typical gas
        g = _gas(row, kind)
        if g:
            out.append(g)
            seen.add(op["tx_hash"].lower())
    return out


# ---- the panel ----

def fmt_cents(c) -> str:
    """'0,53 ¢', '6,00 ¢', '1,20 €' (Czech decimal comma, like money.fmt)."""
    c = Decimal(c)
    if abs(c) >= 100:
        return f"{c / 100:.2f} €".replace(".", ",")
    if 0 < abs(c) < Decimal("0.01"):
        return "< 0,01 ¢"
    return f"{c:.2f} ¢".replace(".", ",")


def _pct(bps: int) -> str:
    return f"{bps / 100:g} %".replace(".", ",")


def _fee(d: dict, moved: bool, sim: bool) -> dict:
    """The bank's fee on the deal: captured on chain, expected at capture, proposed (not charged) or none."""
    terms = db.loads(d.get("terms_json")) or {}
    ev = journal.entries(d["deal_id"], limit=1000)
    caps = [e["payload"] for e in ev if e["event"] == "escrow_captured"]
    fee, gross = sum(int(p.get("fee") or 0) for p in caps), sum(int(p.get("amount") or 0) for p in caps)
    voided = any(e["event"] == "escrow_voided" for e in ev)
    bps = int(terms.get("fee_bps") or 0)
    if fee:
        open_ = gross < int(d["amount"] or 0) and not voided
        where = "on the simulated chain" if sim else "on testnet"
        return {"atomic": fee, "source": "captured",
                "note": f"{_pct(bps)} captured {where} to the bank's clearing address" + (" so far" if open_ else "")}
    if not moved or not terms:
        return {"atomic": 0, "source": "none", "note": "no transaction, no fee: no money moved"}
    if voided and not caps:
        return {"atomic": 0, "source": "none", "note": "everything went back to the buyer: no capture, no fee"}
    base = gross or int(d["amount"] or 0)
    if bps and not caps:
        return {"atomic": base * bps // 10_000, "source": "pending",
                "note": f"at the deal's tariff {_pct(bps)}, taken at capture — not captured yet"}
    if terms.get("tier") == "T0":
        return {"atomic": base * T0_MONTHLY_BPS // 10_000, "source": "proposed",
                "note": f"T0: {_pct(T0_MONTHLY_BPS)} billed monthly from the seller's account (proposed) — "
                        "not charged on testnet"}
    bps = config.TARIFF_BPS.get((terms.get("tier"), terms.get("rule")), 0)
    return {"atomic": base * bps // 10_000, "source": "proposed",
            "note": f"at the proposed tariff {_pct(bps)} — not charged on testnet"}


def deal(deal_id: str, price: dict | None = None) -> dict | None:
    """Transactions, gas, network cost at today's Base mainnet gas price, the bank's fee and the margin of a deal."""
    d = db.one("SELECT deal_id, asset, amount, terms_json FROM deals WHERE deal_id=?", (deal_id,))
    if d is None:
        return None
    asset = d["asset"] or "EURC"
    txs = deal_txs(deal_id)
    gas = sum(t["gas"] for t in txs)
    typical = any(t["typical"] for t in txs)
    price = price or gas_price()
    cost = cost_cents(gas, price["wei"])
    fee = _fee(d, bool(txs), typical)
    fee_cents = Decimal(fee["atomic"]) / money.from_eur_cents(asset, 1)
    margin = (fee_cents - cost) / fee_cents * 100 if fee_cents > 0 else None
    kinds: dict[str, int] = {}
    for t in txs:
        kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
    read = f"read {datetime.fromtimestamp(price['at']).strftime('%H:%M')}" if price["live"] else "fallback constant"
    caption = (f"{'Simulated chain, typical gas' if typical else 'Testnet deal'}; cost estimated at today's Base "
               f"mainnet gas price ({price['wei'] / 1e9:.4g} gwei, {read}; ETH at {config.ETH_EUR_DEMO} €; "
               "L2 execution gas, the L1 data fee is not included).")
    if fee["source"] == "proposed":
        caption += " Fee at the proposed tariff — not charged on testnet."
    return {"deal_id": deal_id, "asset": asset, "tx_count": len(txs), "gas": gas, "typical": typical,
            "kinds": kinds, "price": price, "cost_cents": cost, "fee_atomic": fee["atomic"], "fee_cents": fee_cents,
            "fee_source": fee["source"], "margin_pct": margin,
            "amount_cents": money.to_eur_cents(asset, int(d["amount"] or 0)),
            "gas_text": f"{gas:,}".replace(",", " "), "cost_text": fmt_cents(cost), "fee_text": fmt_cents(fee_cents),
            "fee_native": money.fmt(asset, fee["atomic"]) if asset != "EURC" and fee["atomic"] else "",
            "fee_note": fee["note"], "margin_text": f"{margin:.0f} %" if margin is not None else "—",
            "caption": caption}


def summary(deal_rows: list[dict]) -> dict:
    """Unit economics over deals (the statement): average network cost per deal that sent txs, the fees captured,
    the margin, and how many deals were refused before any tx (cost nothing)."""
    price = gas_price()
    per = [e for e in (deal(r["deal_id"], price) for r in deal_rows) if e]
    moved = [e for e in per if e["tx_count"]]
    cost = sum((e["cost_cents"] for e in moved), Decimal(0))
    fees = sum((e["fee_cents"] for e in per if e["fee_source"] == "captured"), Decimal(0))
    avg = cost / len(moved) if moved else Decimal(0)
    margin = (fees - cost) / fees * 100 if fees > 0 else None
    return {"deals": len(moved), "free": len(per) - len(moved), "txs": sum(e["tx_count"] for e in moved),
            "cost_cents": cost, "avg_cost_cents": avg, "fees_cents": fees, "margin_pct": margin,
            "typical": any(e["typical"] for e in moved), "price": price,
            "avg_text": fmt_cents(avg), "cost_text": fmt_cents(cost), "fees_text": fmt_cents(fees),
            "margin_text": f"{margin:.0f} %" if margin is not None else "—"}
