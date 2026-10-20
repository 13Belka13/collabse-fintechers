# Bank service (:8000): one process for core, mandate, screening, x402 facilitator, escrow
# operator, verifier, disputes, journal and UI (SPEC §1). Streams add their routers below.
# Run: uvicorn bank.app:app --port 8000   (start before the seller: its x402 middleware calls /x402/supported)
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bank import config, db

log = logging.getLogger("bank")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    if config.MOCK:
        print("\n*** MOCK=1: scripted LLM replies, $0 model cost. Set MOCK=0 for live Bedrock. ***\n", flush=True)
    dev = [n for n, v in (("AGENT_TOKEN_ALFA", next(iter(config.AGENT_TOKENS))), ("SELLER_TOKEN", config.SELLER_TOKEN),
                          ("HUMAN_TOKEN", config.HUMAN_TOKEN), ("BANK_TO_SELLER_TOKEN", config.BANK_TO_SELLER_TOKEN))
           if v.startswith("dev-")]
    if dev:
        print(f"*** dev default tokens in use: {', '.join(dev)} — set them in .env before a live demo ***", flush=True)
    yield


app = FastAPI(title="Collabse bank", lifespan=lifespan)


@app.exception_handler(Exception)
async def internal_error(request: Request, exc: Exception):
    """Last net for anything a route did not turn into a refusal or an HTTP error: the traceback goes to the log
    with a reference, the client gets a short JSON (API) or HTML (pages) answer without internals; the bank runs on."""
    ref = secrets.token_hex(4)
    log.error("unhandled error on %s %s [ref %s]", request.method, request.url.path, ref, exc_info=exc)
    text = f"The bank could not complete this request (ref {ref}). Try again; the details are in the bank's log."
    if request.url.path.startswith(("/api", "/x402", "/webhooks")) or "json" in request.headers.get("accept", ""):
        return JSONResponse({"error": "internal_error", "detail": text, "ref": ref}, status_code=500)
    return HTMLResponse(f'<!doctype html><meta charset="utf-8"><title>Error</title><p>{text}</p>'
                        '<p><a href="/story">Back to the demo</a></p>', status_code=500)
app.include_router(__import__("bank.api", fromlist=["router"]).router)


@app.get("/health")
def health():
    return {"ok": True, "service": "bank", "mock": config.MOCK, "network": config.NETWORK,
            "tczk": bool(config.TCZK), "llm": "mock" if config.MOCK else config.MODEL_MAIN}


from bank import facilitator  # noqa: E402  (stream T: x402 facilitator for EURC and tCZK)
app.include_router(facilitator.router)
app.include_router(__import__("bank.ui", fromlist=["router"]).router)  # stream U: pages + dispute timer
app.include_router(__import__("bank.webhooks", fromlist=["router"]).router)  # L7: Tatum webhook + RPC polling fallback
