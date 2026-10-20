# Quality rules A < B < C and the payment schedule (MODEL_V2 §7, §8.2). The bank does not judge goods: it pays by
# the code results of delivery (bank/receipts.py) and by the rule the seller chose in the offer.
#   A  no quality claims: capture P and the final void at the delivery deadline (or when a person of the buyer
#      confirms acceptance earlier).
#   B  quality guarantee X % (5/10/20): at acceptance capture (1 − X)·P; within the window the buyer may get back up
#      to X % of P without investigation (Visa RDR-like refund rule priced in); at window end capture P − refunds,
#      then the final void.
#   C  B plus a dispute with an external decider (bank/claims_c.py): nothing before the window ends; then P minus
#      B refunds minus D, and while the claim is open the disputed sum D is split off: void, and at once a new
#      authorization on D only (salt keccak(deal_id ‖ n+1)), so the undisputed refund never waits for the dispute.
# All refunds reach the buyer through ONE final void per PaymentInfo (void returns the whole remainder, so every
# capture comes first). A capture is written down (pending_json, capture_n) BEFORE it is sent, under the idem key
# capture:n of the current PaymentInfo: a retry after a crash resends the same key, never a second payment.
# A failed capture is retried (the lock is long, §8.3); it never turns into a void (§8.4).
import threading
import time
from collections import defaultdict

from bank import config, db, deals, escrow, journal, money, ramp, receipts

RULES = ("A", "B", "C")
B_PCTS = (5, 10, 20)
B_REASONS = ("closed", "wrong_data", "irrelevant", "other")
B_PAIR_LIMIT, B_NEW_BUYER_LIMIT, B_NEW_BUYER_DEALS, B_RATIO = 2, 2, 20, 0.30
DAYS_30 = 30 * 86400  # B limits count back this long, demo-scaled (config.span)
_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)


class DisputeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _now(now):
    return time.time() if now is None else now


def rank(rule: str) -> int:
    return RULES.index(rule)


# ---- the settlement record ----

def open_(deal_id: str, terms: dict, now: float | None = None) -> dict:
    """At the escrow lock: the schedule starts. Idempotent."""
    now = _now(now)
    with db.immediate() as c:
        if c.execute("SELECT 1 FROM settlements WHERE deal_id=?", (deal_id,)).fetchone() is None:
            c.execute("INSERT INTO settlements (deal_id, rule, b_pct, tier, window_s, deadline_at, step)"
                      " VALUES (?,?,?,?,?,?,'delivery')", (deal_id, terms["rule"], int(terms.get("b_pct") or 0),
                                                         terms["tier"], float(terms["window_s"]),
                                                         now + float(terms["deadline_s"])))
            journal.append("bank", "settlement.opened", {"rule": terms["rule"], "b_pct": terms.get("b_pct"),
                                                         "tier": terms["tier"], "window_s": terms["window_s"],
                                                         "deadline_s": terms["deadline_s"]}, deal_id=deal_id)
    return get(deal_id)


def get(deal_id: str) -> dict | None:
    s = db.one("SELECT * FROM settlements WHERE deal_id=?", (deal_id,))
    if s:
        s["pending"] = db.loads(s.pop("pending_json"))
        s["claims"] = claims(deal_id)
    return s


def claims_of_seller(seller_id: str, rule: str, state: str) -> list[dict]:
    rows = db.rows("SELECT * FROM claims WHERE seller_id=? AND rule=? AND state=? ORDER BY id", (seller_id, rule, state))
    for r in rows:
        r["items"] = db.loads(r.pop("items_json"))
        r["data"] = db.loads(r.pop("data_json")) or {}
    return rows


def claims(deal_id: str, rule: str | None = None) -> list[dict]:
    rows = db.rows("SELECT * FROM claims WHERE deal_id=?" + (" AND rule=?" if rule else "") + " ORDER BY id",
                   (deal_id, rule) if rule else (deal_id,))
    for r in rows:
        r["items"] = db.loads(r.pop("items_json"))
        r["data"] = db.loads(r.pop("data_json")) or {}
    return rows


def delivered(deal_id: str, now: float | None = None):
    """A version was accepted: the claim window (B/C) runs from its acceptance (§5.2 p. 6)."""
    now = _now(now)
    with db.immediate():
        s = get(deal_id)
        if s and s["window_s"] and not s["voided"]:
            db.run("UPDATE settlements SET window_end_at=?, step='window' WHERE deal_id=?",
                   (now + s["window_s"], deal_id))


# ---- amounts ----

def item_value(deal: dict, k: int) -> int:
    """Value of k items at the deal's per-item price, rounded down (the remainder goes back to the buyer)."""
    return money.per_item_split(int(deal["amount"]), int(deal["terms"]["count"]), k)[0]


def payable(deal: dict) -> int:
    """P: the sum due for accepted items (never more than ordered; never decreases)."""
    n = min(receipts.counts(deal["deal_id"])["accepted_items"], int(deal["terms"]["count"]))
    return item_value(deal, n)


def refunded_b(deal_id: str) -> int:
    return sum(c["amount"] for c in claims(deal_id, "B"))


def held_c(deal: dict, s: dict) -> tuple[int, bool]:
    """(D still held for an open C claim or decided for the buyer, whether a C claim is still open)."""
    try:
        from bank import claims_c
    except ImportError:
        return 0, False
    return claims_c.held(deal, s)


def due(deal: dict, s: dict, now: float) -> tuple[int, bool, str]:
    """(what the seller should have been paid by now, whether the deal is final, the rule that says so)."""
    from bank import deadlines
    P = payable(deal)
    pending = receipts.counts(deal["deal_id"])["pending_items"]
    s = {**s, "deadline_at": deadlines.delivery_deadline(s, now)}  # moved by registry/snapshot waits (§5.1)
    rule, x = s["rule"], s["b_pct"]
    if P == 0 and not pending and now >= s["deadline_at"]:
        return 0, True, "NO_DELIVERY: nothing accepted by the delivery deadline"
    if rule == "A":
        if (s["accepted_at"] or now >= s["deadline_at"]) and not pending:
            return P, True, "rule A: delivery deadline" if not s["accepted_at"] else "rule A: acceptance confirmed"
        return 0, False, "rule A: waiting for the delivery deadline"
    window_over = s["window_end_at"] is not None and now >= s["window_end_at"] and not pending
    if rule == "B":
        if window_over:
            return P - refunded_b(deal["deal_id"]), True, "rule B: window over"
        return P * (100 - x) // 100, False, f"rule B: {100 - x} % at acceptance"
    d, open_c = held_c(deal, s)
    if window_over:  # C: nothing before the window ends, then everything not refunded and not in dispute
        return max(0, P - refunded_b(deal["deal_id"]) - d), not open_c, "rule C: window over"
    from bank import claims_c
    c = claims_c.get(deal["deal_id"])
    if c and c["state"] == "withdrawn":  # the buyer did not pay its fee: D goes to the seller at once (§7C.3)
        return c["amount"], False, "rule C: claim withdrawn, D to the seller"
    return 0, False, "rule C: waiting for the window"


def needs_split(deal: dict, s: dict, now: float) -> bool:
    """§8.2: at window end with an open C claim, D is split off once (the undisputed part is captured first)."""
    if s["rule"] != "C" or s["split_n"] is not None or s["window_end_at"] is None or now < s["window_end_at"]:
        return False
    d, open_c = held_c(deal, s)
    return open_c and d > 0


# ---- money ----

def _record(deal_id: str, kind: str, actor: str, rule: str, text: str):
    db.run("INSERT INTO decisions (deal_id, kind, outcome, codes_json, evidence_json, explanation_client,"
           " explanation_compliance, ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
           (deal_id, kind, "ok", db.dumps([None]), db.dumps([]), text, f"{text} Actor: {actor}; rule: {rule}.",
            None, time.time()))


def _send(deal: dict, pending: dict) -> dict:
    if pending["kind"] == "capture":
        return escrow.capture(deal["deal_id"], int(pending["amount"]), int(pending["n"]))
    if pending["kind"] == "split":
        return escrow.split_off(deal["deal_id"], int(pending["amount"]))
    return escrow.void(deal["deal_id"])  # "void" (final) or "void_split" (before the split-off)


def _execute(deal: dict, pending: dict, actor: str, rule: str):
    """Send the written-down tx, then book it. A failure keeps `pending` for the next tick (same idem key)."""
    deal_id, asset = deal["deal_id"], deal["asset"]
    try:
        tx = _send(deal, pending)
    except Exception as e:  # noqa: BLE001 — revert, expiry or an ambiguous RPC error: retried, never a void (§8.4)
        journal.append(actor, "settlement.tx_failed", {"pending": pending, "error": repr(e)[:300]}, deal_id=deal_id)
        return None
    mandate = deals._module("mandate")
    with db.immediate() as c:
        # compare-and-set: book only if this exact tx is still the pending one (another process may have sent the
        # same idem key and booked it already — the chain moved the money once, so it is booked once)
        row = c.execute("SELECT pending_json, captured FROM settlements WHERE deal_id=?", (deal_id,)).fetchone()
        if row is None or row["pending_json"] != db.dumps(pending):
            journal.append(actor, "settlement.already_booked", {**pending, "tx_hash": tx["tx_hash"]}, deal_id=deal_id)
            return tx
        before, h = row["captured"], deals.get(deal_id)["payment_hash"]
        if pending["kind"] == "capture":
            c.execute("UPDATE settlements SET captured=?, pending_json=NULL WHERE deal_id=?",
                      (before + int(pending["amount"]), deal_id))
            c.execute("UPDATE payment_infos SET captured=captured+? WHERE payment_hash=?", (int(pending["amount"]), h))
            text = f"{money.fmt(asset, int(pending['amount']))} paid to the seller from escrow."
        elif pending["kind"] == "void_split":
            returned = escrow.locked_amount(deal_id) - before
            c.execute("UPDATE settlements SET split_n=-1, pending_json=NULL WHERE deal_id=?", (deal_id,))
            c.execute("UPDATE payment_infos SET voided=1 WHERE payment_hash=?", (h,))
            text = (f"{money.fmt(asset, returned)} left the escrow: the undisputed part goes back to the buyer, the "
                    "disputed sum is locked again at once.")
        elif pending["kind"] == "split":
            c.execute("UPDATE settlements SET split_n=?, pending_json=NULL WHERE deal_id=?",
                      (deals.get(deal_id)["pi_n"], deal_id))
            text = f"{money.fmt(asset, int(pending['amount']))} in dispute locked again in escrow (split-off of D)."
        else:
            returned = escrow.locked_amount(deal_id) - before
            c.execute("UPDATE settlements SET voided=1, pending_json=NULL WHERE deal_id=?", (deal_id,))
            c.execute("UPDATE payment_infos SET voided=1 WHERE payment_hash=?", (h,))
            text = f"{money.fmt(asset, returned)} returned to the buyer from escrow."
        journal.append(actor, f"settlement.{pending['kind']}", {**pending, "rule": rule, "tx_hash": tx["tx_hash"],
                                                                 "link": tx.get("link")}, deal_id=deal_id)
        _record(deal_id, pending["kind"], actor, rule, text)
    if pending["kind"] == "split":  # the seller checks the new lock on chain like the first one
        deals.notify_seller(deal_id, kind="split_off", amount=int(pending["amount"]))
        return tx
    if pending["kind"] == "void_split":  # D is in the buyer's wallet under the bank's key: the reservation stays
        return tx
    try:
        if pending["kind"] == "capture":  # commit by the rounded-up total, so the parts never exceed the reservation
            delta = money.to_eur_cents(asset, before + int(pending["amount"])) - money.to_eur_cents(asset, before)
            if delta:
                mandate.commit(deal_id, delta)
        else:
            mandate.release(deal_id)
    except ValueError as e:  # no reservation (a deal created outside deals.pay): the escrow stays the truth
        journal.append("bank", "budget_booking_skipped", {"error": str(e)}, deal_id=deal_id)
    if pending["kind"] == "capture":  # tCZK seller paid on its bank account (bank/ramp.py); never undoes the capture
        ramp.after_capture(deal_id, int(pending["amount"]), int(pending["n"]))
    return tx


def advance(deal_id: str, actor: str = "bank", now: float | None = None) -> dict | None:
    """Move the money to where the schedule says it belongs. Safe to call any number of times."""
    now = _now(now)
    with _locks[deal_id]:
        for _ in range(8):  # resume a pending tx -> capture what is due -> the final void -> close
            deal, s = deals.get(deal_id), get(deal_id)
            if s is None or s["closed_at"]:
                break
            if s["pending"]:
                if _execute(deal, s["pending"], actor, s["pending"].get("rule", "")) is None:
                    break
                continue
            if s["voided"]:
                _close(deal_id, actor, now)
                break
            target, final, rule = due(deal, s, now)
            target = min(target, escrow.locked_amount(deal_id))
            nxt = None
            if target > s["captured"]:
                nxt = {"kind": "capture", "amount": target - s["captured"], "n": s["capture_n"] + 1, "rule": rule}
            elif final:
                if s["captured"] < escrow.locked_amount(deal_id):
                    nxt = {"kind": "void", "rule": rule}
                else:
                    _close(deal_id, actor, now)
                    break
            elif needs_split(deal, s, now):
                nxt = {"kind": "void_split", "rule": "§8.2 split-off of D: void first"}
            elif s["split_n"] == -1:
                nxt = {"kind": "split", "amount": held_c(deal, s)[0], "rule": "§8.2 split-off of D: lock D again"}
            if nxt is None:
                break
            with db.immediate() as c:  # written down before sending: a retry reuses n -> the same idem key
                c.execute("UPDATE settlements SET pending_json=?, capture_n=? WHERE deal_id=?",
                          (db.dumps(nxt), nxt.get("n", s["capture_n"]), deal_id))
        _refresh_step(deal_id, now)
        return get(deal_id)


def _close(deal_id: str, actor: str, now: float):
    with db.immediate():
        s = get(deal_id)
        outcome = "no_delivery" if payable(deals.get(deal_id)) == 0 else "settled"
        db.run("UPDATE settlements SET closed_at=?, step='closed', outcome=? WHERE deal_id=?", (now, outcome, deal_id))
        db.run("UPDATE deals SET state='settled', updated_at=? WHERE deal_id=?", (time.time(), deal_id))
        journal.append(actor, "settlement.closed", {"state": "settled", "captured": s["captured"],
                                                    "voided": bool(s["voided"]), "outcome": outcome}, deal_id=deal_id)
        if outcome == "no_delivery":
            journal.append(actor, "autocode", {"code": "NO_DELIVERY"}, deal_id=deal_id)
    mandate = deals._module("mandate")
    if hasattr(mandate, "release_fee"):  # no C claim came: the claim-fee reservation goes back (§2)
        mandate.release_fee(deal_id)


def _refresh_step(deal_id: str, now: float):
    s = get(deal_id)
    if s is None or s["closed_at"]:
        return
    if s["window_end_at"] is None:
        step = "delivery"
    elif now < s["window_end_at"]:
        step = "window"
    else:
        step = "dispute_c" if held_c(deals.get(deal_id), s)[1] else "settling"
    if step != s["step"]:
        db.run("UPDATE settlements SET step=? WHERE deal_id=?", (step, deal_id))
        if step == "dispute_c":
            db.run("UPDATE deals SET state='dispute_c', updated_at=? WHERE deal_id=?", (time.time(), deal_id))


# ---- rule A: a person confirms acceptance early ----

def accept(deal_id: str, by: str, now: float | None = None) -> dict:
    if not by.startswith("human:"):
        raise DisputeError(403, "only a person of the buyer confirms acceptance")
    with _locks[deal_id]:
        s = get(deal_id)
        if s is None or s["closed_at"] or s["voided"]:
            raise DisputeError(409, "nothing to accept")
        if s["rule"] != "A":
            raise DisputeError(409, f"rule {s['rule']}: the window decides, not an early acceptance")
        db.run("UPDATE settlements SET accepted_at=?, accepted_by=? WHERE deal_id=?", (_now(now), by, deal_id))
        journal.append(by, "settlement.accepted", {"rule": "A"}, deal_id=deal_id)
    return advance(deal_id, actor=by, now=now)


# ---- rule B: limits, slots reserved at pay, the claim ----

def b_availability(buyer_id: str, seller_id: str, now: float | None = None, except_deal: str | None = None) -> dict:
    """Can this pair get the B guarantee (§7B)? Used claims + B/C deals with an open window (their slot reserved at
    pay) must stay under the limits; a buyer with > 30 % B-claimed deals (smoothed) has no B."""
    now = _now(now)
    since = now - config.span(DAYS_30)
    used = db.rows("SELECT seller_id FROM claims WHERE buyer_id=? AND rule='B' AND filed_at>=?", (buyer_id, since))
    open_slots = db.rows(
        "SELECT d.seller_id FROM deals d JOIN settlements s ON s.deal_id=d.deal_id WHERE d.buyer_id=?"
        " AND json_extract(d.terms_json,'$.b_slot')=1 AND s.closed_at IS NULL AND d.deal_id IS NOT ?"
        " AND (s.window_end_at IS NULL OR s.window_end_at>?)"
        " AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.deal_id=d.deal_id AND c.rule='B')",
        (buyer_id, except_deal, now))
    pair = sum(1 for r in used + open_slots if r["seller_id"] == seller_id)
    deals_n = db.one("SELECT COUNT(*) n FROM deals WHERE buyer_id=? AND state IN ('settled','dispute_c')",
                     (buyer_id,))["n"]
    b_deals = db.one("SELECT COUNT(*) n FROM deals d JOIN settlements s ON s.deal_id=d.deal_id WHERE d.buyer_id=?"
                     " AND s.rule IN ('B','C') AND s.deadline_at>=?", (buyer_id, since))["n"]
    ratio = (len(used) + 1) / (b_deals + 20)
    if ratio > B_RATIO:
        return {"available": False, "reason": "b_claim_ratio", "text": "More than 30 % of this buyer's B deals had a claim."}
    if pair >= B_PAIR_LIMIT:
        return {"available": False, "reason": "b_pair_limit",
                "text": f"At most {B_PAIR_LIMIT} B claims per buyer and seller in 30 days (open B deals count)."}
    if deals_n < B_NEW_BUYER_DEALS and len(used) + len(open_slots) >= B_NEW_BUYER_LIMIT:
        return {"available": False, "reason": "b_new_buyer_limit",
                "text": f"A buyer with fewer than {B_NEW_BUYER_DEALS} deals has at most {B_NEW_BUYER_LIMIT} B claims "
                        "in 30 days (open B deals count)."}
    return {"available": True, "reason": None, "text": ""}


def file_b(deal_id: str, items: list, reason: str, by: str, now: float | None = None) -> dict:
    """Return up to X % of P without investigation (§7B). items: [[version, index], ...] of accepted items."""
    now = _now(now)
    if reason not in B_REASONS:
        raise DisputeError(400, f"reason must be one of {', '.join(B_REASONS)}")
    with _locks[deal_id]:
        deal, s = deals.get(deal_id), get(deal_id)
        if s is None or s["rule"] not in ("B", "C"):
            raise DisputeError(409, "this deal has no B guarantee")
        if not deal["terms"].get("b_slot"):
            raise DisputeError(409, "B without claims: the buyer's B limits were used when this deal was paid")
        if s["voided"] or s["closed_at"] or s["window_end_at"] is None or now >= s["window_end_at"]:
            raise DisputeError(409, "the claim window is not open")
        if claims(deal_id, "B"):
            raise DisputeError(409, "one B claim per deal")
        keys = _accepted_keys(deal_id, items)
        cap = payable(deal) * s["b_pct"] // 100
        amount = min(item_value(deal, len(keys)), cap)
        with db.immediate():
            db.run("INSERT INTO claims (deal_id, rule, buyer_id, seller_id, items_json, reason, amount, state, filed_at,"
                   " by) VALUES (?,?,?,?,?,?,?,?,?,?)", (deal_id, "B", deal["buyer_id"], deal["seller_id"],
                                                       db.dumps([list(k) for k in keys]), reason, amount, "refund",
                                                       now, by))
            for v, i in keys:  # the same item cannot be sold to this buyer again in this task (§5.2 p. 6)
                db.run("UPDATE items SET refunded='B' WHERE deal_id=? AND version=? AND idx=?", (deal_id, v, i))
            journal.append(by, "claim_b.filed", {"items": len(keys), "reason": reason, "amount": amount, "cap": cap},
                           deal_id=deal_id)
    advance(deal_id, actor=by, now=now)
    return {"rule": "B", "amount": amount, "amount_text": money.fmt(deal["asset"], amount), "items": len(keys)}


def _accepted_keys(deal_id: str, items: list) -> list[tuple[int, int]]:
    keys = []
    for it in items or []:
        try:
            v, i = int(it[0]), int(it[1])
        except (TypeError, ValueError, IndexError) as e:
            raise DisputeError(400, "items are [[version, index], ...]") from e
        row = db.one("SELECT status, refunded FROM items WHERE deal_id=? AND version=? AND idx=?", (deal_id, v, i))
        if row is None or row["status"] != "accepted" or row["refunded"]:
            raise DisputeError(409, f"item {v}/{i} is not an accepted item without a claim")
        if (v, i) not in keys:
            keys.append((v, i))
    if not keys:
        raise DisputeError(400, "name at least one item")
    return keys


# ---- the timer ----

def open_ids() -> list[str]:
    return [r["deal_id"] for r in db.rows("SELECT deal_id FROM settlements WHERE closed_at IS NULL ORDER BY deal_id")]


def tick(deal_id: str, now: float | None = None) -> dict | None:
    """Recheck items waiting for a registry or snapshot, move a C claim along its deadlines, then let the money
    follow the schedule."""
    deal = deals.get(deal_id)
    if deal["terms"] and deal["terms"].get("rule") == "C":
        from bank import claims_c
        try:
            claims_c.tick(deal_id, now=now)
        except Exception as e:  # noqa: BLE001 — journalled; the escrow does not move because of it
            journal.append("timer", "claim_c.tick_failed", {"error": repr(e)[:200]}, deal_id=deal_id)
    try:
        if receipts.recheck_pending(deal, now=now, **deals.lookups()):
            delivered(deal_id, now=now)  # the buyer sees those items only now: their window starts now
            from bank import deadlines
            t = _now(now)
            journal.append("timer", "delivery.deadline_extended", {
                "deadline_at": deadlines.delivery_deadline(get(deal_id), t), "waited_s": round(deadlines.waited(deal_id, t)),
                "rule": "§5.1: the delivery deadline moves by the time items waited for a registry or a snapshot"},
                deal_id=deal_id)
    except Exception as e:  # noqa: BLE001 — a registry outage keeps items pending; the money waits too
        journal.append("timer", "delivery.recheck_failed", {"error": repr(e)[:200]}, deal_id=deal_id)
    return advance(deal_id, actor="timer", now=now)


def money_view(deal: dict, s: dict | None) -> dict:
    """Where the money of a deal is, atomic units of the deal asset."""
    total = int(deal.get("amount") or 0)
    if s is None:
        locked = total if deal.get("state") in ("authorized", "delivered") else 0
        return {"total": total, "captured": 0, "returned": 0, "in_escrow": locked}
    returned = total - s["captured"] if s["voided"] else 0
    return {"total": total, "captured": s["captured"], "returned": returned,
            "in_escrow": total - s["captured"] - returned}


def silence_text(deal: dict, s: dict) -> str:
    """'What happens if nobody acts', on the deal page (§11). Times are 'up to N business days' for every deal."""
    if s["closed_at"]:
        return ""
    if s["rule"] == "A":
        return "At the delivery deadline the seller is paid for accepted items; the rest returns to the buyer."
    if s["rule"] == "B":
        return (f"{100 - s['b_pct']} % is paid at acceptance; at the end of the window the seller gets the rest minus "
                f"B refunds (up to {s['b_pct']} %), and everything else returns to the buyer in one final void.")
    return ("At the end of the window everything not in dispute is settled; a C claim keeps only the disputed sum in "
            "escrow until the decision.")


def config_note() -> str:
    return (f"Demo scale: euro thresholds and fees ÷{config.MONEY_DIVISOR}, windows ÷{config.TIME_DIVISOR:g}."
            if config.DEMO_SCALE else "")

