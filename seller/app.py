# Seller service (:8001): x402-priced order route, /negotiate, /order/{offer_id}/escrow (SPEC §4.1, §5.4).
# Separate company from the bank: it keeps its own state and only talks to the bank over HTTP.
# Run: uvicorn seller.app:app --port 8001   (after the bank)
from fastapi import FastAPI

app = FastAPI(title="Collabse seller")


@app.get("/health")
def health():
    return {"ok": True, "service": "seller"}
