# Bank service (:8000): one process for core, mandate, screening, x402 facilitator, escrow
# operator, verifier, disputes, journal and UI (SPEC §1). Streams add their routers below.
# Run: uvicorn bank.app:app --port 8000   (start before the seller: its x402 middleware calls /x402/supported)
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bank import config, db


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
app.include_router(__import__("bank.api", fromlist=["router"]).router)


@app.get("/health")
def health():
    return {"ok": True, "service": "bank", "mock": config.MOCK, "network": config.NETWORK,
            "tczk": bool(config.TCZK), "llm": "mock" if config.MOCK else config.MODEL_MAIN}


from bank import facilitator  # noqa: E402  (stream T: x402 facilitator for EURC and tCZK)
app.include_router(facilitator.router)
