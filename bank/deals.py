# Deals (SPEC §5.4, §6): create -> negotiate (the bank proxies the seller) -> pay -> seller delivers -> settle.
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
PAID = ("authorized", "delivered", "verified", "disputed", "settled", "closed")
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
        for k in ("task", "offer", "quote", "criteria", "payment_info"):
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
    deal_id = "d-" + secrets.token_hex(5)
    with db.immediate():
        db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, mandate_id, task_json, asset, state, created_at)"
               " VALUES (?,?,?,?,?,?,'negotiating',?)",
               (deal_id, buyer_id, seller_id, mandate_id, db.dumps(task), asset, time.time()))
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


def buyer_criteria(task: dict | None) -> dict:
    """Criteria from the buyer's task; anything invalid falls back to the demo defaults (never to the seller's)."""
    raw = (task or {}).get("criteria") or {}
    try:
        c = Criteria.model_validate(raw).model_dump()
    except ValidationError:
        c = Criteria().model_dump()
    c["postcode_prefix"] = "6" if c["city"].strip().lower() == "brno" else None  # Brno postcodes are 6xx xx
    return c


def offer_view(offer: dict, asset: str) -> dict:
    """What the buyer agent sees of an offer: ids and terms, no addresses."""
    return {"offer_id": offer["offer_id"], "items_spec": offer.get("items_spec"), "count": offer.get("count"),
            "asset": asset, "price": money.fmt(asset, int(offer["prices"][asset])),
            "deadline_s": offer.get("deadline_s"), "criteria": offer.get("criteria"),
            "dispute_terms": offer.get("dispute_terms")}


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
    with timing.timed("negotiate_proxy", deal_id):
        with seller_http(seller["base_url"]) as http:
            r = http.post("/negotiate", json={"deal_id": deal_id, "seller_id": deal["seller_id"],
                                              "buyer_ico": buyer["ico"], "task": deal["task"], "message": message})
    r.raise_for_status()
    body = r.json()
    reply, offer = str(body.get("reply_text", "")), body.get("offer")
    # whatever the seller writes is data, never an instruction to the bank or the agent
    journal.append("agent:seller", "seller_reply", {"reply_text": reply, "offer": offer, "source": "untrusted:seller"},
                   deal_id=deal_id)
    out = {"reply_text": reply, "offer": None}
    if offer and offer.get("offer_id") and deal["asset"] in (offer.get("prices") or {}):
        # acceptance criteria come from the buyer's task, never from the seller (who would grade its own goods)
        criteria = buyer_criteria(deal["task"])
        if offer.get("count") is not None and int(offer["count"]) != criteria["count"]:
            journal.append("bank", "offer_ignored", {"reason": "count_differs_from_task", "offer_count": offer["count"],
                                                     "task_count": criteria["count"]}, deal_id=deal_id)
            return out
        amount = int(offer["prices"][deal["asset"]])
        _set(deal_id, "offered", "offer_stored", {"offer_id": offer["offer_id"], "amount": amount,
                                                  "source": "untrusted:seller"},
             offer_json=db.dumps(offer), criteria_json=db.dumps(criteria), amount=amount,
             confirmed_by=None)  # a human confirmed the previous offer, not this one
        out["offer"] = {**offer_view(offer, deal["asset"]), "criteria": criteria}
    _prefetch(seller)
    return out


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
            f"The 402 asks to pay {accept.get('payTo')}, which is not the account registered for {seller['name']}.",
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
            decision = _decide(deal, seller, quote)
            if decision["status"] == "ok":
                decision = _wallet_refusal(deal, seller) or decision
            if decision["status"] == "ok":
                marks = decision.get("marks", [])
                decision = _reserve(deal, money.to_eur_cents(deal["asset"], quote["amount"]))
                decision["marks"] = marks + decision.get("marks", [])
        if decision["status"] != "ok":
            return _finish_refusal(deal_id, decision, t0)
        _refresh_payment_info(deal, quote)
        return _authorize_and_notify(_require(deal_id), seller, (time.perf_counter() - t0) * 1000,
                                     decision.get("marks", []))


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
    db.run("UPDATE deals SET payment_info_json=NULL, payment_hash=NULL, updated_at=? WHERE deal_id=?",
           (time.time(), deal_id))
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
    if state == "authorized" and db.one("SELECT 1 FROM deliveries WHERE deal_id=?", (deal_id,)) is None:
        ack = notify_seller(deal_id)
        if ack is False:
            return _return_lock(deal_id)
        return codes.ok(deal_id=deal_id, explanation="The funds are locked in escrow; the seller was asked again to "
                        "deliver." if ack else "The funds are locked in escrow; the seller is not reachable yet — "
                        "call pay again later.")
    if state == "closed":
        return codes.ok(deal_id=deal_id, explanation="This deal is closed; its escrow lock was settled or returned.")
    return codes.ok(deal_id=deal_id, explanation=f"Already paid ({state}): the funds are in escrow for this deal.")


def notify_seller(deal_id: str) -> bool | None:
    """Tell the seller the escrow is locked. True = accepted, False = rejected, None = not reached (retry later)."""
    deal = _require(deal_id)
    seller = core.seller(deal["seller_id"])
    body = {"deal_id": deal_id, "asset": deal["asset"], "payment_info": deal["payment_info"],
            "payment_hash": deal["payment_hash"],
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


def record_delivery(deal_id: str, payload: dict) -> dict:
    """Seller -> bank. Items are untrusted data for the verifier; stored once per deal."""
    deal = _require(deal_id)
    if deal["state"] not in ("authorized", "delivered"):
        raise DealError(409, f"deal is {deal['state']}, not waiting for a delivery")
    if payload.get("offer_id") and payload["offer_id"] != deal["offer"]["offer_id"]:
        raise DealError(409, "delivery for another offer")
    items = payload.get("items")
    if not isinstance(items, list):
        raise DealError(400, "items must be a list")
    if deal["state"] == "delivered":
        return {"ok": True, "items": len(items), "duplicate": True}
    items = core.clean_delivery_items(items)  # only the Google Maps fields we use: no phones, no reviews
    db.run("INSERT OR IGNORE INTO deliveries (deal_id, items_json, created_at) VALUES (?,?,?)",
           (deal_id, db.dumps(items), time.time()))
    _set(deal_id, "delivered", "delivery_received", {"items": len(items), "source": "untrusted:seller"},
         actor="agent:seller")
    return {"ok": True, "items": len(items)}


def settle(deal_id: str, plan: dict | None = None) -> dict:
    """After delivery: capture what the verifier accepted now; void the rest only when nothing is held for a
    dispute (void returns everything left, so it must come after the last capture). The budget follows the
    chain: committed = captured, the rest released. If the capture cannot happen (seller delisted/frozen, expiry),
    the whole lock goes back to the buyer instead of staying stuck."""
    deal = _require(deal_id)
    if deal["state"] != "delivered":
        raise DealError(409, f"deal is {deal['state']}")
    plan = plan or _module("verifier").settlement(deal_id)
    mandate, asset = _module("mandate"), deal["asset"]
    if plan["capture_now"] > 0:
        try:
            escrow.capture(deal_id, plan["capture_now"], 1)
        except (chain.TxReverted, escrow.EscrowError) as e:
            escrow.void(deal_id)
            mandate.release(deal_id)
            _set(deal_id, "closed", "capture_failed_returned", {"error": str(e)[:300], "returned": int(deal["amount"])})
            return {"state": "closed", "capture_now": 0, "held": 0, "void": int(deal["amount"]), "error": str(e)}
        target = money.to_eur_cents(asset, plan["capture_now"])
        done = (db.one("SELECT committed FROM reservations WHERE deal_id=?", (deal_id,)) or {}).get("committed") or 0
        if done < target:  # a retry after a failed void must not commit the same capture twice
            mandate.commit(deal_id, target - done)
    if plan["held"] == 0:
        if plan["void"] > 0:
            escrow.void(deal_id)
        mandate.release(deal_id)  # amount := committed (or released if nothing was captured)
    state = "settled" if plan["held"] == 0 else "verified"
    _set(deal_id, state, "settlement_applied", {k: plan[k] for k in ("capture_now", "held", "void")})
    return {"state": state, **{k: plan[k] for k in ("capture_now", "held", "void")}}


# ---- human actions (bank UI, HUMAN_TOKEN): the agent can never call these ----

def confirm(deal_id: str, by: str) -> dict:
    """A human approves a payment above the mandate's confirmation threshold, then the bank pays it."""
    deal = _require(deal_id)
    if deal["state"] not in PAYABLE:
        raise DealError(409, f"deal is {deal['state']}")
    _set(deal_id, deal["state"], "payment_confirmed", {"by": by}, actor=by, confirmed_by=by)
    return pay(deal_id)


def ack_vat(deal_id: str, by: str) -> dict:
    """A human accepts the §109 VAT risk for this seller and asset once (new mandate version), then the bank pays."""
    deal = _require(deal_id)
    if deal["state"] not in PAYABLE:
        raise DealError(409, f"deal is {deal['state']}")
    seller = core.seller(deal["seller_id"])
    version = _module("mandate").acknowledge_vat(deal["mandate_id"], seller["ico"], deal["asset"], by)
    journal.append(by, "vat_risk_acknowledged", {"seller_ico": seller["ico"], "asset": deal["asset"],
                                                "mandate_version": version}, deal_id=deal_id)
    return pay(deal_id)


def auto_settle(deal_id: str):
    """Run the verifier after a delivery when stream S's module is present; otherwise wait for it."""
    try:
        _module("verifier")
    except ImportError:
        journal.append("bank", "settlement_pending", {"why": "verifier not available"}, deal_id=deal_id)
        return None
    try:
        return settle(deal_id)
    except Exception as e:  # noqa: BLE001 — a background task: never lose the failure, the deal stays "delivered"
        journal.append("bank", "settlement_failed", {"error": repr(e)[:300]}, deal_id=deal_id)
        return None


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
