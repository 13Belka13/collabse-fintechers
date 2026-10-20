# Ramp tCZK <-> CZK (P1). tCZK is the same crowns of the same bank, written on chain: a deposit debits the client's
# account and mints tCZK to its wallet, a withdrawal burns tCZK and credits the account; no rate, no fee. A payout is
# the withdrawal the bank makes by itself after an escrow capture to a seller with payout_mode='account': the seller
# gets crowns on its account and never has to touch the token. Core accounts are simulated (table accounts, haléře).
# Order protects the bank: debit before mint, burn before credit. One ref moves money once: the op is written down
# before the chain call, and mint/burn reuse the idem key tczk:mint|burn:<ref> (the same ref -> the same tx).
# A failed call is judged by its stored tx, not by the exception: never sent, dropped or reverted -> nothing moved
# (a deposit gets its debit back); signed with no receipt yet -> 'pending', a retry resends the same tx; mined -> done.
# Prototype limit: the bank burns without the holder's signature (in production: the client's instruction). The prep
# fallback token has no burn with a bank ref: the ramp is off there and the UI says so.
import secrets
import time

from bank import config, core, db, journal, money, tczk

EVENTS = {"deposit": "ramp_deposit", "withdraw": "ramp_withdraw", "payout": "payout_to_account"}
PAYOUT_BURN_GAS = 120_000  # a payout burns right after the capture: no gas estimate on a node that may lag behind it


class RampError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def off_reason() -> str | None:
    """Why deposits, withdrawals and payouts are off, or None when the ramp works."""
    if not config.TCZK:
        return "tCZK is not configured"
    if tczk.is_prep():
        return "the fallback token has no burn with a bank reference, so the bank cannot pay out to accounts"
    return None


def mask(company: dict | None) -> str:
    """'CZ…9718': enough to recognise the account, never the number."""
    number = (company or {}).get("iban") or (company or {}).get("account_number") or ""
    return f"{number[:2]}…{number[-4:]}" if len(number) > 6 else "—"


def account(company_id: str) -> dict | None:
    return db.one("SELECT * FROM accounts WHERE company_id=?", (company_id,))


def accounts() -> list[dict]:
    rows = db.rows("SELECT a.*, c.name, c.iban FROM accounts a JOIN companies c ON c.id=a.company_id ORDER BY c.id")
    return [{**r, "masked": mask(r)} for r in rows]


def op(ref: str) -> dict | None:
    return db.one("SELECT * FROM ramp_ops WHERE ref=?", (ref,))


def ops(deal_id: str | None = None, limit: int = 50) -> list[dict]:
    if deal_id:
        return db.rows("SELECT * FROM ramp_ops WHERE deal_id=? ORDER BY created_at", (deal_id,))
    return db.rows("SELECT r.*, c.name FROM ramp_ops r LEFT JOIN companies c ON c.id=r.company_id"
                   " WHERE r.direction!='initial' ORDER BY r.created_at DESC LIMIT ?", (limit,))


def _client(company_id: str) -> dict:
    reason = off_reason()
    if reason:
        raise RampError(409, f"Ramp is off: {reason}.")
    co = core.company(company_id)
    if co is None or account(company_id) is None:
        raise RampError(404, f"{company_id} has no account at the bank")
    if not co.get("wallet_address"):
        raise RampError(409, f"{company_id} has no wallet bound to its account")
    return co


def _positive(amount: int) -> int:
    amount = int(amount)
    if amount <= 0:
        raise RampError(400, "the amount must be positive")
    return amount


def _tx_state(kind: str, ref: str) -> tuple[str, str | None]:
    """What the mint/burn under this ref did on chain: ('mined'|'pending'|'none', tx_hash)."""
    row = db.one("SELECT tx_hash, status FROM onchain_txs WHERE idem_key=?", (f"tczk:{kind}:{ref}",))
    if row is None or row["status"] in ("reverted", "failed"):
        return "none", row and row["tx_hash"]
    return ("mined" if row["status"] in ("mined", "simulated") else "pending"), row["tx_hash"]


def _chain(kind: str, o: dict, wallet: str) -> tuple[str, str | None, str | None]:
    """mint|burn of the op under its ref's idem key -> (done | pending | failed, tx_hash, error)."""
    ref = o["ref"]
    try:
        if kind == "mint":
            tx = tczk.mint(wallet, o["amount"], ref)
        else:  # a payout burns right after the capture: fixed gas, and the tx is listed with the deal
            payout = o["direction"] == "payout"
            tx = tczk.burn(wallet, o["amount"], ref, gas=PAYOUT_BURN_GAS if payout else None, deal_id=o["deal_id"])
        return "done", tx["tx_hash"], None
    except Exception as e:  # noqa: BLE001 — judged by the stored tx below, never by the exception type
        state, h = _tx_state(kind, ref)
        return {"mined": "done", "pending": "pending", "none": "failed"}[state], h, f"{type(e).__name__}: {str(e)[:160]}"


def _insert(c, ref: str, company_id: str, direction: str, amount: int, deal_id: str | None = None):
    now = time.time()
    c.execute("INSERT INTO ramp_ops (ref, company_id, direction, amount, deal_id, state, created_at, updated_at)"
              " VALUES (?,?,?,?,?,'pending',?,?)", (ref, company_id, direction, amount, deal_id, now, now))


def _book(o: dict, co: dict, state: str, tx_hash: str | None, error: str | None, by: str) -> dict:
    """Close a pending op once (compare-and-set on state): money on the account follows what the chain did."""
    with db.immediate() as c:
        cur = c.execute("SELECT state FROM ramp_ops WHERE ref=?", (o["ref"],)).fetchone()
        if cur is None or cur["state"] != "pending":
            return op(o["ref"])  # another call booked it already
        if o["direction"] == "deposit" and state == "failed":  # nothing was minted: the debit goes back
            c.execute("UPDATE accounts SET balance=balance+? WHERE company_id=?", (o["amount"], o["company_id"]))
        if o["direction"] != "deposit" and state == "done":  # burnt on chain: only now the crowns reach the account
            c.execute("UPDATE accounts SET balance=balance+? WHERE company_id=?", (o["amount"], o["company_id"]))
        c.execute("UPDATE ramp_ops SET state=?, tx_hash=COALESCE(?, tx_hash), updated_at=? WHERE ref=?",
                  (state, tx_hash, time.time(), o["ref"]))
        if state == "done":
            event = EVENTS[o["direction"]]
        else:
            event = "payout_failed" if o["direction"] == "payout" else f"ramp_{state}"
        journal.append(by, event, {"ref": o["ref"], "direction": o["direction"], "state": state,
                                   "amount": o["amount"], "amount_text": money.fmt("tCZK", o["amount"]),
                                   "account": mask(co), "tx_hash": tx_hash, "error": error}, deal_id=o["deal_id"])
    return op(o["ref"])


def _finish(o: dict, by: str) -> dict:
    co = core.company(o["company_id"])
    state, h, err = _chain("mint" if o["direction"] == "deposit" else "burn", o, co["wallet_address"])
    return _book(o, co, state, h, err, by)


def deposit(company_id: str, amount: int, by: str, ref: str | None = None) -> dict:
    """Account -> wallet: debit first, then mint outside the DB transaction. A mint that provably did not happen gives
    the debit back; the same ref again never debits or mints twice."""
    _client(company_id)
    amount, ref = _positive(amount), ref or f"dep-{secrets.token_hex(6)}"
    with db.immediate() as c:
        prior = op(ref)
        if prior is None:
            acc = account(company_id)
            if acc["balance"] < amount:
                raise RampError(409, f"not enough on the account: {money.fmt('tCZK', acc['balance'])}")
            c.execute("UPDATE accounts SET balance=balance-? WHERE company_id=?", (amount, company_id))
            _insert(c, ref, company_id, "deposit", amount)
    if prior is not None and prior["state"] != "pending":
        return prior
    return _finish(op(ref), by)


def withdraw(company_id: str, amount: int, by: str, ref: str | None = None, deal_id: str | None = None,
             direction: str = "withdraw") -> dict:
    """Wallet -> account: burn first, credit only after the burn's receipt. A failed burn leaves the account as it was;
    the same ref again never burns or credits twice."""
    _client(company_id)
    amount, ref = _positive(amount), ref or f"wd-{secrets.token_hex(6)}"
    with db.immediate() as c:
        prior = op(ref)
        if prior is None:
            _insert(c, ref, company_id, direction, amount, deal_id)
    if prior is not None and prior["state"] != "pending":
        return prior
    return _finish(op(ref), by)


def payout(deal_id: str, amount: int, n: int, by: str = "bank") -> dict | None:
    """After the deal's n-th capture: `amount` (what reached the seller, net of the bank fee) goes from the seller's
    wallet to its account. Only for tCZK deals to a seller with payout_mode='account'. Never raises: the capture
    stands either way; a failure is journalled as payout_failed and retried from the deal page."""
    try:
        deal = db.one("SELECT asset, seller_id FROM deals WHERE deal_id=?", (deal_id,))
        if deal is None or deal["asset"] != "tCZK" or int(amount) <= 0 or off_reason():
            return None
        seller = core.seller(deal["seller_id"])
        acc = seller and account(seller["company_id"])
        if not acc or acc["payout_mode"] != "account":
            return None
        return withdraw(seller["company_id"], amount, by, ref=f"payout:{deal_id}:{n}", deal_id=deal_id,
                        direction="payout")
    except Exception as e:  # noqa: BLE001 — the seller already holds the money in its wallet
        journal.append(by, "payout_failed", {"n": n, "amount": int(amount), "error": f"{type(e).__name__}: {str(e)[:160]}"},
                       deal_id=deal_id)
        return None


def after_capture(deal_id: str, gross: int, n: int) -> dict | None:
    """Hook after a successful capture of `gross`: the seller received gross minus the bank fee."""
    from bank import escrow
    try:
        net = int(gross) - escrow.fee_of(escrow.stored_payment_info(deal_id), int(gross))
    except Exception as e:  # noqa: BLE001
        journal.append("bank", "payout_failed", {"n": n, "error": f"{type(e).__name__}: {str(e)[:160]}"}, deal_id=deal_id)
        return None
    return payout(deal_id, net, n)


def retry(ref: str, by: str) -> dict:
    """A person retries an op that did not finish: a pending one resends the same tx; a failed payout (nothing burnt)
    gets a fresh burn under the same ref."""
    o = op(ref)
    if o is None:
        raise RampError(404, "unknown ramp operation")
    if o["state"] == "done":
        return o
    if o["state"] == "failed":
        if o["direction"] != "payout":
            raise RampError(409, "nothing moved; start a new deposit or withdrawal")
        with db.immediate():  # the reverted burn moved nothing: its idem key may be used by a new tx
            db.run("UPDATE onchain_txs SET idem_key=NULL WHERE idem_key=? AND status='reverted'", (f"tczk:burn:{ref}",))
            db.run("UPDATE ramp_ops SET state='pending', updated_at=? WHERE ref=? AND state='failed'", (time.time(), ref))
    _client(o["company_id"])
    return _finish(op(ref), by)


def deal_view(deal: dict) -> dict | None:
    """Payouts of a tCZK deal to the seller's account (deal page, /story); None when the seller keeps the token."""
    if deal.get("asset") != "tCZK":
        return None
    seller = core.seller(deal["seller_id"])
    acc = seller and account(seller["company_id"])
    if not acc or acc["payout_mode"] != "account":
        return None
    rows = ops(deal_id=deal["deal_id"])
    done = [r for r in rows if r["state"] == "done"]
    return {"ops": rows, "paid_out": sum(r["amount"] for r in done), "open": [r for r in rows if r["state"] != "done"],
            "last": done[-1] if done else None, "account": mask(seller), "off": off_reason()}


# ---- reconciliation: the bank's books against the token on chain ----

def total_supply() -> int:
    """totalSupply of tCZK on chain (DEMO_SIM replaces this with the simulator's)."""
    return tczk.token().functions.totalSupply().call()


def books() -> dict:
    """Initial issuance + Σ minted − Σ burnt, over the done ramp operations."""
    by = {r["direction"]: r["s"] for r in db.rows(
        "SELECT direction, SUM(amount) s FROM ramp_ops WHERE state='done' GROUP BY direction")}
    return {"initial": by.get("initial"), "minted": by.get("deposit", 0),
            "burned": by.get("withdraw", 0) + by.get("payout", 0)}


def record_initial_supply() -> int | None:
    """Once, at seed: the supply minted outside the ramp (the setup's demo deposit). Without it every reconciliation
    would show the setup mint as a false difference."""
    if off_reason():
        return None
    prior = op("initial-supply")
    if prior:
        return prior["amount"]
    supply = total_supply()
    b = books()
    with db.immediate() as c:
        c.execute("INSERT OR IGNORE INTO ramp_ops (ref, direction, amount, state, created_at) VALUES"
                  " ('initial-supply','initial',?,'done',?)", (supply - b["minted"] + b["burned"], time.time()))
    return op("initial-supply")["amount"]


def reconcile() -> dict:
    """'Bank books = chain': totalSupply == initial issuance + Σmint − Σburn; the difference in haléře (expected 0)."""
    b = books()
    out = {**b, "books": None, "chain": None, "diff": None, "ok": None, "err": None, "off": off_reason(),
           "pending": db.one("SELECT COUNT(*) n FROM ramp_ops WHERE state='pending'")["n"]}
    if out["off"]:
        return out
    if b["initial"] is None:
        out["err"] = "initial issuance not recorded (run python -m scripts.seed)"
        return out
    out["books"] = b["initial"] + b["minted"] - b["burned"]
    try:
        out["chain"] = total_supply()
    except Exception as e:  # noqa: BLE001 — the chain may be unreachable: say so instead of failing the page
        out["err"] = f"chain unreachable: {type(e).__name__}"
        return out
    out["diff"] = out["chain"] - out["books"]
    out["ok"] = out["diff"] == 0
    return out
