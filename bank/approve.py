# A person approves an agent's payment from a phone (demo P3). When the bank answers CONFIRMATION_REQUIRED, /story
# shows a QR code to /approve/{deal_id}?t=<token>: a card in plain words with Approve and Decline. The token is
# one-time, for one deal, lives 10 minutes, and only its SHA-256 is stored; the raw token stays in this process's
# memory so the QR on /story stays the same between refreshes. Approve calls deals.confirm(deal_id, "human:jury"),
# the same function as the confirm button on /mandate (the fallback). Each token is bound to the payment it was
# made for (deals.payment_binding: offer, amount, asset, payee — PSD2 dynamic linking): if the agent renegotiates
# while the bank waits, the old link is refused ("changed") and /story shows a new QR for the new price. Caddy lets /approve/* through without the UI
# password (deploy/Caddyfile): a valid token is the only key, and without one the page shows nothing about the deal.
import hashlib
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
           "nothing": "Nothing to approve: this payment no longer waits for a person.",
           "changed": "The payment changed since this link was made; scan the new QR code on the screen."}
_issued: dict[str, tuple[str, float]] = {}  # deal_id -> (token, expires_at): only for the QR on /story
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
            "threshold": limit("confirm_above"), "per_deal": limit("per_deal_max"), "why": _why(dec)}


def _why(dec: dict) -> str:
    """The bank's public reasons in one line ("8.00 € is above the confirmation threshold 6.00 €."); the client's
    explanation when any reason is non-public (same rule as codes.client_view)."""
    reasons = db.loads(dec["evidence_json"]) or []
    if reasons and all(r.get("public") is True for r in reasons):
        return " ".join(r["text"] for r in reasons)
    return dec["explanation_client"]


def issue(deal_id: str, base_url: str, now: float | None = None) -> tuple[str, float, str]:
    """The token for the QR on /story: the same one while it has a minute left, is unused and the payment is the one
    it was made for, otherwise a new one bound to the deal's current offer. An older token for the same payment stays
    valid until its own expiry (a phone may have just scanned it). Returns (token, expires_at, url)."""
    now = time.time() if now is None else now
    with _lock:
        held = _issued.get(deal_id)
        if not (held and held[1] - now > 60 and check(deal_id, held[0], now) is None):
            held = (secrets.token_urlsafe(16), now + TTL_S)
            with db.immediate():
                db.run("INSERT INTO approvals (token_hash, deal_id, expires_at, created_at, binding) VALUES (?,?,?,?,?)",
                       (_hash(held[0]), deal_id, held[1], now, deals.payment_binding(deals.get(deal_id))))
                journal.append("bank", "approval_link_issued", {"expires_at": held[1]}, deal_id=deal_id)
            _issued[deal_id] = held
        return held[0], held[1], f"{base_url.rstrip('/')}/approve/{deal_id}?t={held[0]}"


def check(deal_id: str, token: str | None, now: float | None = None) -> str | None:
    """None when the token is valid for this deal now, else why not: invalid (unknown, or another deal's) | used |
    expired | changed (the deal's offer, amount or payee is not the one the token was made for)."""
    row = db.one("SELECT deal_id, expires_at, used_at, binding FROM approvals WHERE token_hash=?", (_hash(token or ""),))
    if row is None or not token or row["deal_id"] != deal_id:
        return "invalid"
    if row["used_at"] is not None:
        return "used"
    if (time.time() if now is None else now) >= row["expires_at"]:
        return "expired"
    current = deals.payment_binding(deals.get(deal_id))
    if current is None or row["binding"] != current:
        return "changed"
    return None


def use(deal_id: str, token: str | None, by: str = BY, now: float | None = None) -> str | None:
    """Spend the token and every other token of this deal, in one transaction: one approval per deal, and two
    phones cannot both use one. None = spent now."""
    now = time.time() if now is None else now
    with db.immediate():
        why = check(deal_id, token, now)
        if why is None:
            db.run("UPDATE approvals SET used_at=?, used_by=? WHERE deal_id=? AND used_at IS NULL", (now, by, deal_id))
    return why


def act(deal_id: str, token: str | None, action: str, now: float | None = None) -> dict:
    """approve -> deals.confirm(deal_id, "human:jury") (the bank then pays: escrow lock, the seller delivers);
    decline -> deals.decline. Refused (DealError 403/409) without a valid token, when nothing waits, or when the
    payment changed since the token was made (confirm checks the binding again under the deal's lock)."""
    if action not in ("approve", "decline"):
        raise deals.DealError(400, "unknown action")
    why = check(deal_id, token, now) or (None if pending(deal_id) else "nothing")  # nothing about the deal leaks
    if why:
        raise deals.DealError(403 if why == "invalid" else 409, why)
    binding = db.one("SELECT binding FROM approvals WHERE token_hash=?", (_hash(token),))["binding"]
    why = use(deal_id, token, now=now)
    if why:
        raise deals.DealError(409, why)
    return deals.confirm(deal_id, BY, binding=binding) if action == "approve" else deals.decline(deal_id, BY)


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
        return _page(request, {"invalid": 403, "changed": 409}.get(why, 410), error=REASONS[why])
    expires = db.one("SELECT expires_at FROM approvals WHERE token_hash=?", (_hash(t),))["expires_at"]
    return _page(request, card=card, token=t, expires_at=expires)


@router.post("/approve/{deal_id}", response_class=HTMLResponse)
def approve_post(request: Request, deal_id: str, t: str = Form(""), action: str = Form("approve")):
    try:
        result = act(deal_id, t, action)
    except deals.DealError as e:
        return _page(request, e.status if 400 <= e.status < 600 else 409, error=REASONS.get(str(e), str(e)))
    view = codes.client_view(result) if action == "approve" else result  # the client's view, never compliance text
    text = (view.get("explanation") or "").replace(BY, "a jury member")
    return _page(request, done={"action": action, "status": view.get("status"), "text": text})
