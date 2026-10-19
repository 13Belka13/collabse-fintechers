# Bank HTTP API (docs/INTERFACES.md): ids only — no addresses, no amounts to pay, no keys.
# Every Decision leaves through codes.client_view (non-public reasons never reach the client, SPEC §6.7).
# Three callers, three shared secrets (prototype auth; config.*_TOKEN):
#   agent  (x-agent-token)  -> acts for exactly one company: buyer_id comes from the token, never from the body;
#   seller (x-seller-token) -> may only post deliveries;
#   human  (x-human-token)  -> bank UI: confirmations above the threshold, VAT-risk acknowledgements.
import hmac

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel

from bank import codes, config, core, db, deals, timing

router = APIRouter(prefix="/api", tags=["agents"])


def _same(a: str | None, b: str) -> bool:
    return bool(a) and hmac.compare_digest(a.encode(), b.encode())


def agent_company(x_agent_token: str | None = Header(None)) -> str:
    for token, company_id in config.AGENT_TOKENS.items():
        if _same(x_agent_token, token):
            return company_id
    raise HTTPException(401, "agent token required")


def seller_caller(x_seller_token: str | None = Header(None)) -> None:
    if not _same(x_seller_token, config.SELLER_TOKEN):
        raise HTTPException(401, "seller token required")


def human_caller(x_human_token: str | None = Header(None)) -> None:
    if not _same(x_human_token, config.HUMAN_TOKEN):
        raise HTTPException(401, "human token required")


class DealIn(BaseModel):
    seller_id: str
    mandate_id: str
    task: dict | str
    asset: str = "EURC"
    buyer_id: str | None = None  # ignored unless it matches the token's company


class MessageIn(BaseModel):
    message: str = ""


class DeliveryIn(BaseModel):
    offer_id: str | None = None
    items: list[dict]


class HumanIn(BaseModel):
    by: str


class VatAckIn(BaseModel):
    seller_ico: str
    asset: str
    by: str


def _call(fn, *args):
    try:
        return fn(*args)
    except deals.DealError as e:
        raise HTTPException(e.status, str(e)) from e


def _own_deal(deal_id: str, company_id: str) -> dict:
    deal = deals.get(deal_id)
    if deal is None or deal["buyer_id"] != company_id:
        raise HTTPException(404, f"unknown deal {deal_id}")
    return deal


@router.get("/sellers")
def sellers(category: str | None = None, company_id: str = Depends(agent_company)):
    return [{k: s[k] for k in ("seller_id", "name", "ico", "profile_url")} for s in core.sellers(category)]


@router.get("/sellers/{seller_id}/check")
def check_seller(seller_id: str, company_id: str = Depends(agent_company)):
    seller = core.seller(seller_id)
    if seller is None:
        raise HTTPException(404, "unknown seller")
    try:
        screening = deals._module("screening")
        # not the money path: an empty cache runs the check now (parallel registries, 2 s timeout, fail-closed)
        result = screening.cached(seller["ico"]) or screening.check(seller["ico"], seller["wallet_address"],
                                                                    seller.get("website_url"))
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
def create_deal(body: DealIn, company_id: str = Depends(agent_company)):
    if body.buyer_id not in (None, company_id):
        raise HTTPException(403, "an agent can only buy for its own company")
    return {"deal_id": _call(deals.create, company_id, body.seller_id, body.mandate_id, body.task, body.asset)}


@router.post("/deals/{deal_id}/negotiate")
def negotiate(deal_id: str, body: MessageIn, company_id: str = Depends(agent_company)):
    _own_deal(deal_id, company_id)
    return _call(deals.negotiate, deal_id, body.message)


@router.post("/deals/{deal_id}/pay")
def pay(deal_id: str, company_id: str = Depends(agent_company)):
    _own_deal(deal_id, company_id)
    with timing.timed("api_pay", deal_id):
        return codes.client_view(_call(deals.pay, deal_id))


@router.get("/statement")
def statement(mandate_id: str, company_id: str = Depends(agent_company)):
    owner = db.one("SELECT company_id FROM mandates WHERE mandate_id=?", (mandate_id,))
    if owner is None or owner["company_id"] != company_id:
        raise HTTPException(404, f"unknown mandate {mandate_id}")
    return deals.statement(mandate_id)


@router.post("/deals/{deal_id}/delivery", dependencies=[Depends(seller_caller)])
def delivery(deal_id: str, body: DeliveryIn, background: BackgroundTasks):
    out = _call(deals.record_delivery, deal_id, body.model_dump())
    if not out.get("duplicate"):
        background.add_task(deals.auto_settle, deal_id)  # verifier + capture/void after the seller is answered
    return out


@router.post("/deals/{deal_id}/confirm", dependencies=[Depends(human_caller)])
def confirm(deal_id: str, body: HumanIn):
    return codes.client_view(_call(deals.confirm, deal_id, f"human:{body.by}"))


@router.post("/deals/{deal_id}/ack-vat", dependencies=[Depends(human_caller)])
def ack_vat_for_deal(deal_id: str, body: HumanIn):
    return codes.client_view(_call(deals.ack_vat, deal_id, f"human:{body.by}"))


@router.post("/mandates/{mandate_id}/ack-vat", dependencies=[Depends(human_caller)])
def ack_vat(mandate_id: str, body: VatAckIn):
    try:
        version = deals._module("mandate").acknowledge_vat(mandate_id, body.seller_ico, body.asset, f"human:{body.by}")
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {"mandate_id": mandate_id, "version": version}
