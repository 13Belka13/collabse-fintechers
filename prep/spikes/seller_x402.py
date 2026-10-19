# Spike: x402 seller charging 0.01 EURC (or USDC with TOKEN=USDC) on Base Sepolia via the public facilitator.
import os
from dotenv import load_dotenv
from fastapi import FastAPI
from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact import ExactEvmServerScheme

load_dotenv()
TOKENS = {
    "EURC": {"asset": "0x808456652fdb597867f38412077A9182bf77359F", "extra": {"name": "EURC", "version": "2"}},
    "USDC": {"asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "extra": {"name": "USDC", "version": "2"}},
}
token = TOKENS[os.getenv("TOKEN", "EURC")]

server = x402ResourceServer(HTTPFacilitatorClient({"url": "https://x402.org/facilitator"}))
server.register("eip155:84532", ExactEvmServerScheme())

routes = {
    "GET /data": {
        "accepts": {
            "scheme": "exact",
            "payTo": os.environ["SELLER_ADDR"],
            "price": {"amount": "10000", **token},  # 0.01 with 6 decimals
            "network": "eip155:84532",
        }
    }
}

app = FastAPI()
mw = payment_middleware(routes, server)


@app.middleware("http")
async def x402_mw(request, call_next):
    return await mw(request, call_next)


@app.get("/data")
async def data():
    return {"warehouses": ["demo"]}
