# Rule C: the B guarantee plus a dispute with an external decider (MODEL_V2 §7C). The bank checks form, not
# substance, and executes the decision; it never judges the goods.
#  1. One C claim per deal; after it no new delivery versions. D = the sum due for the admissible items.
#  2. Admissibility at filing, before any fee: per item only a FIELD is named; the bank itself reads that field from
#     its own snapshot taken at delivery (template mapping, §2.1) — no quotes from the buyer. An item is admissible
#     when the extracted value exists, has the comparison type, and differs from the item's value after the same
#     normalization as in predicates. Nothing admissible -> no claim, no fee, the right to C stays until window end.
#  3. Negotiation (2 business days): the seller may agree -> D is refunded (by the §8.2 schedule). Otherwise the
#     buyer pays F within 3 business days, or the claim is withdrawn and D goes to the seller.
#  4. Fees are PAID (captured to the clearing address), not blocked; the seller pays within 3 business days of the
#     buyer, or the decision is for the buyer without a decider (D refunded, the buyer's F returned in full).
#     F = 50 € + 2 € per disputed item, at least 10 % of D, at most 1000 € (demo scale ÷100); ×2 for a buyer with
#     ≥ 3 lost C claims in 30 days.
#  5. Both fees in -> the decider stage (bank/deciders.py). The decider rules on the sampled items; the buyer gets
#     back D × upheld / sampled. Fees: each side gets back its fee times its winning share, the decider the rest.
#  6. Re-review: within 5 business days the loser (share < 50 %) may pay F3 = 2 × the 50 € base; two new deciders
#     decide the same sample, the majority of three per item is final and the 10 days of p. 9 run from it. F3 pays
#     the two new deciders; if the result changed in the requester's favour, F3 goes back to it out of the first
#     decider's pay (the rest of its penalty is under its contract with the bank).
#  9. The decision is executed 10 business days later unless a side confirms a court claim (UDRP 4(k)); then D stays
#     in escrow until the court or an agreement.
# 10. Review limits, not filing limits: at most 2 C claims of one buyer against one seller with a decider at once,
#     and at most max(3, 5 % of the seller's open rule-C deals) from buyers independent of the seller (no common
#     beneficial owner, EvSM). Negotiation and fees run from filing; only the decider stage waits ("queued", FIFO
#     by the time both fees came in). A queued claim is an open C claim: D stays held.
# Money: D stays in escrow (split off at window end, §8.2); the schedule in bank/disputes.py reads held().
import functools
import time

from bank import config, db, deals, deciders, escrow, evsm, journal, money, snapshots, verifier

NEGOTIATION_BD, FEE_BD, REREVIEW_BD, EXECUTE_BD = 2, 3, 5, 10
PAIR_LIMIT, SELLER_MIN, SELLER_PCT = 2, 3, 5
CLEAR_TRIES = 10  # failed sends of one clearing transfer before it waits for a person
FEE_PI_MARGIN_S = 60  # a fee PaymentInfo this close to its preApprovalExpiry is rebuilt before anyone signs it
OPEN = ("negotiation", "buyer_fee", "seller_fee", "queued", "decider", "decided", "rereview")


class ClaimError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def bd(n: float) -> float:
    return n * config.BUSINESS_DAY_S


def _cents(eur: int) -> int:
    return eur * 100 // config.MONEY_DIVISOR


def fee_eur_cents(n_items: int, d_eur_cents: int, buyer_lost_c: int = 0) -> int:
    f = min(_cents(1000), max(_cents(50) + n_items * _cents(2), -(-d_eur_cents // 10)))
    return f * 2 if buyer_lost_c >= 3 else f


def fee_max_eur_cents(count: int, amount_eur_cents: int, buyer_lost_c: int = 0) -> int:
    """F_max reserved from claim_fee_budget at pay (§2): the largest fee any C claim on this deal can cost."""
    return fee_eur_cents(count, amount_eur_cents, buyer_lost_c)


def lost_c(buyer_id: str, now: float) -> int:
    return db.one("SELECT COUNT(*) n FROM claims WHERE buyer_id=? AND rule='C' AND state='executed'"
                  " AND json_extract(data_json,'$.buyer_share_pct') < 50 AND filed_at>=?",
                  (buyer_id, now - config.span(30 * 86400)))["n"]


def get(deal_id: str) -> dict | None:
    from bank import disputes
    return next(iter(disputes.claims(deal_id, "C")), None)


def _deal_locked(fn):
    """The claim's API actions and the timer run under the deal's lock (disputes._locks) and read the claim inside
    it: a fee in flight and a deadline never act on the same claim at once."""
    @functools.wraps(fn)
    def run(deal_id, *a, **kw):
        from bank import disputes
        with disputes._locks[deal_id]:
            return fn(deal_id, *a, **kw)
    return run


def _set(claim: dict, state: str, actor: str, event: str, **data):
    """Compare-and-set: the claim moves only from the state it was read in (never over a newer state)."""
    merged = {**claim["data"], **data}
    with db.immediate():
        if db.run("UPDATE claims SET state=?, data_json=?, closed_at=? WHERE id=? AND state=?",
                  (state, db.dumps(merged), time.time() if state in ("executed", "refund", "withdrawn") else None,
                   claim["id"], claim["state"])).rowcount != 1:
            raise ClaimError(409, f"the claim is no longer {claim['state']}")
        journal.append(actor, f"claim_c.{event}", {"state": state, **data}, deal_id=claim["deal_id"])


# ---- 2. filing and admissibility ----

def admissibility(deal: dict, items: list, fields: dict) -> tuple[list[dict], list[dict]]:
    """(admissible, excluded) — form only: the bank's own extracted source value, same type, and different."""
    src = next((p for p in deal["terms"]["predicates"] if p["op"] == "source"), None)
    ok, out = [], []
    for it in items:
        try:
            v, i = int(it[0]), int(it[1])
        except (TypeError, ValueError, IndexError) as e:
            raise ClaimError(400, "items are [[version, index], ...]") from e
        field = fields.get(f"{v}/{i}")
        row = db.one("SELECT item_json, status, refunded FROM items WHERE deal_id=? AND version=? AND idx=?",
                     (deal["deal_id"], v, i))
        why = None
        mapping = snapshots.TEMPLATES.get(src["template"], {}).get("fields", {}).get(field) if src else None
        snap = snapshots.get(deal["deal_id"], v, i, src["template"]) if src else None
        if row is None or row["status"] != "accepted" or row["refunded"]:
            why = "not an accepted item without a claim"
        elif mapping is None:
            why = "no published field mapping for this field (only B protects it)"
        elif snap is None or snap["status"] != "ok" or field not in snap["fields"]:
            why = "no value for this field in the bank's snapshot"
        else:
            ctype = mapping["type"]
            mine, theirs = db.loads(row["item_json"]).get(field), snap["fields"][field]
            a, b = verifier.norm(mine, ctype), verifier.norm(theirs, ctype)
            if a is None or b is None:
                why = "not the same comparison type"
            elif a == b:
                why = "the source says the same"
            else:
                ok.append({"version": v, "index": i, "field": field, "item_value": mine, "source_value": theirs,
                           "snapshot_hash": snap["body_hash"], "snapshot_at": snap["taken_at"]})
                continue
        out.append({"version": v, "index": i, "field": field, "why": why})
    return ok, out


def file(deal_id: str, items: list, fields: dict, by: str, now: float | None = None) -> dict:
    from bank import disputes
    now = time.time() if now is None else now
    with disputes._locks[deal_id]:
        deal, s = deals.get(deal_id), disputes.get(deal_id)
        if s is None or s["rule"] != "C":
            raise ClaimError(409, "this deal has no rule C")
        if s["voided"] or s["closed_at"] or s["window_end_at"] is None or now >= s["window_end_at"]:
            raise ClaimError(409, "the claim window is not open")
        if get(deal_id):
            raise ClaimError(409, "one C claim per deal")
        ok, excluded = admissibility(deal, items, fields)
        if not ok:
            journal.append(by, "claim_c.inadmissible", {"excluded": excluded}, deal_id=deal_id)
            return {"rule": "C", "claim": None, "admissible": 0, "excluded": excluded,
                    "text": "Nothing admissible: no claim and no fee; the right to C stays until the window ends."}
        d = disputes.item_value(deal, len(ok))
        f = money.from_eur_cents(deal["asset"], fee_eur_cents(len(ok), money.to_eur_cents(deal["asset"], d),
                                                              lost_c(deal["buyer_id"], now)))
        data = {"fee": f, "excluded": excluded, "negotiation_until": now + bd(NEGOTIATION_BD)}
        with db.immediate():
            cid = db.run("INSERT INTO claims (deal_id, rule, buyer_id, seller_id, items_json, reason, amount, state,"
                         " filed_at, by, data_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (deal_id, "C", deal["buyer_id"], deal["seller_id"], db.dumps(ok), "source_differs", d,
                          "negotiation", now, by, db.dumps(data))).lastrowid
            journal.append(by, "claim_c.filed", {"claim_id": cid, "items": len(ok), "excluded": len(excluded),
                                                 "D": d, "fee": f}, deal_id=deal_id)
    _fee_budget(deal, f)
    return {"rule": "C", "claim": cid, "admissible": len(ok), "excluded": excluded, "D": d,
            "D_text": money.fmt(deal["asset"], d), "fee": f, "fee_text": money.fmt(deal["asset"], f)}


def _fee_budget(deal: dict, f: int):
    """The fee reservation made at pay (F_max) shrinks to the actual F (§2)."""
    try:
        deals._module("mandate").set_fee_reservation(deal["deal_id"], money.to_eur_cents(deal["asset"], f))
    except (AttributeError, ValueError):
        pass


# ---- 3–4. negotiation and fees ----

@_deal_locked
def agree(deal_id: str, by: str, now: float | None = None) -> dict:
    """The seller agrees in negotiation: D back to the buyer, no fees (by the §8.2 schedule)."""
    c = _claim(deal_id)
    if c["state"] != "negotiation":
        raise ClaimError(409, "agreement is possible during negotiation")
    _set(c, "refund", by, "seller_agreed", outcome="seller_agreed", buyer_share_pct=100)
    return get(deal_id)


@_deal_locked
def fee_terms(deal_id: str, role: str) -> dict:
    """The fee PaymentInfo a party authorizes (the seller signs its ERC-3009 authorization itself)."""
    from bank import core
    c, deal = _claim(deal_id), deals.get(deal_id)
    payer = core.company(deal["buyer_id"])["wallet_address"] if role == "buyer" else \
        core.seller(deal["seller_id"])["wallet_address"]
    return _fee_pi(c, f"fee_pi_{role}", role, c["data"]["fee"], payer)


def _fee_pi(c: dict, key: str, role: str, amount: int, payer: str) -> dict:
    """The stored fee PaymentInfo, or a new one (next salt) once it is near its preApprovalExpiry and its authorize
    was never sent: an expired one can never lock, and that side would lose by default."""
    deal_id, pi = c["deal_id"], c["data"].get(key)
    if pi is not None and time.time() >= int(pi["preApprovalExpiry"]) - FEE_PI_MARGIN_S and db.one(
            "SELECT 1 FROM onchain_txs WHERE idem_key=? AND status IN ('signed','mined')",
            (f"{deal_id}:{escrow.payment_hash(pi)[2:10]}:fee-authorize",)) is None:
        pi = None
    if pi is None:
        k = db.one("SELECT COUNT(*) n FROM payment_infos WHERE deal_id=? AND purpose=?", (deal_id, f"fee_{role}"))["n"]
        pi = escrow.fee_payment_info(deal_id, role, amount, payer, k=k)
        c["data"][key] = pi
        db.run("UPDATE claims SET data_json=? WHERE id=?", (db.dumps(c["data"]), c["id"]))
    return pi


@_deal_locked
def pay_fee(deal_id: str, role: str, by: str, signature: str | None = None, now: float | None = None) -> dict:
    from bank import core
    now = time.time() if now is None else now
    c, deal = _claim(deal_id), deals.get(deal_id)
    want = {"buyer": "buyer_fee", "seller": "seller_fee"}[role]
    if c["state"] != want:
        raise ClaimError(409, f"the {role}'s fee is not due now (claim is {c['state']})")
    pi = fee_terms(deal_id, role)
    c = _claim(deal_id)  # with the fee PaymentInfo fee_terms stored (or rebuilt)
    if role == "buyer":  # the bank holds the buyer's key; the fee comes from the claim-fee budget reserved at pay
        signature = escrow.buyer_signature(pi, deal["asset"], core.wallet_key(deal["buyer_id"]))
    elif escrow.receive_signer(pi, deal["asset"], signature or "") != pi["payer"]:
        raise ClaimError(400, "the fee authorization is not signed by the seller's wallet")
    tx = escrow.pay_fee(deal_id, pi, signature)
    if role == "buyer":
        _set(c, "seller_fee", by, "buyer_fee_paid", buyer_fee_tx=tx["tx_hash"], seller_fee_until=now + bd(FEE_BD))
    else:
        _set(c, "queued", by, "seller_fee_paid", seller_fee_tx=tx["tx_hash"], fees_in_at=now)
        start_review(_claim(deal_id), now)
    return get(deal_id)


# ---- 10. review limits ----

def _ico(company_id: str) -> str | None:
    from bank import core
    return (core.company(company_id) or {}).get("ico")


def seller_limit(seller_id: str) -> int:
    """max(3, 5 % of the seller's open rule-C deals) claims of independent buyers under review at once."""
    n = db.one("SELECT COUNT(*) n FROM settlements s JOIN deals d ON d.deal_id=s.deal_id WHERE d.seller_id=?"
               " AND s.rule='C' AND s.closed_at IS NULL", (seller_id,))["n"]
    return max(SELLER_MIN, n * SELLER_PCT // 100)


def review_block(c: dict) -> str | None:
    """Why this claim cannot go to a decider now (§7C.10), or None. Claims of buyers related to the seller neither
    count in nor wait for the seller's limit: the seller cannot fill its own queue with stand-in buyers."""
    from bank import core
    under = db.rows("SELECT buyer_id FROM claims WHERE rule='C' AND state='decider' AND seller_id=? AND id<>?",
                    (c["seller_id"], c["id"]))
    if sum(r["buyer_id"] == c["buyer_id"] for r in under) >= PAIR_LIMIT:
        return f"at most {PAIR_LIMIT} C claims of one buyer against one seller are under review at once"
    seller_ico = (core.seller(c["seller_id"]) or {}).get("ico")
    if evsm.related(_ico(c["buyer_id"]), seller_ico):
        return None
    independent = sum(not evsm.related(_ico(r["buyer_id"]), seller_ico) for r in under)
    if independent >= seller_limit(c["seller_id"]):
        return "the seller's review limit for claims of independent buyers is reached"
    return None


def start_review(c: dict, now: float) -> bool:
    """A queued claim goes to the decider stage when the limits allow and no earlier queued claim against the same
    seller could start first (FIFO by the time both fees came in)."""
    from bank import core, disputes
    if c["state"] != "queued":
        return False
    queue = sorted(disputes.claims_of_seller(c["seller_id"], "C", "queued"),
                   key=lambda q: (q["data"].get("fees_in_at", 0), q["id"]))
    for q in queue:
        if q["id"] == c["id"]:
            break
        if review_block(q) is None:
            return False
    why = review_block(c)
    if why:
        if c["data"].get("queue_reason") != why:
            _set(c, "queued", "bank", "review_queued", queue_reason=why)
        return False
    with db.immediate():  # compare-and-set: the API (seller's fee) and the timer may both try to start it
        if db.run("UPDATE claims SET state='decider' WHERE id=? AND state='queued'", (c["id"],)).rowcount != 1:
            return False
        journal.append("bank", "claim_c.review_started", {"state": "decider"}, deal_id=c["deal_id"])
    deal = deals.get(c["deal_id"])
    sides = {"buyer": core.company(deal["buyer_id"])["ico"], "seller": core.seller(deal["seller_id"])["ico"]}
    deciders.open_stage(_claim(c["deal_id"]), sides, now)
    return True


@_deal_locked
def court(deal_id: str, by: str) -> dict:
    """A side confirms it filed a court claim: D stays in escrow until the court or an agreement (§7C.9)."""
    c = _claim(deal_id)
    if c["state"] != "decided":
        raise ClaimError(409, "a court claim stops execution of a decision")
    _set(c, "decided", by, "court_claim", court=True)
    return get(deal_id)


def _claim(deal_id: str) -> dict:
    c = get(deal_id)
    if c is None:
        raise ClaimError(404, "no C claim on this deal")
    return c


# ---- 5. the decision ----

@_deal_locked
def decide(deal_id: str, decider_id: str, verdicts: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    c = _claim(deal_id)
    if c["state"] == "rereview":
        return decide_rereview(deal_id, decider_id, verdicts, now)
    if c["state"] != "decider":
        raise ClaimError(409, "no decision is due")
    case = deciders.decide(c["id"], decider_id, verdicts, now)
    pct = case["upheld"] * 100 // case["of"]
    _set(c, "decided", f"decider:{decider_id}", "decided", buyer_share_pct=pct, upheld=case["upheld"],
         of=case["of"], decided_at=now, execute_after=now + bd(EXECUTE_BD), rereview_until=now + bd(REREVIEW_BD))
    return get(deal_id)


# ---- 6. re-review paid by the loser ----

def rereview_fee(asset: str) -> int:
    return money.from_eur_cents(asset, 2 * _cents(50))


def loser(c: dict) -> str | None:
    pct = c["data"].get("buyer_share_pct")
    return None if pct is None or pct == 50 else ("buyer" if pct < 50 else "seller")


@_deal_locked
def rereview_terms(deal_id: str, role: str) -> dict:
    """The F3 PaymentInfo the loser authorizes (the seller signs it itself), stored before anything is signed."""
    from bank import core
    c, deal = _claim(deal_id), deals.get(deal_id)
    if role != loser(c):
        raise ClaimError(403, "only the side that lost may request a re-review")
    payer = core.company(deal["buyer_id"])["wallet_address"] if role == "buyer" else \
        core.seller(deal["seller_id"])["wallet_address"]
    return _fee_pi(c, "fee_pi_rereview", f"rereview_{role}", rereview_fee(deal["asset"]), payer)


@_deal_locked
def request_rereview(deal_id: str, role: str, by: str, signature: str | None = None,
                     now: float | None = None) -> dict:
    """The loser pays F3 within 5 business days of the decision; the case goes to two new deciders."""
    from bank import core
    now = time.time() if now is None else now
    c, deal = _claim(deal_id), deals.get(deal_id)
    d = c["data"]
    if c["state"] != "decided" or d.get("court") or d.get("rereview") or now > d["rereview_until"]:
        raise ClaimError(409, "a re-review is requested once, within 5 business days of the decision")
    if role != loser(c):
        raise ClaimError(403, "only the side that lost may request a re-review")
    pi = rereview_terms(deal_id, role)
    if role == "buyer":
        signature = escrow.buyer_signature(pi, deal["asset"], core.wallet_key(deal["buyer_id"]))
    elif escrow.receive_signer(pi, deal["asset"], signature or "") != pi["payer"]:
        raise ClaimError(400, "the re-review fee authorization is not signed by the seller's wallet")
    tx = escrow.pay_fee(deal_id, pi, signature)
    _set(c, "rereview", by, "rereview_requested", rereview=role, f3=int(pi["maxAmount"]), f3_tx=tx["tx_hash"],
         fee_pi_rereview=pi, first_pct=d["buyer_share_pct"])
    deciders.open_rereview(c["id"], now)
    return get(deal_id)


@_deal_locked
def decide_rereview(deal_id: str, decider_id: str, verdicts: dict, now: float) -> dict:
    c = _claim(deal_id)
    final = deciders.decide_rereview(c["id"], decider_id, verdicts, now)
    if final:
        pct, first = final["upheld"] * 100 // final["of"], c["data"]["first_pct"]
        won = pct > first if c["data"]["rereview"] == "buyer" else pct < first  # changed in the requester's favour
        _set(c, "decided", f"decider:{decider_id}", "rereview_decided", buyer_share_pct=pct, upheld=final["upheld"],
             of=final["of"], changed=won, decided_at=now, execute_after=now + bd(EXECUTE_BD), rereview_until=now)
    return get(deal_id)


@_deal_locked
def tick(deal_id: str, now: float | None = None) -> None:
    """Deadlines: negotiation end, the buyer's and the seller's fee, the draw and sample, a late decider, and the
    execution 10 business days after the decision."""
    now = time.time() if now is None else now
    c = get(deal_id)
    if c is None or c["state"] not in OPEN:
        return
    d = c["data"]
    if c["state"] == "negotiation" and now >= d["negotiation_until"]:
        _set(c, "buyer_fee", "timer", "negotiation_over", buyer_fee_until=now + bd(FEE_BD))
    elif c["state"] == "buyer_fee" and now >= d["buyer_fee_until"]:
        _set(c, "withdrawn", "timer", "withdrawn", outcome="buyer_fee_not_paid", buyer_share_pct=0)
    elif c["state"] == "seller_fee" and now >= d["seller_fee_until"]:
        _set(c, "refund", "timer", "seller_fee_not_paid", outcome="for_buyer_without_decider", buyer_share_pct=100,
             transfers=_fees_back(deals.get(deal_id), c, buyer_back=d["fee"], seller_back=0, decider_gets=0))
        send_transfers(deal_id)
    elif c["state"] == "queued":
        start_review(c, now)
    elif c["state"] == "decider":
        try:
            deciders.select(c["id"], now)
            deciders.sample(c["id"], len(c["items"]), now)
            deciders.replace_late(c["id"], now)
        except deciders.DeciderError as e:
            if e.status != 425:  # 425: the round is not out yet — wait
                raise
    elif c["state"] == "rereview":
        try:
            deciders.select_rereview(c["id"], now)
            deciders.replace_late_rereview(c["id"], now)
        except deciders.DeciderError as e:
            if e.status != 425:
                raise
    elif c["state"] == "decided" and now >= d["execute_after"] and not d.get("court"):
        _execute(deals.get(deal_id), c, now)


def _execute(deal: dict, c: dict, now: float):
    d, pct = c["data"], c["data"]["buyer_share_pct"]
    buyer_back = d["fee"] * pct // 100
    seller_back = d["fee"] * (100 - pct) // 100
    decider_gets, extra = 2 * d["fee"] - buyer_back - seller_back, []
    if d.get("rereview"):  # F3 pays the two new deciders; a changed result refunds F3 out of the first decider's pay
        from bank import core
        rr = (deciders.get(c["id"]) or {}).get("rereview") or {}
        new = [x for x in deciders.pool() + [deciders.RESERVE_PROVIDER] if x["id"] in (rr.get("deciders") or [])]
        for i, x in enumerate(new):
            extra.append((x["address"], d["f3"] // len(new) + (d["f3"] % len(new) if i == 0 else 0),
                          f"rereview_fee_{i + 1}"))
        if d.get("changed"):
            refund = min(d["f3"], decider_gets)
            decider_gets -= refund
            to = core.company(deal["buyer_id"]) if d["rereview"] == "buyer" else core.seller(deal["seller_id"])
            extra.append((to["wallet_address"], refund, "rereview_fee_back"))
    _set(c, "executed", "timer", "executed", buyer_refund=c["amount"] * pct // 100,
         transfers=_fees_back(deal, c, buyer_back=buyer_back, seller_back=seller_back, decider_gets=decider_gets,
                              extra=extra))
    send_transfers(deal["deal_id"])


def _fees_back(deal: dict, c: dict, buyer_back: int, seller_back: int, decider_gets: int, extra: list = ()) -> list:
    """The transfers from the clearing address: the winning shares of the fees back, the rest to the decider. Written
    down with the state that owes them, before anything is sent (send_transfers)."""
    from bank import core
    case = deciders.get(c["id"]) or {}
    decider = next((x for x in deciders.pool() + [deciders.RESERVE_PROVIDER] if x["id"] == case.get("decider")), None)
    return [{"to": to, "amount": int(amount), "what": what, "done": False, "tries": 0}
            for to, amount, what in ((core.company(deal["buyer_id"])["wallet_address"], buyer_back, "fee_back_buyer"),
                                     (core.seller(deal["seller_id"])["wallet_address"], seller_back, "fee_back_seller"),
                                     (decider and decider["address"], decider_gets, "decider_fee"), *extra)
            if to and amount > 0]


@_deal_locked
def send_transfers(deal_id: str) -> None:
    """Send the written-down transfers not done yet. The timer calls it again (retry_transfers), also after the
    settlement closed; the idem key {deal_id}:clearing:{what} makes a resend safe. Given up after CLEAR_TRIES
    failures with a journalled alarm (e.g. a token that refuses the clearing address: a person must act)."""
    c, deal = get(deal_id), deals.get(deal_id)
    for t in (c["data"].get("transfers") or []):
        if t["done"] or t["tries"] >= CLEAR_TRIES:
            continue
        try:
            escrow.transfer_from_clearing(deal_id, deal["asset"], t["to"], t["amount"], t["what"])
            t["done"] = True
        except Exception as e:  # noqa: BLE001 — journalled and retried; the timer does not move the escrow for it
            t["tries"] += 1
            journal.append("bank", "clearing_transfer_failed" if t["tries"] < CLEAR_TRIES else "clearing_transfer.stuck",
                           {"what": t["what"], "to": t["to"], "amount": t["amount"], "tries": t["tries"],
                            "error": repr(e)[:200]}, deal_id=deal_id)
        db.run("UPDATE claims SET data_json=? WHERE id=?", (db.dumps(c["data"]), c["id"]))


def retry_transfers() -> None:
    """The timer: transfers owed by executed or refunded claims that failed are sent again."""
    for r in db.rows("SELECT deal_id FROM claims WHERE rule='C' AND state IN ('executed','refund') AND EXISTS"
                     " (SELECT 1 FROM json_each(data_json, '$.transfers') WHERE json_extract(value, '$.done')=0"
                     " AND json_extract(value, '$.tries') < ?)", (CLEAR_TRIES,)):
        send_transfers(r["deal_id"])


# ---- the schedule's view ----

def held(deal: dict, s: dict) -> tuple[int, bool]:
    """(the part of D the seller does not get now, whether a C claim is still open)."""
    c = get(deal["deal_id"])
    if c is None or c["state"] == "withdrawn":
        return 0, False
    if c["state"] in OPEN:
        return c["amount"], True
    if c["state"] == "refund":
        return c["amount"], False
    return c["amount"] * c["data"].get("buyer_share_pct", 0) // 100, False  # executed


def view(deal_id: str) -> dict | None:
    c = get(deal_id)
    if c is None:
        return None
    case = deciders.get(c["id"])
    return {**c, "case": deciders.public(case) if case else None}

