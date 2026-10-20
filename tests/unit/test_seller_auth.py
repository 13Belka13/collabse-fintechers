# Seller credentials: one token per seller (HMAC of SELLER_TOKEN and the seller id, or SELLER_TOKENS), so a seller can
# register keys, deliver and act in a C claim only for itself. The shared SELLER_TOKEN works only when allowed.
import hashlib
import hmac

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import api, config, receipts
from seller import app as seller_mod
from tests.unit import dealkit as k

KEY = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"


@pytest.fixture
def bank(seeded, monkeypatch):
    w = k.world(monkeypatch)
    k.make_deal(w, deal_id="d-beta")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def tok(seller_id):
    return {"x-seller-token": config.seller_token(seller_id)}


def test_seller_token_is_hmac_of_the_shared_secret_unless_overridden(monkeypatch):
    assert config.seller_token("s-beta") == hmac.new(b"dev-seller", b"s-beta", hashlib.sha256).hexdigest()
    assert config.seller_token("s-beta") != config.seller_token("s-gama") != config.SELLER_TOKEN
    monkeypatch.setenv("SELLER_TOKENS", "s-beta:beta-secret, s-gama:gama-secret")
    config.reload()
    assert config.seller_token("s-beta") == "beta-secret" and config.seller_token("s-gama") == "gama-secret"
    assert config.seller_token("s-delta") == hmac.new(b"dev-seller", b"s-delta", hashlib.sha256).hexdigest()


def test_register_key_only_for_itself(bank):
    path = "/api/sellers/s-beta/keys"
    assert bank.post(path, json={"address": KEY}, headers=tok("s-gama")).status_code == 403
    assert bank.post(path, json={"address": KEY}).status_code == 401
    assert receipts.key_valid("s-beta", KEY, 1e12) is False
    r = bank.post(path, json={"address": KEY}, headers=tok("s-beta"))
    assert r.status_code == 200 and r.json()["seller_id"] == "s-beta"
    assert bank.post(path, json={"address": KEY}, headers=tok("s-beta")).status_code == 200  # idempotent re-register


@pytest.mark.parametrize("method, path", [
    ("post", "/delivery"), ("get", "/claims/c/fee-terms"), ("post", "/claims/c/seller-fee"), ("post", "/claims/c/agree"),
    ("post", "/claims/c/seller-challenge"), ("post", "/claims/c/seller-reveal"), ("get", "/claims/c/rereview-terms"),
    ("post", "/claims/c/seller-rereview")])
def test_another_sellers_deal_is_403(bank, method, path):
    body = {"manifest": {}, "signature": "0x", "items": [], "hash": "0x", "deciders": [], "nonce": "0x"}
    kw = {"json": body} if method == "post" else {}
    assert getattr(bank, method)(f"/api/deals/d-beta{path}", headers=tok("s-gama"), **kw).status_code == 403
    assert getattr(bank, method)(f"/api/deals/d-beta{path}", **kw).status_code == 401
    assert getattr(bank, method)(f"/api/deals/d-none{path}", headers=tok("s-beta"), **kw).status_code == 404
    assert getattr(bank, method)(f"/api/deals/d-beta{path}", headers=tok("s-beta"), **kw).status_code not in (401, 403)


def test_shared_token_only_when_allowed(bank, monkeypatch):
    shared = {"x-seller-token": config.SELLER_TOKEN}
    assert bank.post("/api/sellers/s-beta/keys", json={"address": KEY}, headers=shared).status_code == 401
    monkeypatch.setenv("ALLOW_SHARED_SELLER_TOKEN", "1")
    config.reload()
    assert bank.post("/api/sellers/s-beta/keys", json={"address": KEY}, headers=shared).status_code == 200


def test_seller_service_sends_each_sellers_own_token(monkeypatch):
    sent = []

    class Resp:
        def raise_for_status(self):
            return self

    monkeypatch.setattr(httpx, "post", lambda url, json, timeout, headers: sent.append(headers) or Resp())
    seller_mod.bank_post("/api/deals/d-1/delivery", {}, "s-gama")
    assert sent == [{"x-seller-token": config.seller_token("s-gama")}]
