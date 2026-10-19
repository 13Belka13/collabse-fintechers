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
    yield


app = FastAPI(title="Collabse bank", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True, "service": "bank", "mock": config.MOCK, "network": config.NETWORK,
            "tczk": bool(config.TCZK), "llm": "mock" if config.MOCK else config.MODEL_MAIN}
