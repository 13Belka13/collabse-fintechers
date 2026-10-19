# Disputes and the L5 settlement (SPEC §9). People argue about items, not deals: what passed the rules is paid
# to the seller at once (minus the holdback, §9.3), only the items in question wait in escrow. Every step has a
# deadline and a default for whoever stays silent (dispute_terms from the offer, §9.1); the timer in
# bank/deadlines.py applies them, a human (buyer, seller, bank/arbiter) can act before.
# Money: escrow.capture(n) for everything owed, and only at the very end escrow.void (there is no partial void,
# so every capture comes first). A capture is written down (pending_json, capture_n) BEFORE it is sent, under
# the deal's idem key f"{deal_id}:capture:{n}": a retry after a crash resends the same key, never a second payment.
import threading
import time
from collections import defaultdict

from bank import chain, db, deals, escrow, journal, money

DEMO_TERMS = {"review_window_s": 60, "objection_window_s": 120, "decision_sla_s": 120,
              "on_buyer_silence": "accept", "on_seller_silence": "rejection_stands",
              "on_decision_timeout": "rules_verdict", "holdback_bps": 500, "holdback_s": 300}
SILENCE = {"accept": "accepted", "rejection_stands": "rejected", "reject": "rejected"}
BASE_CRITERIA = ("permanently_closed", "duplicate")  # §8 rules that hold for every deal, beside criteria_json
FINAL_FAILURES = {"over_quantity", "duplicate", "placeId"}  # extra or broken items: nothing to argue about
_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)


class DisputeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _now(now):
    return time.time() if now is None else now


def terms_of(deal: dict) -> dict:
    return {**DEMO_TERMS, **((deal.get("offer") or {}).get("dispute_terms") or {})}


def guard_s(terms: dict) -> int:
    """No step may end later than this before authorizationExpiry (§9.5): 1 h, with demo windows 30 s."""
    return 3600 if int(terms["holdback_s"]) >= 86400 else 30


def _clamp(t: float, deal: dict, terms: dict) -> float:
    pi = deal.get("payment_info") or {}
    if pi.get("authorizationExpiry"):
        return min(t, int(pi["authorizationExpiry"]) - guard_s(terms))
    return t


def allowed_criteria(deal: dict) -> list[str]:
    """What a rejection may cite: the deal's agreed criteria (criteria_json) plus the §8 base rules."""
    from bank import verifier
    c = verifier.criteria_of(deal.get("criteria"))
    return ["city", "min_rating", "category", *[f"has_{f}" for f in c["required_fields"]], *BASE_CRITERIA]


def get(deal_id: str) -> dict | None:
    row = db.one("SELECT * FROM disputes WHERE deal_id=?", (deal_id,))
    if row:
        row["items"] = db.loads(row.pop("items_json")) or []
        row["terms"] = db.loads(row.pop("terms_json")) or DEMO_TERMS
        row["pending"] = db.loads(row.pop("pending_json"))
    return row


def _save(c, deal_id: str, d: dict, **cols):
    cols = {"items_json": db.dumps(d["items"]), **cols}
    c.execute(f"UPDATE disputes SET {', '.join(f'{k}=?' for k in cols)} WHERE deal_id=?", (*cols.values(), deal_id))


def _deal_state(deal_id: str, state: str, event: str, actor: str, payload: dict):
    with db.immediate():
        db.run("UPDATE deals SET state=?, updated_at=? WHERE deal_id=?", (state, time.time(), deal_id))
        journal.append(actor, event, {"state": state, **payload}, deal_id=deal_id)


def _captured_so_far(deal_id: str) -> tuple[int, int]:
    """(amount, last n) of captures already sent for this deal (deals.settle may have paid the first part)."""
    caps = [e for e in journal.entries(deal_id, limit=1000) if e["event"] == "escrow_captured"]
    return sum(int(e["payload"]["amount"]) for e in caps), max([int(e["payload"].get("n", 1)) for e in caps] or [0])


# ---- open ----

def settle(deal_id: str, now: float | None = None) -> dict:
    """The L5 settlement after a delivery (replaces deals.settle): rules verdict -> dispute (maybe empty) -> money."""
    open_(deal_id, now=now)
    return advance(deal_id, actor="bank", rule="rules_verdict", now=now)


def open_(deal_id: str, now: float | None = None, plan: dict | None = None) -> dict:
    """Open the dispute record for a delivered deal (idempotent). Items held by the judge go to the buyer's
    review, items rejected by the rules to the seller's objection window. Also adopts a deal that deals.settle
    left 'verified' (accepted part already captured in full: no holdback then)."""
    now = _now(now)
    with _locks[deal_id]:
        existing = get(deal_id)
        if existing:
            return existing
        deal = deals.get(deal_id)
        if deal is None:
            raise DisputeError(404, f"unknown deal {deal_id}")
        if deal["state"] not in ("delivered", "verified"):
            raise DisputeError(409, f"deal is {deal['state']}, nothing to settle")
        if plan is None:
            from bank import verifier
            plan = verifier.settlement(deal_id)
        terms = terms_of(deal)
        captured, capture_n = _captured_so_far(deal_id)
        adopted = deal["state"] == "verified"
        holdback = 0 if adopted else plan["capture_now"] * int(terms["holdback_bps"]) // 10_000
        items = []
        for it in plan["items"]:
            if it["status"] == "accepted" or set(it["failed"]) & FINAL_FAILURES:
                continue
            judge = it.get("judge") or None
            base = {"placeId": it["placeId"], "title": it.get("title"), "failed": it["failed"], "outcome": None,
                    "judge": judge and {k: judge.get(k) for k in ("reason", "plausible_warehouse", "ai_generated")},
                    "rejection": None, "objection": None, "decided_by": None, "rule": None, "decided_at": None}
            if it["status"] == "held":
                base.update(origin="judge", step="review",
                            deadline_at=_clamp(now + int(terms["review_window_s"]), deal, terms))
            else:
                base.update(origin="rules", step="objection",
                            deadline_at=_clamp(now + int(terms["objection_window_s"]), deal, terms),
                            rejection={"criterion": it["failed"][0], "criteria": it["failed"], "by": "rules",
                                       "evidence": "Code rules on the delivered item (SPEC §8).", "at": now})
            items.append(base)
        holdback_until = _clamp(now + int(terms["holdback_s"]), deal, terms) if holdback else None
        with db.immediate() as c:
            c.execute("INSERT INTO disputes (deal_id, items_json, rejection_criteria_json, terms_json, captured,"
                      " capture_n, holdback, holdback_until, holdback_released, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (deal_id, db.dumps(items), db.dumps(allowed_criteria(deal)), db.dumps(terms), captured,
                       capture_n, holdback, holdback_until, 0 if holdback else 1, now))
            journal.append("bank", "dispute.opened", {
                "items": len(items), "accepted_by_rules": (plan.get("counts") or {}).get("accepted"),
                "holdback": holdback, "holdback_until": holdback_until, "terms": terms, "adopted": adopted,
                "already_captured": captured}, deal_id=deal_id)
            _deal_state(deal_id, "disputed" if items else "settled", "dispute.state", "bank",
                        {"items_in_question": len(items)})
        _refresh_step(deal_id)
        return get(deal_id)


# ---- money ----

def _counts(deal: dict) -> tuple[int, int]:
    from bank import verifier
    count = int(verifier.criteria_of(deal.get("criteria"))["count"])
    row = db.one("SELECT rule_results_json FROM deliveries WHERE deal_id=?", (deal["deal_id"],)) or {}
    rules = db.loads(row.get("rule_results_json")) or []
    return count, sum(1 for r in rules if r.get("status") == "accepted")


def owed(deal: dict, d: dict) -> int:
    """What the seller is owed right now: accepted items (rules + dispute) minus the holdback still running."""
    count, by_rules = _counts(deal)
    accepted = min(count, by_rules + sum(1 for it in d["items"] if it["outcome"] == "accepted"))
    to_seller, _ = money.per_item_split(int(deal["amount"]), count, accepted)
    return max(0, to_seller - (0 if d["holdback_released"] else d["holdback"]))


def _record(deal_id: str, kind: str, actor: str, rule: str, text: str):
    db.run("INSERT INTO decisions (deal_id, kind, outcome, codes_json, evidence_json, explanation_client,"
           " explanation_compliance, ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
           (deal_id, kind, "ok", db.dumps([None]), db.dumps([]), text, f"{text} Actor: {actor}; rule: {rule}.",
            None, time.time()))


def _send(deal: dict, pending: dict) -> dict:
    if pending["kind"] == "capture":
        return escrow.capture(deal["deal_id"], int(pending["amount"]), int(pending["n"]))
    return escrow.void(deal["deal_id"])


def _execute(deal: dict, pending: dict, actor: str, rule: str):
    """Send the written-down tx, then book it. A failure keeps `pending` for the next tick (same idem key)."""
    deal_id, asset = deal["deal_id"], deal["asset"]
    try:
        tx = _send(deal, pending)
    except (escrow.EscrowError, chain.TxReverted) as e:
        if pending["kind"] == "capture" and (isinstance(e, chain.TxReverted) or "authorizationExpiry" in str(e)):
            # the seller can no longer be paid (expiry, seller delisted/frozen): like deals.settle, the whole
            # remaining lock goes back to the buyer and the budget is released (§9.5, INTERFACES integration rules)
            journal.append(actor, "dispute.capture_impossible", {"error": str(e)[:300], "rule": rule}, deal_id=deal_id)
            db.run("UPDATE disputes SET pending_json=? WHERE deal_id=?", (db.dumps({"kind": "void"}), deal_id))
            return _execute(deal, {"kind": "void"}, actor, "capture_impossible")
        journal.append(actor, "dispute.tx_failed", {"pending": pending, "error": str(e)[:200]}, deal_id=deal_id)
        return None
    except Exception as e:  # noqa: BLE001 — ambiguous RPC error: retry later with the same idem key
        journal.append(actor, "dispute.tx_failed", {"pending": pending, "error": repr(e)[:200]}, deal_id=deal_id)
        return None
    from bank import mandate
    before = get(deal_id)["captured"]
    with db.immediate() as c:
        if pending["kind"] == "capture":
            c.execute("UPDATE disputes SET captured=?, pending_json=NULL WHERE deal_id=?",
                      (before + int(pending["amount"]), deal_id))
            text = f"{money.fmt(asset, int(pending['amount']))} paid to the seller from escrow."
        else:
            c.execute("UPDATE disputes SET voided=1, pending_json=NULL WHERE deal_id=?", (deal_id,))
            text = f"{money.fmt(asset, int(deal['amount']) - before)} returned to the buyer from escrow."
        journal.append(actor, f"dispute.{pending['kind']}", {**pending, "rule": rule, "tx_hash": tx["tx_hash"],
                                                             "link": tx.get("link")}, deal_id=deal_id)
        _record(deal_id, pending["kind"], actor, rule, text)
    try:
        if pending["kind"] == "capture":  # commit by the rounded-up total, so the parts never exceed the reservation
            delta = (money.to_eur_cents(asset, before + int(pending["amount"])) - money.to_eur_cents(asset, before))
            if delta:
                mandate.commit(deal_id, delta)
        else:
            mandate.release(deal_id)
    except ValueError as e:  # no reservation (a deal created outside deals.pay): the escrow stays the truth
        journal.append("bank", "budget_booking_skipped", {"error": str(e)}, deal_id=deal_id)
    return tx


def advance(deal_id: str, actor: str = "bank", rule: str = "", now: float | None = None) -> dict | None:
    """Move the money to where the dispute state says it belongs. Safe to call any number of times."""
    with _locks[deal_id]:
        for _ in range(8):  # resume a pending tx -> capture what is owed -> the final void -> close
            deal, d = deals.get(deal_id), get(deal_id)
            if d is None or d["closed_at"]:
                break
            if d["pending"]:
                if _execute(deal, d["pending"], actor, rule) is None:
                    break
                continue
            if d["voided"]:  # the lock went back to the buyer (final void or a failed capture): nothing left to pay
                _close(deal_id, actor, rule, now)
                break
            nxt, due = None, owed(deal, d)
            if due > d["captured"]:
                nxt = {"kind": "capture", "amount": due - d["captured"], "n": d["capture_n"] + 1}
            elif all(it["outcome"] for it in d["items"]) and d["holdback_released"]:
                if d["captured"] < int(deal["amount"]):
                    nxt = {"kind": "void"}
                else:
                    _close(deal_id, actor, rule, now)
                    break
            if nxt is None:
                break
            with db.immediate() as c:  # written down before sending: a retry reuses n -> the same idem key
                c.execute("UPDATE disputes SET pending_json=?, capture_n=? WHERE deal_id=?",
                          (db.dumps(nxt), nxt.get("n", d["capture_n"]), deal_id))
        _refresh_step(deal_id)
        return get(deal_id)


def _close(deal_id: str, actor: str, rule: str, now):
    with db.immediate() as c:
        c.execute("UPDATE disputes SET closed_at=?, step='closed', step_deadline_at=NULL WHERE deal_id=?",
                  (_now(now), deal_id))
        d = get(deal_id)
        _deal_state(deal_id, "closed", "dispute.closed", actor,
                    {"captured": d["captured"], "voided": bool(d["voided"]), "rule": rule})


def _refresh_step(deal_id: str):
    """Summary columns for the UI and the timer: the earliest open step and its deadline."""
    d = get(deal_id)
    if d is None or d["closed_at"]:
        return
    open_items = [it for it in d["items"] if not it["outcome"]]
    deadlines = [it["deadline_at"] for it in open_items]
    if not d["holdback_released"] and d["holdback_until"]:
        deadlines.append(d["holdback_until"])
    step = next((s for s in ("review", "objection", "decision") if any(it["step"] == s for it in open_items)), None)
    step = step or ("holdback" if not d["holdback_released"] else "settling")
    with db.immediate() as c:
        c.execute("UPDATE disputes SET step=?, step_deadline_at=? WHERE deal_id=?",
                  (step, min(deadlines) if deadlines else None, deal_id))
        if not open_items and db.one("SELECT state FROM deals WHERE deal_id=?", (deal_id,))["state"] == "disputed":
            _deal_state(deal_id, "settled", "dispute.items_resolved", "bank", {})


# ---- human actions ----

def _item(d: dict, place_id: str, step: str) -> dict:
    it = next((i for i in d["items"] if i["placeId"] == place_id), None)
    if it is None:
        raise DisputeError(404, f"item {place_id} is not in question")
    if it["outcome"] or it["step"] != step:
        raise DisputeError(409, f"item {place_id} is at step {it['outcome'] or it['step']}, not {step}")
    return it


def _act(deal_id: str, actor: str, event: str, place_id: str, step: str, change, now):
    """Change one item under the deal lock, journal it with the human actor, then let the money follow."""
    if not actor.startswith("human:") or len(actor) <= len("human:"):
        raise DisputeError(400, "actor must be human:<who>")
    now = _now(now)
    with _locks[deal_id]:
        d = get(deal_id)
        if d is None or d["closed_at"]:
            raise DisputeError(404 if d is None else 409, "no open dispute for this deal")
        it = _item(d, place_id, step)
        payload, extra = change(it, deals.get(deal_id), d["terms"], now)
        with db.immediate() as c:
            _save(c, deal_id, d, **extra)
            journal.append(actor, event, {"placeId": place_id, **payload}, deal_id=deal_id)
        return advance(deal_id, actor=actor, rule=payload["rule"], now=now)


def buyer_review(deal_id: str, place_id: str, action: str, who: str, criterion: str | None = None,
                 evidence: str | None = None, now: float | None = None) -> dict:
    """Buyer on an item the judge flagged: accept, or reject citing an agreed criterion with evidence
    ('I don't like it' is not a reason, §9.2). A rejection opens the seller's objection window."""
    if action not in ("accept", "reject"):
        raise DisputeError(400, "action must be accept or reject")

    def change(it, deal, terms, now):
        if action == "accept":
            it.update(outcome="accepted", step="done", decided_by=who, rule="buyer_accepted", decided_at=now)
            return {"outcome": "accepted", "rule": "buyer_accepted"}, {}
        if criterion not in allowed_criteria(deal):
            raise DisputeError(400, "a rejection must cite an agreed criterion: " + ", ".join(allowed_criteria(deal)))
        if len((evidence or "").strip()) < 5:
            raise DisputeError(400, "a rejection needs evidence (what is wrong with the item, a link)")
        it.update(step="objection", deadline_at=_clamp(now + int(terms["objection_window_s"]), deal, terms),
                  rejection={"criterion": criterion, "criteria": [criterion], "evidence": evidence.strip()[:500],
                             "by": who, "at": now})
        return {"outcome": "rejected_pending_objection", "criterion": criterion, "evidence": evidence.strip()[:500],
                "rule": "buyer_rejected"}, {}
    return _act(deal_id, who, "dispute.buyer_review", place_id, "review", change, now)


def seller_objection(deal_id: str, place_id: str, statement: str, who: str, now: float | None = None) -> dict:
    """Seller objects to a rejection with a statement -> the bank (or an arbiter) decides within decision_sla_s."""
    statement = (statement or "").strip()[:1000]
    if len(statement) < 5:
        raise DisputeError(400, "an objection needs a statement")

    def change(it, deal, terms, now):
        it.update(step="decision", deadline_at=_clamp(now + int(terms["decision_sla_s"]), deal, terms),
                  objection={"statement": statement, "by": who, "at": now})
        return ({"statement": statement, "source": "untrusted:seller", "rule": "seller_objected"},
                {"seller_statement": statement})
    return _act(deal_id, who, "dispute.seller_objection", place_id, "objection", change, now)


def decide(deal_id: str, place_id: str, verdict: str, who: str, note: str = "", now: float | None = None) -> dict:
    """Bank officer / arbiter decides an objected item: accept (seller paid for it) or reject (back to the buyer)."""
    if verdict not in ("accept", "reject"):
        raise DisputeError(400, "verdict must be accept or reject")

    def change(it, deal, terms, now):
        outcome = "accepted" if verdict == "accept" else "rejected"
        it.update(outcome=outcome, step="done", decided_by=who, rule="human_decision", decided_at=now,
                  note=(note or "")[:500])
        return ({"outcome": outcome, "note": (note or "")[:500], "rule": "human_decision"},
                {"decision": outcome, "decided_by": who, "decided_at": now, "default_rule": None})
    return _act(deal_id, who, "dispute.decision", place_id, "decision", change, now)


# ---- silence defaults (the timer) ----

def apply_defaults(deal_id: str, now: float | None = None) -> list[dict]:
    """Apply the default of every step whose deadline passed, release the holdback when due, then move the money.
    Returns what was applied. Idempotent: a second call at the same moment finds nothing to do."""
    now = _now(now)
    with _locks[deal_id]:
        d = get(deal_id)
        if d is None or d["closed_at"]:
            return []
        terms, applied = d["terms"], []
        for it in d["items"]:
            if it["outcome"] or it["deadline_at"] > now:
                continue
            if it["step"] == "review":
                rule, outcome = f"on_buyer_silence: {terms['on_buyer_silence']}", SILENCE[terms["on_buyer_silence"]]
            elif it["step"] == "objection":
                rule, outcome = f"on_seller_silence: {terms['on_seller_silence']}", SILENCE[terms["on_seller_silence"]]
            else:  # no decision in time -> what the agreed rules say about the item
                rule, outcome = f"on_decision_timeout: {terms['on_decision_timeout']}", \
                    "rejected" if it["failed"] else "accepted"
            it.update(outcome=outcome, step="done", decided_by="timer", rule=rule, decided_at=now)
            applied.append({"placeId": it["placeId"], "outcome": outcome, "rule": rule})
        release = not d["holdback_released"] and d["holdback_until"] is not None and d["holdback_until"] <= now
        if applied or release:
            with db.immediate() as c:
                _save(c, deal_id, d, **({"holdback_released": 1} if release else {}),
                      **({"default_rule": applied[-1]["rule"]} if applied else {}))
                for a in applied:
                    journal.append("timer", "dispute.default_applied", a, deal_id=deal_id)
                if release:
                    rule = f"holdback_s: {terms['holdback_s']} elapsed"
                    journal.append("timer", "dispute.holdback_released", {"holdback": d["holdback"], "rule": rule},
                                   deal_id=deal_id)
                    applied.append({"holdback": d["holdback"], "rule": rule})
        if applied or d["pending"]:
            advance(deal_id, actor="timer", rule="; ".join(a["rule"] for a in applied) or "retry pending tx", now=now)
        return applied


def open_ids() -> list[str]:
    return [r["deal_id"] for r in db.rows("SELECT deal_id FROM disputes WHERE closed_at IS NULL ORDER BY opened_at")]


def money_view(deal: dict, d: dict | None) -> dict:
    """Where the money of a deal is (§9.3), atomic units of the deal asset."""
    total = int(deal.get("amount") or 0)
    if d is None:
        locked = total if deal.get("state") in ("authorized", "delivered", "verified") else 0
        return {"total": total, "captured": 0, "returned": 0, "in_escrow": locked, "holdback": 0}
    returned = total - d["captured"] if d["voided"] else 0
    return {"total": total, "captured": d["captured"], "returned": returned,
            "in_escrow": total - d["captured"] - returned, "holdback": 0 if d["holdback_released"] else d["holdback"]}


def silence_text(item: dict, terms: dict) -> str:
    """'What happens if nobody acts', shown to both sides on the card (§9.7)."""
    if item["step"] == "review":
        return ("Buyer silent → item accepted, the seller is paid" if SILENCE[terms["on_buyer_silence"]] == "accepted"
                else "Buyer silent → item rejected")
    if item["step"] == "objection":
        return "Seller silent → the rejection stands, the item's money goes back to the buyer"
    if item["step"] == "decision":
        return "No decision → rules verdict: " + ("rejected, back to the buyer" if item["failed"]
                                                  else "accepted, the seller is paid")
    return ""
