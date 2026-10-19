# Bank HTTP API for agents (docs/INTERFACES.md): ids only — no addresses, no amounts to pay, no keys.
# Every Decision leaves through codes.client_view (non-public reasons never reach the client, SPEC §6.7).
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from bank import codes, core, deals, timing

router = APIRouter(prefix="/api", tags=["agents"])


class DealIn(BaseModel):
    buyer_id: str
    seller_id: str
    mandate_id: str
    task: dict | str
    asset: str = "EURC"


class MessageIn(BaseModel):
    message: str = ""


class DeliveryIn(BaseModel):
    offer_id: str | None = None
    items: list[dict]


def _call(fn, *args):
    try:
        return fn(*args)
    except deals.DealError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("/sellers")
def sellers(category: str | None = None):
    return [{k: s[k] for k in ("seller_id", "name", "ico", "profile_url")} for s in core.sellers(category)]


@router.get("/sellers/{seller_id}/check")
def check_seller(seller_id: str):
    seller = core.seller(seller_id)
    if seller is None:
        raise HTTPException(404, "unknown seller")
    try:
        result = deals._module("screening").cached(seller["ico"])
    except ImportError:
        result = None
    if result is None:
        return {"seller_id": seller_id, "level": "review", "public_reasons": [], "explanation": codes.NEUTRAL_CLIENT_TEXT}
    reasons = result.get("reasons", [])
    if any(r.get("public") is not True for r in reasons):  # same rule as client_view: no hint of hidden reasons
        return {"seller_id": seller_id, "level": "review", "public_reasons": [], "explanation": codes.NEUTRAL_CLIENT_TEXT}
    return {"seller_id": seller_id, "level": result.get("level"),
            "public_reasons": [{k: r.get(k) for k in codes.CLIENT_REASON_FIELDS} for r in reasons],
            "sources": result.get("sources", [])}


@router.post("/deals")
def create_deal(body: DealIn):
    return {"deal_id": _call(deals.create, body.buyer_id, body.seller_id, body.mandate_id, body.task, body.asset)}


@router.post("/deals/{deal_id}/negotiate")
def negotiate(deal_id: str, body: MessageIn):
    return _call(deals.negotiate, deal_id, body.message)


@router.post("/deals/{deal_id}/pay")
def pay(deal_id: str):
    with timing.timed("api_pay", deal_id):
        return codes.client_view(_call(deals.pay, deal_id))


@router.get("/statement")
def statement(mandate_id: str):
    return deals.statement(mandate_id)


@router.post("/deals/{deal_id}/delivery")
def delivery(deal_id: str, body: DeliveryIn, background: BackgroundTasks):
    out = _call(deals.record_delivery, deal_id, body.model_dump())
    if not out.get("duplicate"):
        background.add_task(deals.auto_settle, deal_id)  # verifier + capture/void after the seller is answered
    return out
