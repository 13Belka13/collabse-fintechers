# Deals (SPEC §5.4, §6; MODEL_V2 §2–§5): create -> negotiate (the bank proxies the seller) -> pay -> the seller
# delivers through the bank -> the schedule of the deal's quality rule (bank/disputes.py).
# pay(deal_id) is the only way money leaves a client: the 402 from the seller is a quote, compared with the
# stored offer and the seller's registered wallet; then cached screening + mandate (code, no LLM, no network),
# an atomic budget reservation, the escrow lock, and the seller is told to check the escrow and deliver.
# Refusals are results (codes.refuse), recorded in `decisions` and the journal; nothing is signed before them.
import base64
import importlib
import json
import secrets
import threading
import time
from collections import defaultdict

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bank import chain, codes, config, core, db, escrow, journal, money, timing
from bank.codes import Code

PAYABLE = ("offered", "quoted", "awaiting_human", "refused")
PAID = ("authorized", "delivered", "dispute_c", "settled", "closed")
DEFAULT_DEADLINE_S = 300  # delivery deadline of a task unless the task says otherwise
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)  # one pay() per deal at a time (one process)


class DealError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _module(name: str):
    """Streams A/S modules (mandate, screening, verifier), imported at call time so tests can inject fakes."""
    return importlib.import_module(f"bank.{name}")


def seller_http(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=10)


def get(deal_id: str) -> dict | None:
    deal = db.one("SELECT * FROM deals WHERE deal_id=?", (deal_id,))
    if deal:
        for k in ("task", "offer", "quote", "criteria", "payment_info", "terms"):
            deal[k] = db.loads(deal[f"{k}_json"])
    return deal


def _require(deal_id: str) -> dict:
    deal = get(deal_id)
    if deal is None:
        raise DealError(404, f"unknown deal {deal_id}")
    return deal


def _set(deal_id: str, state: str, event: str, payload: dict | None = None, actor: str = "bank", **fields):
    """One writer per transition: state + fields + journal entry in one transaction."""
    cols = "".join(f", {k}=?" for k in fields)
    with db.immediate():
        db.run(f"UPDATE deals SET state=?, updated_at=?{cols} WHERE deal_id=?",
               (state, time.time(), *fields.values(), deal_id))
        journal.append(actor, event, {"state": state, **(payload or {})}, deal_id=deal_id)


def _prefetch(seller: dict):
    try:
        _module("screening").prefetch(seller["ico"], seller["wallet_address"], seller.get("website_url"))
    except Exception as e:  # noqa: BLE001 — prefetch only warms the cache; pay() fails closed without it
        journal.append("bank", "screening_prefetch_failed", {"ico": seller["ico"], "error": repr(e)[:200]})


def create(buyer_id: str, seller_id: str, mandate_id: str, task, asset: str = "EURC") -> str:
    buyer, seller = core.company(buyer_id), core.seller(seller_id)
    if buyer is None or seller is None:
        raise DealError(404, "unknown buyer or seller")
    owner = db.one("SELECT company_id FROM mandates WHERE mandate_id=?", (mandate_id,))
    if owner is None or owner["company_id"] != buyer_id:  # a mandate spends only its own company's money
        raise DealError(403, f"mandate {mandate_id} does not belong to {buyer_id}")
    if asset not in config.ASSETS or not config.ASSETS[asset]["address"]:
        raise DealError(400, f"asset {asset} is not available")
    task = task if isinstance(task, dict) else {"text": str(task)}
    try:
        terms = task_terms(task)
    except (ValueError, ValidationError) as e:
        raise DealError(400, f"task terms are not valid: {e}") from e
    deal_id = "d-" + secrets.token_hex(5)
    with db.immediate():
        db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, mandate_id, task_json, criteria_json, asset, state,"
               " created_at) VALUES (?,?,?,?,?,?,?,'negotiating',?)",
               (deal_id, buyer_id, seller_id, mandate_id, db.dumps(task), db.dumps(terms), asset, time.time()))
        journal.append("agent:buyer", "deal_created", {"seller_id": seller_id, "mandate_id": mandate_id,
                                                       "asset": asset, "task": task}, deal_id=deal_id)
    _prefetch(seller)  # counterparty check runs during negotiation, never on the money path
    return deal_id


class Criteria(BaseModel):
    """Acceptance criteria from the human's task (SPEC §7 AcceptanceCriteria), validated: the verifier pays on these."""
    model_config = ConfigDict(extra="ignore")
    count: int = Field(20, ge=1, le=100)
    city: str = Field("Brno", min_length=1, max_length=80)
    min_rating_exclusive: float = Field(4.0, ge=0, le=5)
    category_keywords: list[str] = Field(default_factory=lambda: ["sklad", "warehouse", "storage", "logist"],
                                         min_length=1, max_length=20)
    require_website: bool = True
    registry_check: bool = True  # registry_active on IČO; False for data without IČO (Google Maps places)


POSTCODE_PREFIX = {"brno": "6", "praha": "1", "prague": "1"}


def buyer_criteria(task: dict | None, strict: bool = False) -> dict:
    """Criteria from the buyer's task; anything invalid falls back to the demo defaults (never to the seller's).
    strict (a deal's binding terms): invalid criteria raise ValidationError; defaults only when none were given."""
    raw = (task or {}).get("criteria") or {}
    try:
        c = Criteria.model_validate(raw).model_dump()
    except ValidationError:
        if strict:
            raise
        c = Criteria().model_dump()
    # Brno postcodes are 6xx xx, Prague ones 1xx xx; Google Maps names districts ("Brno-střed", "Praha 5")
    c["postcode_prefix"] = POSTCODE_PREFIX.get(c["city"].strip().lower())
    return c


def task_terms(task: dict) -> dict:
    """The acceptance terms of the task (§2.1): predicates (AND only), count, category, delivery deadline, and the
    hash a person of the buyer confirms. Explicit task["terms"] win; otherwise the agent's criteria are turned into
    predicates by code (verifier.criteria_predicates)."""
    from bank import receipts, verifier
    raw = task.get("terms")
    if raw:
        terms = {"category": str(raw.get("category") or "data_purchase"), "count": int(raw["count"]),
                 "predicates": verifier.validate(list(raw["predicates"])),
                 "deadline_s": int(raw.get("deadline_s") or DEFAULT_DEADLINE_S)}
    else:
        c = buyer_criteria(task, strict=True)  # the verifier pays on these: never swapped for the demo defaults
        terms = {"category": "data_purchase", "count": c["count"], "predicates": verifier.criteria_predicates(c),
                 "deadline_s": DEFAULT_DEADLINE_S}
    if task.get("task_id"):  # one task, bought from several sellers, shares `unique`; another task does not
        terms["task_id"] = str(task["task_id"])[:64]
    if terms["count"] < 1 or terms["deadline_s"] < 1:
        raise ValueError("count and deadline must be positive")
    return {**terms, "hash": receipts.terms_hash(terms)}


def offer_view(offer: dict, asset: str) -> dict:
    """What the buyer agent sees of an offer: ids and terms, no addresses."""
    return {"offer_id": offer["offer_id"], "items_spec": offer.get("items_spec"), "count": offer.get("count"),
            "asset": asset, "price": money.fmt(asset, int(offer["prices"][asset])),
            "price_eur_cents": money.to_eur_cents(asset, int(offer["prices"][asset])),  # to compare with the task budget
            "deadline_s": offer.get("deadline_s"), "rule": offer.get("rule"), "b_pct": offer.get("b_pct"),
            "window_s": offer.get("window_s"), "predicates": offer.get("predicates")}


def negotiate(deal_id: str, message: str) -> dict:
    """Under the same per-deal lock as pay(): an offer can never change between the decision and the lock."""
    with _locks[deal_id]:
        return _negotiate(deal_id, message)


def _negotiate(deal_id: str, message: str) -> dict:
    deal = _require(deal_id)
    if deal["state"] in PAID:
        raise DealError(409, f"deal is {deal['state']}")
    seller, buyer = core.seller(deal["seller_id"]), core.company(deal["buyer_id"])
    journal.append("agent:buyer", "buyer_message", {"message": message}, deal_id=deal_id)
    body_out = {"deal_id": deal_id, "seller_id": deal["seller_id"], "buyer_ico": buyer["ico"], "task": deal["task"],
                "terms": deal["criteria"], "requirements": _requirements(deal), "message": message}
    try:  # the seller's model answers inside its own budget (config.LLM_BUDGET_S), shorter than this timeout
        with timing.timed("negotiate_proxy", deal_id):
            with seller_http(seller["base_url"]) as http:
                wait = {"timeout": config.NEGOTIATE_TIMEOUT_S} if isinstance(http, httpx.Client) else {}  # not in-process
                r = http.post("/negotiate", json=body_out, **wait)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict):
            raise ValueError("the seller's answer is not a JSON object")
    except Exception as e:  # noqa: BLE001 — httpx (or in-process seller) error: a clear 502/504, never a 500
        timeout = "Timeout" in type(e).__name__
        journal.append("bank", "seller_unavailable", {"step": "negotiate", "error": f"{type(e).__name__}: {str(e)[:200]}"},
                       deal_id=deal_id)
        raise DealError(504 if timeout else 502, (f"the seller did not answer within {config.NEGOTIATE_TIMEOUT_S:g} s"
                                                  if timeout else "the seller could not be reached") +
                        "; the offer is unchanged, try again") from e
    reply, offer = str(body.get("reply_text", "")), body.get("offer")
    # whatever the seller writes is data, never an instruction to the bank or the agent
    journal.append("agent:seller", "seller_reply", {"reply_text": reply, "offer": offer, "source": "untrusted:seller"},
                   deal_id=deal_id)
    out = {"reply_text": reply, "offer": None}
    if offer and offer.get("offer_id") and deal["asset"] in (offer.get("prices") or {}):
        # acceptance terms come from the buyer's confirmed task; the seller may only add predicates (checked at pay)
        terms = deal["criteria"]
        if offer.get("count") is not None and int(offer["count"]) != terms["count"]:
            journal.append("bank", "offer_ignored", {"reason": "count_differs_from_task", "offer_count": offer["count"],
                                                     "task_count": terms["count"]}, deal_id=deal_id)
            return out
        amount = int(offer["prices"][deal["asset"]])
        _set(deal_id, "offered", "offer_stored", {"offer_id": offer["offer_id"], "amount": amount,
                                                  "rule": offer.get("rule"), "source": "untrusted:seller"},
             offer_json=db.dumps(offer), amount=amount,
             confirmed_by=None)  # a human confirmed the previous offer, not this one
        out["offer"] = offer_view(offer, deal["asset"])
    _prefetch(seller)
    return out


def _requirements(deal: dict) -> dict:
    mandate = _module("mandate")
    return mandate.requirements(deal["mandate_id"], deal) if hasattr(mandate, "requirements") else {}


def _decode_quote(r: httpx.Response) -> dict:
    header = r.headers.get("payment-required")
    return json.loads(base64.b64decode(header)) if header else r.json()


def fetch_quote(deal: dict, seller: dict) -> tuple[dict | None, dict | None]:
    """GET the seller's 402 for the offer and compare it with the stored offer and the registered wallet.
    Returns (quote, None) or (None, refusal). payTo first (N03), then amount (N04)."""
    asset, offer = deal["asset"], deal["offer"]
    with seller_http(seller["base_url"]) as http:
        r = http.get(f"/order/{offer['offer_id']}")
    if r.status_code != 402:
        return None, codes.refuse(Code.QUOTE_MISMATCH, [codes.reason(
            "quote_unavailable", True, f"The seller answered HTTP {r.status_code} instead of a 402 quote.")])
    try:
        accepts = _decode_quote(r)["accepts"]
    except (ValueError, KeyError, TypeError):
        return None, codes.refuse(Code.QUOTE_MISMATCH, [codes.reason("quote_unreadable", True,
                                                                     "The seller's 402 quote could not be read.")])
    address = config.ASSETS[asset]["address"].lower()
    accept = next((a for a in accepts if str(a.get("asset", "")).lower() == address and a.get("scheme") == "exact"
                   and a.get("network") == config.NETWORK), None)
    if accept is None:
        return None, codes.refuse(Code.QUOTE_MISMATCH, [codes.reason(
            "asset_not_quoted", True, f"The seller's 402 does not offer {asset} on Base Sepolia.")])
    registered = seller["wallet_address"]
    if str(accept.get("payTo", "")).lower() != registered.lower():
        return None, codes.refuse(Code.COUNTERPARTY_NOT_IN_SCOPE, [codes.reason(
            "payto_not_registered", True,
            f"The 402 asks to pay {accept.get('payTo')}, which is not the account registered for {seller['name'].rstrip('.')}.",
            None, quoted_pay_to=accept.get("payTo"), registered_pay_to=registered)])
    agreed = int(offer["prices"][asset])
    if int(accept.get("amount", -1)) != agreed:
        return None, codes.refuse(Code.QUOTE_MISMATCH, [codes.reason(
            "amount_not_offer", True,
            f"The 402 asks {money.fmt(asset, int(accept.get('amount', 0)))}, the agreed offer is "
            f"{money.fmt(asset, agreed)}.", None, quoted=str(accept.get("amount")), agreed=str(agreed))])
    quote = {"offer_id": offer["offer_id"], "asset": asset, "asset_address": accept["asset"],
             "amount": agreed, "pay_to": accept["payTo"], "network": accept["network"], "scheme": accept["scheme"],
             "extra": accept.get("extra"), "fetched_at": time.time()}
    return quote, None


def _record(deal_id: str, decision: dict, ms: float, kind: str = "pay"):
    reasons = decision.get("reasons", [])
    full = (decision.get("explanation") or "") + "".join(f"\n- [{r['code']}] {r['text']}" for r in reasons)
    db.run("INSERT INTO decisions (deal_id, kind, outcome, codes_json, evidence_json, explanation_client,"
           " explanation_compliance, ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
           (deal_id, kind, decision["status"], db.dumps([decision.get("code")] + [r["code"] for r in reasons]),
            db.dumps(reasons), codes.client_view({**decision, "deal_id": deal_id})["explanation"], full.strip(),
            ms, time.time()))


def _finish_refusal(deal_id: str, decision: dict, t0: float) -> dict:
    decision = {**decision, "deal_id": deal_id}
    if not decision.get("explanation"):
        decision["explanation"] = " ".join(r["text"] for r in decision.get("reasons", []) if r.get("public"))
    state = "awaiting_human" if decision["status"] == "requires_user_action" else "refused"
    _record(deal_id, decision, (time.perf_counter() - t0) * 1000)
    _set(deal_id, state, "pay_" + decision["status"], {"code": decision.get("code"),
                                                        "reasons": decision.get("reasons", []), "money_moved": False})
    return decision


def _decide(deal: dict, seller: dict, quote: dict) -> dict:
    """Mandate + cached counterparty check. Pure code and SQLite (target p95 <= 50 ms)."""
    try:
        screening, mandate = _module("screening"), _module("mandate")
    except ImportError as e:
        return codes.refuse(Code.UNDER_REVIEW, [codes.reason("check_unavailable", False, f"{e.name} is not available")])
    result = screening.cached(seller["ico"])
    if result is None:  # fail closed: no prefetched check, no payment
        _prefetch(seller)
        return codes.refuse(Code.UNDER_REVIEW, [codes.reason(
            "screening_not_ready", False, "The counterparty check has not finished yet.")])
    asset = deal["asset"]
    offer = {**deal["offer"], "asset": asset, "amount": int(deal["offer"]["prices"][asset]),  # mandate's offer shape
             "price": money.fmt(asset, int(deal["offer"]["prices"][asset]))}
    view = {**deal, "offer": offer, "eur_cents": money.to_eur_cents(asset, int(quote["amount"])),
            "seller": {"seller_id": seller["seller_id"], "ico": seller["ico"], "wallet": seller["wallet_address"],
                       "category": seller.get("category")}}
    return mandate.decide(deal["mandate_id"], view, quote, result)


def _wallet_refusal(deal: dict, seller: dict) -> dict | None:
    """Bank-side freeze (every asset) and, for tCZK, the token's own allowlist/freeze — checked before anything is
    reserved or signed, so a lock can never be made that the escrow could not later capture or return."""
    from bank import tczk
    payer, payee = core.company(deal["buyer_id"])["wallet_address"], seller["wallet_address"]
    if tczk.bank_frozen(payer) or tczk.bank_frozen(payee):
        return codes.refuse(Code.UNDER_REVIEW, [codes.reason("wallet_frozen_at_bank", False,
                                                             "A wallet in this payment is frozen at the bank.")])
    if deal["asset"] == "tCZK":
        if not tczk.is_allowed(payee):
            return codes.refuse(Code.COUNTERPARTY_NOT_IN_SCOPE, [codes.reason(
                "payee_not_on_tczk_allowlist", True, f"{seller['name']} cannot receive tCZK (not a bank client account).")])
        if not tczk.is_allowed(payer):
            return codes.refuse(Code.CATEGORY_NOT_IN_SCOPE, [codes.reason(
                "payer_not_on_tczk_allowlist", True, "This wallet is not enabled for tCZK.")])
        if tczk.is_frozen(payer) or tczk.is_frozen(payee):
            return codes.refuse(Code.UNDER_REVIEW, [codes.reason("wallet_frozen_on_chain", False,
                                                                 "A wallet in this payment is frozen on chain.")])
    return None


def _reserve(deal: dict, eur_cents: int) -> dict:
    held = db.one("SELECT state, amount FROM reservations WHERE mandate_id=? AND deal_id=?",
                  (deal["mandate_id"], deal["deal_id"]))
    if held and held["state"] == "reserved":
        if held["amount"] == eur_cents:  # a retry after an ambiguous authorize: keep the same reservation
            return codes.ok()
        _module("mandate").release(deal["deal_id"])  # the price changed before anything was locked: re-reserve
    return _module("mandate").reserve(deal["mandate_id"], deal["deal_id"], eur_cents)


def pay(deal_id: str) -> dict:
    """The money path. Returns a Decision (status ok/refused/requires_user_action), never moves money twice."""
    with _locks[deal_id]:
        deal = _require(deal_id)
        if deal["state"] in PAID or deal["state"] == "closed":
            return _already_paid(deal)
        if deal["state"] not in PAYABLE or not deal["offer"]:
            raise DealError(409, "no offer to pay yet: negotiate first")
        seller = core.seller(deal["seller_id"])
        t0 = time.perf_counter()
        with timing.timed("quote", deal_id):
            quote, refusal = fetch_quote(deal, seller)
        if refusal:
            return _finish_refusal(deal_id, refusal, t0)
        _set(deal_id, "quoted", "quote_accepted", {"quote": quote}, quote_json=db.dumps(quote), amount=quote["amount"])
        deal = _require(deal_id)
        t0 = time.perf_counter()
        with timing.timed("decision", deal_id):
            decision = _fix_terms(deal, money.to_eur_cents(deal["asset"], quote["amount"]))
            if decision["status"] == "ok":
                marks = decision.get("marks", [])
                deal = _require(deal_id)
                decision = _decide(deal, seller, quote)
                decision["marks"] = marks + decision.get("marks", [])
            if decision["status"] == "ok":
                decision = _wallet_refusal(deal, seller) or decision
            if decision["status"] == "ok":
                marks = decision.get("marks", [])
                decision = _reserve_b_slot(_require(deal_id))
                decision["marks"] = marks + decision.get("marks", [])
            if decision["status"] == "ok":
                marks = decision.get("marks", [])
                decision = _reserve(deal, money.to_eur_cents(deal["asset"], quote["amount"]))
                decision["marks"] = marks + decision.get("marks", [])
        if decision["status"] != "ok":
            return _finish_refusal(deal_id, decision, t0)
        _reserve_fee(_require(deal_id))
        _refresh_payment_info(deal, quote)
        return _authorize_and_notify(_require(deal_id), seller, (time.perf_counter() - t0) * 1000,
                                     decision.get("marks", []))


TIERS = ("T0", "T1", "T2")
RULE_TIERS = {"A": "T0", "B": "T1", "C": "T2"}  # the lowest tier where a rule exists (§4)


def tier_of(eur_cents: int) -> str:
    return "T0" if eur_cents <= config.TIER_T0_MAX else "T1" if eur_cents <= config.TIER_T1_MAX else "T2"


def _terms_refusal(reason: str, text: str, **evidence) -> dict:
    return codes.refuse(Code.TERMS_MISMATCH, [codes.reason(reason, True, text, None, **evidence)])


def _fix_terms(deal: dict, eur_cents: int) -> dict:
    """Check the offer against the confirmed task terms (§2.1, §3) and fix the deal's terms for the escrow: the
    confirmed predicates verbatim (JCS), a rule A < B < C allowed in the tier, a window within the tier's bounds,
    a delivery deadline no later than the task's, the tariff, and the B claim slot reserved now (§7B). Written in
    one transaction with the slot check, so parallel pays cannot take the same slot."""
    from bank import disputes, verifier
    offer, task = deal["offer"], deal["criteria"]
    missing = verifier.verbatim_missing(task["predicates"], offer.get("predicates") or [])
    if missing:
        return _terms_refusal("predicates_not_verbatim", f"The offer drops or changes {len(missing)} of the confirmed "
                              "acceptance predicates.", missing=missing)
    try:
        verifier.validate(offer.get("predicates") or [])
    except verifier.PredicateError as e:
        return _terms_refusal("offer_predicates_invalid", f"The seller's predicates are not valid: {e}")
    rule = offer.get("rule")
    if rule not in disputes.RULES:
        return _terms_refusal("rule_unknown", "The offer names no quality rule A, B or C.")
    b_pct = int(offer.get("b_pct") or 0) if rule in ("B", "C") else 0
    if rule in ("B", "C") and b_pct not in disputes.B_PCTS:
        return _terms_refusal("b_pct_unknown", "The B guarantee must be 5, 10 or 20 %.")
    tier = max(tier_of(eur_cents), RULE_TIERS[rule], offer.get("tier") if offer.get("tier") in TIERS else "T0",
               key=TIERS.index)
    if rule == "C" and not any(p["op"] == "source" for p in task["predicates"]):
        return _terms_refusal("c_needs_source", "Rule C needs a source template in the confirmed task terms.")
    window = 0.0
    if rule != "A":
        lo, hi = config.WINDOW_S[tier]
        window = float(offer.get("window_s") or 0)
        if not lo <= window <= hi:
            return _terms_refusal("window_out_of_bounds", f"The claim window must be {lo:g} to {hi:g} s in {tier}.")
    deadline = int(offer.get("deadline_s") or 0)
    if not 0 < deadline <= int(task["deadline_s"]):
        return _terms_refusal("deadline_after_task", "The delivery deadline is later than the task's.")
    terms = {"rule": rule, "b_pct": b_pct, "tier": tier, "window_s": window, "deadline_s": deadline,
             "fee_bps": config.TARIFF_BPS[(tier, rule)], "count": int(task["count"]),
             "predicates": offer["predicates"], "task_hash": task["hash"], "category": task["category"],
             "b_slot": 0}
    marks = []
    with db.immediate():
        mandate = _module("mandate")
        if hasattr(mandate, "protection"):  # §2: the floor by tier_sum, require B / C, unprotected caps, fee budget
            floor = mandate.protection(deal["mandate_id"], deal, terms, eur_cents)
            if floor["status"] != "ok":
                return floor
            marks += floor.get("marks", [])
        db.run("UPDATE deals SET terms_json=?, updated_at=? WHERE deal_id=?",
               (db.dumps(terms), time.time(), deal["deal_id"]))
    return codes.ok(marks=marks)


def _reserve_b_slot(deal: dict) -> dict:
    """§7B: a B (or C) deal takes a claim slot at pay — after the mandate and the counterparty checks, so a refusal
    is never hidden behind a slot question. Check and write in one transaction: parallel pays cannot share a slot."""
    from bank import disputes
    terms = deal["terms"]
    if terms["rule"] not in ("B", "C"):
        return codes.ok()
    with db.immediate():
        avail = disputes.b_availability(deal["buyer_id"], deal["seller_id"], except_deal=deal["deal_id"])
        marks = []
        if avail["available"]:
            terms["b_slot"] = 1
        elif not deal.get("confirmed_by"):
            return codes.refuse(Code.CONFIRMATION_REQUIRED, [codes.reason(
                avail["reason"], True, avail["text"] + " A person can confirm the deal as B without claims.")])
        else:
            terms["b_slot"] = 0
            marks.append("B without claims (confirmed by a person)")
        db.run("UPDATE deals SET terms_json=?, updated_at=? WHERE deal_id=?",
               (db.dumps(terms), time.time(), deal["deal_id"]))
    return codes.ok(marks=marks)


def _reserve_fee(deal: dict):
    """The largest C fee is reserved from the claim-fee budget with the deal's own reservation (§2)."""
    cents_ = (deal.get("terms") or {}).get("fee_reserved_eur_cents") or 0
    mandate = _module("mandate")
    if cents_ and hasattr(mandate, "reserve_fee"):
        mandate.reserve_fee(deal["mandate_id"], deal["deal_id"], cents_)


def _refresh_payment_info(deal: dict, quote: dict):
    """A stored PaymentInfo is reused only while it matches the accepted quote and can still be authorized.
    Otherwise it is dropped — after the chain shows it never locked money (or after that lock is returned)."""
    pi, deal_id = deal["payment_info"], deal["deal_id"]
    if not pi:
        return
    same = (int(pi["maxAmount"]) == int(quote["amount"]) and pi["receiver"].lower() == quote["pay_to"].lower()
            and pi["token"].lower() == config.ASSETS[deal["asset"]]["address"].lower())
    fresh = time.time() < int(pi["preApprovalExpiry"]) - 5
    if same and (fresh or escrow.authorized_tx(deal_id)):
        return  # authorize() reuses it (or reports the tx that already landed)
    pending = db.one("SELECT status FROM onchain_txs WHERE idem_key=?", (escrow.idem_key(deal_id, "authorize"),))
    if pending and pending["status"] == "signed" and fresh:
        raise DealError(503, "an earlier escrow authorize is still pending; call pay again shortly")
    st = escrow.state(deal_id)
    if st["capturable"] > 0 or st["collected"]:
        if same:
            return
        escrow.void(deal_id)  # an old lock with other terms: give it back before locking anew
        journal.append("bank", "old_lock_returned", {"payment_hash": deal["payment_hash"]}, deal_id=deal_id)
    db.run("UPDATE deals SET payment_info_json=NULL, payment_hash=NULL, pi_n=pi_n+1, updated_at=? WHERE deal_id=?",
           (time.time(), deal_id))  # the next PaymentInfo gets the next salt keccak(deal_id ‖ n+1)
    journal.append("bank", "payment_info_dropped", {"reason": "terms_changed" if not same else "expired",
                                                    "payment_hash": deal["payment_hash"]}, deal_id=deal_id)


def _authorize_and_notify(deal: dict, seller: dict, decision_ms: float, marks: list | None = None) -> dict:
    deal_id, mandate = deal["deal_id"], _module("mandate")
    try:
        tx = escrow.authorize(deal_id)
    except (chain.TxReverted, escrow.EscrowError) as e:  # nothing locked: free the budget
        mandate.release(deal_id)
        journal.append("bank", "authorize_failed", {"error": str(e), "money_moved": False}, deal_id=deal_id)
        raise DealError(502, f"escrow authorize failed: {e}") from e
    except Exception as e:  # ambiguous (timeout, RPC): the tx may land — keep the reservation, retry pay()
        journal.append("bank", "authorize_pending", {"error": repr(e)[:200]}, deal_id=deal_id)
        raise DealError(503, "escrow authorize pending, call pay again (same idempotency key)") from e
    amount, asset = int(deal["amount"]), deal["asset"]
    _set(deal_id, "authorized", "escrow_locked", {"tx_hash": tx["tx_hash"], "link": tx["link"], "amount": amount})
    _module("disputes").open_(deal_id, deal["terms"])
    marks = marks or []
    decision = codes.ok(deal_id=deal_id, marks=marks, explanation=(
        f"{money.fmt(asset, amount)} locked in escrow for {seller['name']}: {tx['link']}. "
        "The seller delivers; the bank pays per accepted item and returns the rest."
        + "".join(f" [{m}]" for m in marks)))
    _record(deal_id, decision, decision_ms)
    ack = notify_seller(deal_id)
    if ack is False:  # the seller saw something wrong in the escrow: give the money back
        return _return_lock(deal_id, decision_ms)
    return decision


def _return_lock(deal_id: str, decision_ms: float = 0.0) -> dict:
    escrow.void(deal_id)
    _module("mandate").release(deal_id)
    _set(deal_id, "closed", "escrow_returned", {"reason": "seller_rejected_escrow"})
    refusal = codes.refuse(Code.QUOTE_MISMATCH, [codes.reason(
        "seller_rejected_escrow", True, "The seller did not accept the escrow terms; the lock was returned.")],
        deal_id=deal_id)
    _record(deal_id, refusal, decision_ms)
    return refusal


def _already_paid(deal: dict) -> dict:
    """pay() again on a deal past the lock never locks twice. If the seller has not delivered yet (its notice was
    lost or it was down), the notice is sent again — that is the retry path for a stuck 'authorized' deal."""
    deal_id, state = deal["deal_id"], deal["state"]
    if state == "authorized" and db.one("SELECT 1 FROM delivery_attempts WHERE deal_id=? AND accepted=1",
                                        (deal_id,)) is None:
        ack = notify_seller(deal_id)
        if ack is False:
            return _return_lock(deal_id)
        return codes.ok(deal_id=deal_id, explanation="The funds are locked in escrow; the seller was asked again to "
                        "deliver." if ack else "The funds are locked in escrow; the seller is not reachable yet — "
                        "call pay again later.")
    if state in ("closed", "settled"):
        return codes.ok(deal_id=deal_id, explanation="This deal is closed; its escrow lock was settled or returned.")
    return codes.ok(deal_id=deal_id, explanation=f"Already paid ({state}): the funds are in escrow for this deal.")


def notify_seller(deal_id: str, kind: str = "deal", amount: int | None = None) -> bool | None:
    """Tell the seller the escrow is locked (kind deal, or split_off: the disputed sum D locked again, §8.2).
    True = accepted, False = rejected, None = not reached (retry later)."""
    deal = _require(deal_id)
    seller = core.seller(deal["seller_id"])
    terms = deal["terms"] or {}
    body = {"deal_id": deal_id, "asset": deal["asset"], "payment_info": deal["payment_info"],
            "payment_hash": deal["payment_hash"], "n": int(deal["pi_n"] or 0), "kind": kind,
            "tier": terms.get("tier"), "rule": terms.get("rule"),
            "amount": int(amount if amount is not None else deal["amount"]),
            "tx_hash": (db.one("SELECT tx_hash FROM onchain_txs WHERE idem_key=?", (escrow.idem_key(deal_id, "authorize"),)) or {}).get("tx_hash")}
    try:
        with timing.timed("notify_seller", deal_id):
            with seller_http(seller["base_url"]) as http:
                r = http.post(f"/order/{deal['offer']['offer_id']}/escrow", json=body,
                              headers={"x-bank-token": config.BANK_TO_SELLER_TOKEN})
    except httpx.HTTPError as e:
        journal.append("bank", "seller_not_notified", {"error": repr(e)[:200]}, deal_id=deal_id)
        return None
    detail = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("detail")
    journal.append("agent:seller", "seller_escrow_answer", {"status": r.status_code, "detail": detail,
                                                            "source": "untrusted:seller"}, deal_id=deal_id)
    if r.status_code == 200:
        return True
    return False if r.status_code in (404, 409) else None


def lookups() -> dict:
    """Registry and source lookups for delivery autocodes (screening cache; snapshots for rule C)."""
    from bank import snapshots, verifier
    out = {"registry": verifier.registry_record, "snapshot": getattr(snapshots, "for_item", None)}
    return {k: v for k, v in out.items() if v}


def record_delivery(deal_id: str, payload: dict, now: float | None = None) -> dict:
    """Seller -> bank: a signed manifest and its items (§5.2). The bank recomputes root and count, runs the
    autocodes and answers the seller at once; the money follows in the background (auto_settle)."""
    from bank import deadlines, disputes, receipts
    now = time.time() if now is None else now
    with disputes._locks[deal_id]:  # not the pay() lock: the seller may deliver while pay() still waits for its ack
        deal = _require(deal_id)
        s = disputes.get(deal_id)
        if deal["state"] not in ("authorized", "delivered") or s is None:
            raise DealError(409, f"deal is {deal['state']}, not waiting for a delivery")
        if s["voided"] or s["closed_at"]:
            raise DealError(409, "the deal is settled: no new versions after the final void")
        if any(c["rule"] == "C" for c in s["claims"]):
            raise DealError(409, "a C claim was filed: no new versions are accepted (§7C.1)")
        if now > deadlines.delivery_deadline(s, now):  # moved by the time items waited for a registry (§5.1)
            raise DealError(409, "the delivery deadline has passed")
        try:
            out = receipts.receive(deal, payload, now=now, **lookups())
        except receipts.Refused as e:
            raise DealError(e.status, str(e)) from e
        if out["accepted"]:
            disputes.delivered(deal_id, now=now)
            if deal["state"] == "authorized":
                _set(deal_id, "delivered", "delivery_received", {"version": out["version"],
                                                                 "accepted_items": out["accepted_items"],
                                                                 "source": "untrusted:seller"}, actor="agent:seller")
    return out


def fetch_delivery(deal_id: str) -> dict:
    """Buyer: only the accepted items, with inclusion proofs against the seller-signed roots (§5.2 p. 5)."""
    from bank import receipts
    _require(deal_id)
    return receipts.fetch(deal_id)


def auto_settle(deal_id: str):
    """After a delivery: let the money follow the deal's rule (bank/disputes.py). A background task: a failure is
    journalled and the timer retries."""
    try:
        return _module("disputes").advance(deal_id)
    except Exception as e:  # noqa: BLE001
        journal.append("bank", "settlement_failed", {"error": repr(e)[:300]}, deal_id=deal_id)
        return None


# ---- human actions (bank UI, HUMAN_TOKEN): the agent can never call these ----

def asks_human(deal: dict, code: str) -> bool:
    """The bank is waiting for a person on this deal: the agent asked to pay and the bank's last answer was `code`
    (CONFIRMATION_REQUIRED or VAT_LIABILITY_RISK). Only then may a person's action start pay()."""
    if deal["state"] != "awaiting_human":
        return False
    dec = db.one("SELECT codes_json FROM decisions WHERE deal_id=? AND kind='pay' ORDER BY id DESC LIMIT 1",
                 (deal["deal_id"],))
    return dec is not None and (db.loads(dec["codes_json"]) or [None])[0] == code


def confirm(deal_id: str, by: str) -> dict:
    """A human approves a payment above the mandate's confirmation threshold, then the bank pays it. Only when the
    bank asked for exactly this confirmation: a person cannot start a payment the agent never asked for."""
    with _locks[deal_id]:
        deal = _require(deal_id)
        if not asks_human(deal, Code.CONFIRMATION_REQUIRED):
            raise DealError(409, f"no confirmation is pending for this deal (it is {deal['state']})")
        _set(deal_id, deal["state"], "payment_confirmed", {"by": by}, actor=by, confirmed_by=by)
    return pay(deal_id)


def decline(deal_id: str, by: str) -> dict:
    """A human declines a payment the bank asked about. Nothing was reserved or signed before the question, so
    nothing moves; a new pay() would ask a person again."""
    with _locks[deal_id]:
        deal = _require(deal_id)
        if deal["state"] != "awaiting_human":
            raise DealError(409, f"deal is {deal['state']}")
        _set(deal_id, "refused", "payment_declined", {"by": by, "money_moved": False}, actor=by)
    return {"status": "refused", "deal_id": deal_id, "money_moved": False,
            "explanation": "A person declined this payment. No money moved."}


def ack_vat(deal_id: str, by: str) -> dict:
    """A human accepts the §109 VAT risk for this seller and asset once (new mandate version), then the bank pays.
    Only when the bank's last answer on this deal was the VAT question."""
    deal = _require(deal_id)
    if not asks_human(deal, Code.VAT_LIABILITY_RISK):
        raise DealError(409, f"no VAT question is pending for this deal (it is {deal['state']})")
    seller = core.seller(deal["seller_id"])
    version = _module("mandate").acknowledge_vat(deal["mandate_id"], seller["ico"], deal["asset"], by)
    journal.append(by, "vat_risk_acknowledged", {"seller_ico": seller["ico"], "asset": deal["asset"],
                                                "mandate_version": version}, deal_id=deal_id)
    return pay(deal_id)


def statement(mandate_id: str) -> dict:
    """Mandate summary from the reservations table (the only truth about the budget, SPEC §6.2)."""
    sums = {r["state"]: r["cents"] for r in db.rows(
        "SELECT state, COALESCE(SUM(amount),0) cents FROM reservations WHERE mandate_id=? GROUP BY state", (mandate_id,))}
    deals = db.rows("SELECT deal_id, seller_id, asset, amount, state, updated_at FROM deals WHERE mandate_id=?"
                    " ORDER BY created_at", (mandate_id,))
    for d in deals:
        d["amount_text"] = money.fmt(d["asset"], d["amount"]) if d["amount"] else None
    return {"mandate_id": mandate_id, "reserved_eur_cents": sums.get("reserved", 0),
            "committed_eur_cents": sums.get("committed", 0), "released_eur_cents": sums.get("released", 0),
            "deals": deals}
