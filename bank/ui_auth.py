# Human actions in the bank UI are same-origin only (CSRF). The demo password (Caddy basic auth) is sent by the
# browser with any request, so a form on another site could otherwise make the logged-in person confirm a payment.
# A state-changing request passes when the browser says it comes from a page of this bank: Sec-Fetch-Site, when
# present, is same-origin (or none) and the Origin, else the Referer, names this Host (Caddy keeps the Host
# header). A request with neither header (curl, a test client) must carry the human token (x-human-token).
# Use: APIRouter(dependencies=[Depends(same_origin)]) or @router.post(..., dependencies=[Depends(same_origin)]).
import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from bank import config

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def same_origin(request: Request) -> None:
    """FastAPI dependency: 403 for a cross-site or anonymous state-changing request; GET/HEAD/OPTIONS pass."""
    if request.method in SAFE_METHODS:
        return
    h = request.headers
    site = h.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, "Refused: this action can only be sent from the bank's own pages.")
    source = h.get("origin") if h.get("origin") not in (None, "", "null") else h.get("referer")
    if source:
        if urlsplit(source).netloc.lower() != h.get("host", "").lower():
            raise HTTPException(403, "Refused: this action can only be sent from the bank's own pages.")
        return
    if site == "same-origin":  # a browser that sends neither Origin nor Referer (no-referrer policy) still says this
        return
    token = h.get("x-human-token") or ""
    if not (token and hmac.compare_digest(token.encode(), config.HUMAN_TOKEN.encode())):
        raise HTTPException(403, "Refused: no Origin header and no human token.")
