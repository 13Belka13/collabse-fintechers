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
#  9. The decision is executed 10 business days later unless a side confirms a court claim (UDRP 4(k)); then D stays
#     in escrow until the court or an agreement.
# Money: D stays in escrow (split off at window end, §8.2); the schedule in bank/disputes.py reads held().
import time

from bank import config, db, deals, deciders, escrow, journal, money, snapshots, verifier

NEGOTIATION_BD, FEE_BD, REREVIEW_BD, EXECUTE_BD = 2, 3, 5, 10
OPEN = ("negotiation", "buyer_fee", "seller_fee", "decider", "decided")


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
                  (buyer_id, now - 30 * 86400))["n"]


def get(deal_id: str) -> dict | None:
    from bank import disputes
    return next(iter(disputes.claims(deal_id, "C")), None)


def _set(claim: dict, state: str, actor: str, event: str, **data):
    merged = {**claim["data"], **data}
    with db.immediate():
        db.run("UPDATE claims SET state=?, data_json=?, closed_at=? WHERE id=?",
               (state, db.dumps(merged), time.time() if state in ("executed", "refund", "withdrawn") else None,
                claim["id"]))
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

def agree(deal_id: str, by: str, now: float | None = None) -> dict:
    """The seller agrees in negotiation: D back to the buyer, no fees (by the §8.2 schedule)."""
    c = _claim(deal_id)
    if c["state"] != "negotiation":
        raise ClaimError(409, "agreement is possible during negotiation")
    _set(c, "refund", by, "seller_agreed", outcome="seller_agreed", buyer_share_pct=100)
    return get(deal_id)


def fee_terms(deal_id: str, role: str) -> dict:
    """The fee PaymentInfo a party authorizes (the seller signs its ERC-3009 authorization itself)."""
    from bank import core
    c, deal = _claim(deal_id), deals.get(deal_id)
    payer = core.company(deal["buyer_id"])["wallet_address"] if role == "buyer" else \
        core.seller(deal["seller_id"])["wallet_address"]
    pi = c["data"].get(f"fee_pi_{role}")
    if pi is None:
        pi = escrow.fee_payment_info(deal_id, role, c["data"]["fee"], payer)
        c["data"][f"fee_pi_{role}"] = pi
        db.run("UPDATE claims SET data_json=? WHERE id=?", (db.dumps(c["data"]), c["id"]))
    return pi


def pay_fee(deal_id: str, role: str, by: str, signature: str | None = None, now: float | None = None) -> dict:
    from bank import core
    now = time.time() if now is None else now
    c, deal = _claim(deal_id), deals.get(deal_id)
    want = {"buyer": "buyer_fee", "seller": "seller_fee"}[role]
    if c["state"] != want:
        raise ClaimError(409, f"the {role}'s fee is not due now (claim is {c['state']})")
    pi = fee_terms(deal_id, role)
    if role == "buyer":  # the bank holds the buyer's key; the fee comes from the claim-fee budget reserved at pay
        signature = escrow.buyer_signature(pi, deal["asset"], core.wallet_key(deal["buyer_id"]))
    elif escrow.receive_signer(pi, deal["asset"], signature or "") != pi["payer"]:
        raise ClaimError(400, "the fee authorization is not signed by the seller's wallet")
    tx = escrow.pay_fee(deal_id, pi, signature)
    if role == "buyer":
        _set(c, "seller_fee", by, "buyer_fee_paid", buyer_fee_tx=tx["tx_hash"], seller_fee_until=now + bd(FEE_BD))
    else:
        _set(c, "decider", by, "seller_fee_paid", seller_fee_tx=tx["tx_hash"])
        c = _claim(deal_id)
        sides = {"buyer": core.company(deal["buyer_id"])["ico"], "seller": core.seller(deal["seller_id"])["ico"]}
        deciders.open_stage(c, sides, now)
    return get(deal_id)


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

def decide(deal_id: str, decider_id: str, verdicts: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    c = _claim(deal_id)
    if c["state"] != "decider":
        raise ClaimError(409, "no decision is due")
    case = deciders.decide(c["id"], decider_id, verdicts, now)
    pct = case["upheld"] * 100 // case["of"]
    _set(c, "decided", f"decider:{decider_id}", "decided", buyer_share_pct=pct, upheld=case["upheld"],
         of=case["of"], decided_at=now, execute_after=now + bd(EXECUTE_BD), rereview_until=now + bd(REREVIEW_BD))
    return get(deal_id)


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
        _set(c, "refund", "timer", "seller_fee_not_paid", outcome="for_buyer_without_decider", buyer_share_pct=100)
        _fees_back(deals.get(deal_id), get(deal_id), buyer_back=d["fee"], seller_back=0, decider_gets=0)
    elif c["state"] == "decider":
        try:
            deciders.select(c["id"], now)
            deciders.sample(c["id"], len(c["items"]), now)
            deciders.replace_late(c["id"], now)
        except deciders.DeciderError as e:
            if e.status != 425:  # 425: the round is not out yet — wait
                raise
    elif c["state"] == "decided" and now >= d["execute_after"] and not d.get("court"):
        _execute(deals.get(deal_id), c, now)


def _execute(deal: dict, c: dict, now: float):
    d, pct = c["data"], c["data"]["buyer_share_pct"]
    buyer_back = d["fee"] * pct // 100
    seller_back = d["fee"] * (100 - pct) // 100
    _set(c, "executed", "timer", "executed", buyer_refund=c["amount"] * pct // 100)
    _fees_back(deal, get(deal["deal_id"]), buyer_back=buyer_back, seller_back=seller_back,
               decider_gets=2 * d["fee"] - buyer_back - seller_back)


def _fees_back(deal: dict, c: dict, buyer_back: int, seller_back: int, decider_gets: int):
    """Transfers from the clearing address: the winning shares of the fees back, the rest to the decider."""
    from bank import core
    case = deciders.get(c["id"]) or {}
    decider = next((x for x in deciders.pool() + [deciders.RESERVE_PROVIDER] if x["id"] == case.get("decider")), None)
    for to, amount, what in ((core.company(deal["buyer_id"])["wallet_address"], buyer_back, "fee_back_buyer"),
                             (core.seller(deal["seller_id"])["wallet_address"], seller_back, "fee_back_seller"),
                             (decider and decider["address"], decider_gets, "decider_fee")):
        if to and amount > 0:
            try:
                escrow.transfer_from_clearing(deal["deal_id"], deal["asset"], to, amount, what)
            except Exception as e:  # noqa: BLE001 — journalled; the timer does not move the escrow because of it
                journal.append("bank", "clearing_transfer_failed", {"what": what, "error": repr(e)[:200]},
                               deal_id=deal["deal_id"])


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

