# A person approves an agent's payment from a phone (demo P3). When the bank answers CONFIRMATION_REQUIRED, /story
# shows a QR code to /approve/{deal_id}?t=<token>: a card in plain words with Approve and Decline. The token is
# one-time, for one deal, lives 10 minutes, and only its SHA-256 is stored; the raw token stays in this process's
# memory so the QR on /story stays the same between refreshes. Approve calls deals.confirm(deal_id, "human:jury"),
# the same function as the confirm button on /mandate (the fallback). Caddy lets /approve/* through without the UI
# password (deploy/Caddyfile): a valid token is the only key, and without one the page shows nothing about the deal.
import hashlib
import hmac
import secrets
import threading
import time
from html import escape
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from bank import codes, config, core, db, deals, journal, money

TTL_S = 600
BY = "human:jury"
REASONS = {"invalid": "This approval link is not valid.",
           "used": "This approval link was already used.",
           "expired": "This approval link has expired. Scan the new QR code on the screen.",
           "nothing": "Nothing to approve: this payment no longer waits for a person."}
_issued: dict[str, tuple[str, float, str]] = {}  # deal_id -> (token, expires_at, url): for the QR on /story only
_lock = threading.Lock()
router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pending(deal_id: str) -> dict | None:
    """The card for a deal that waits for a person's confirmation (the bank's last answer CONFIRMATION_REQUIRED),
    or None. Plain words: who pays whom, how much, for what, and the mandate's threshold."""
    deal = deals.get(deal_id)
    if deal is None or deal["state"] != "awaiting_human":
        return None
    dec = db.one("SELECT * FROM decisions WHERE deal_id=? AND kind='pay' ORDER BY id DESC LIMIT 1", (deal_id,))
    if dec is None or "CONFIRMATION_REQUIRED" not in (db.loads(dec["codes_json"]) or []):
        return None
    from bank import mandate
    m = mandate.current(deal["mandate_id"]) if deal.get("mandate_id") else None
    seller, buyer = core.seller(deal["seller_id"]), core.company(deal["buyer_id"])
    asset, amount = deal["asset"] or "EURC", int(deal["amount"] or 0)
    eur = lambda cents: f"{cents / 100:.2f} €".replace(".", ",")  # noqa: E731
    limit = lambda key: eur(mandate.cents(m["policy"][key])) if m else None  # noqa: E731
    return {"deal_id": deal_id, "buyer": buyer["name"] if buyer else deal["buyer_id"],
            "seller": seller["name"] if seller else deal["seller_id"], "price": money.fmt(asset, amount),
            "eur": eur(money.to_eur_cents(asset, amount)), "asset": asset, "count": (deal["offer"] or {}).get("count"),
            "threshold": limit("confirm_above"), "per_deal": limit("per_deal_max"), "why": dec["explanation_client"]}


def issue(deal_id: str, base_url: str, now: float | None = None) -> tuple[str, float, str]:
    """The token for the QR on /story: the same one while it has a minute left and is unused, otherwise a new one
    (its hash replaces the old one, so an older QR stops working). Returns (token, expires_at, url)."""
    now = time.time() if now is None else now
    with _lock:
        held = _issued.get(deal_id)
        if held and held[1] - now > 60:
            row = db.one("SELECT token_hash, used_at FROM approvals WHERE deal_id=?", (deal_id,))
            if row and row["used_at"] is None and hmac.compare_digest(row["token_hash"], _hash(held[0])):
                return held
        token, expires = secrets.token_urlsafe(16), now + TTL_S
        with db.immediate():
            db.run("INSERT INTO approvals (deal_id, token_hash, expires_at, created_at) VALUES (?,?,?,?)"
                   " ON CONFLICT(deal_id) DO UPDATE SET token_hash=excluded.token_hash, expires_at=excluded.expires_at,"
                   " used_at=NULL, used_by=NULL, created_at=excluded.created_at", (deal_id, _hash(token), expires, now))
            journal.append("bank", "approval_link_issued", {"expires_at": expires}, deal_id=deal_id)
        url = f"{base_url.rstrip('/')}/approve/{deal_id}?t={token}"
        _issued[deal_id] = (token, expires, url)
        return _issued[deal_id]


def check(deal_id: str, token: str | None, now: float | None = None) -> str | None:
    """None when the token is valid for this deal now, else why not: invalid (unknown, or another deal's) | used |
    expired."""
    row = db.one("SELECT token_hash, expires_at, used_at FROM approvals WHERE deal_id=?", (deal_id,))
    if row is None or not token or not hmac.compare_digest(row["token_hash"], _hash(token)):
        return "invalid"
    if row["used_at"] is not None:
        return "used"
    if (time.time() if now is None else now) >= row["expires_at"]:
        return "expired"
    return None


def use(deal_id: str, token: str | None, by: str = BY, now: float | None = None) -> str | None:
    """Spend the token: check and mark used in one transaction, so two phones cannot both use it. None = spent now."""
    now = time.time() if now is None else now
    with db.immediate():
        why = check(deal_id, token, now)
        if why is None:
            db.run("UPDATE approvals SET used_at=?, used_by=? WHERE deal_id=? AND used_at IS NULL", (now, by, deal_id))
    return why


def act(deal_id: str, token: str | None, action: str, now: float | None = None) -> dict:
    """approve -> deals.confirm(deal_id, "human:jury") (the bank then pays: escrow lock, the seller delivers);
    decline -> deals.decline. Refused (DealError 403/409) without a valid token or when nothing waits."""
    if action not in ("approve", "decline"):
        raise deals.DealError(400, "unknown action")
    why = check(deal_id, token, now) or (None if pending(deal_id) else "nothing")  # nothing about the deal leaks
    if why:
        raise deals.DealError(403 if why == "invalid" else 409, why)
    why = use(deal_id, token, now=now)
    if why:
        raise deals.DealError(409, why)
    return deals.confirm(deal_id, BY) if action == "approve" else deals.decline(deal_id, BY)


def qr_svg(url: str) -> str:
    """Server-side SVG QR code (qrcode's SVG path factory, no PIL); the URL is in its aria-label."""
    svg = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2).to_string(
        encoding="unicode")
    return svg.replace("<svg ", f'<svg role="img" aria-label="QR code: {escape(url)}" ', 1)


def story_qr(deal_id: str, request: Request | None = None) -> dict | None:
    """For /story: the QR to approve this deal from a phone, or None when it does not wait for a confirmation.
    The link's host is PUBLIC_BASE_URL, else the host the presenter's browser used (Caddy forwards it)."""
    if pending(deal_id) is None:
        return None
    base = config.PUBLIC_BASE_URL or (str(request.base_url) if request is not None else config.BANK_URL)
    token, expires, url = issue(deal_id, base)
    return {"url": url, "path": f"/approve/{deal_id}", "svg": qr_svg(url), "expires_at": expires}


# ---- the phone page: outside the UI password, so it renders nothing about a deal without a valid token ----

def _page(request: Request, status: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, "approve.html", {"card": None, "error": None, "done": None, **ctx},
                                      status_code=status)


@router.get("/approve/{deal_id}", response_class=HTMLResponse)
def approve_page(request: Request, deal_id: str, t: str = ""):
    why = check(deal_id, t)
    card = pending(deal_id) if why is None else None
    if why is None and card is None:
        why = "nothing"
    if why:
        return _page(request, 403 if why == "invalid" else 410, error=REASONS[why])
    expires = db.one("SELECT expires_at FROM approvals WHERE deal_id=?", (deal_id,))["expires_at"]
    return _page(request, card=card, token=t, expires_at=expires)


@router.post("/approve/{deal_id}", response_class=HTMLResponse)
def approve_post(request: Request, deal_id: str, t: str = Form(""), action: str = Form("approve")):
    try:
        result = act(deal_id, t, action)
    except deals.DealError as e:
        return _page(request, e.status if 400 <= e.status < 600 else 409, error=REASONS.get(str(e), str(e)))
    view = codes.client_view(result) if action == "approve" else result  # the client's view, never compliance text
    return _page(request, done={"action": action, "status": view.get("status"), "text": view.get("explanation") or ""})
