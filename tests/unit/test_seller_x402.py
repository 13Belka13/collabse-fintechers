# Seller x402 route (SPEC §4.1, scenario N12): unpaid -> 402 with EURC and tCZK accepts priced from the offer,
# swap modes change the 402, a paid request settles through the facilitator, the seller checks escrow on chain.
import asyncio
import base64
import json
import time

import httpx
import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from x402.http.clients.httpx import x402AsyncTransport
from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse

from bank import config, x402_buyer, receipts
from seller import app as seller_mod

TCZK = "0x6a99C9e95DD220172d07D5f730f2e82B466D4B67"
BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"
FAKE_TX = "0x" + "ab" * 32


class FakeFacilitator:
    """Stands in for the bank facilitator (stream T): /supported, verify, settle."""

    def __init__(self):
        self.verified, self.settled = [], []

    def get_supported(self):
        return SupportedResponse(kinds=[SupportedKind(x402_version=2, scheme="exact", network=config.NETWORK)])

    async def verify(self, payload, requirements):
        self.verified.append((payload, requirements))
        return VerifyResponse(is_valid=True, payer=payload.payload["authorization"]["from"])

    async def settle(self, payload, requirements):
        self.settled.append((payload, requirements))
        return SettleResponse(success=True, transaction=FAKE_TX, network=config.NETWORK,
                              payer=payload.payload["authorization"]["from"])


@pytest.fixture
def seller(monkeypatch):
    monkeypatch.setenv("TCZK", TCZK)
    monkeypatch.setenv("TCZK_NAME", "Demo Bank tCZK")
    config.reload()
    fac = FakeFacilitator()
    app = seller_mod.create_app(fac)
    return app, fac, TestClient(app)


def offer(client, seller_id="s-beta", deal_id="d-1"):
    return client.post("/negotiate", json={"deal_id": deal_id, "seller_id": seller_id}).json()["offer"]


def quote(response) -> dict:
    return json.loads(base64.b64decode(response.headers["payment-required"]))


def test_route_keys_never_use_braces():
    assert all("{" not in k and ":offer_id" in k for k in seller_mod.PAID_ROUTES)


def test_unpaid_request_gets_402_with_both_assets(seller):
    _, _, client = seller
    o = offer(client)
    for path in (f"/order/{o['offer_id']}", f"/data/{o['offer_id']}"):
        r = client.get(path)
        assert r.status_code == 402
        accepts = {a["asset"]: a for a in quote(r)["accepts"]}
        eurc, tczk = accepts[config.EURC], accepts[TCZK]
        assert (eurc["amount"], eurc["extra"], eurc["payTo"]) == ("5000000", {"name": "EURC", "version": "2"}, BETA)
        assert (tczk["amount"], tczk["extra"], tczk["payTo"]) == ("12500", {"name": "Demo Bank tCZK", "version": "1"}, BETA)
        assert {a["network"] for a in accepts.values()} == {"eip155:84532"}


def test_unknown_offer_is_404_not_free(seller):
    _, _, client = seller
    assert client.get("/data/o-missing").status_code == 404


@pytest.mark.parametrize("mode,field,expected", [("swap_payto", "payTo", seller_mod.SWAP_PAYTO),
                                                 ("swap", "payTo", seller_mod.SWAP_PAYTO),
                                                 ("swap_price", "amount", "10000000")])
def test_swap_modes_change_the_402(seller, mode, field, expected):
    _, _, client = seller
    client.post("/mode", json={"seller_id": "s-beta", "mode": mode})  # the behaviour is fixed when the offer is made
    o = offer(client)
    eurc = next(a for a in quote(client.get(f"/order/{o['offer_id']}"))["accepts"] if a["asset"] == config.EURC)
    assert eurc[field] == expected


def _pay(app, url, key, asset=None, cap=x402_buyer.PER_PAYMENT_CAP_EUR_CENTS):
    async def go():
        transport = x402AsyncTransport(x402_buyer.client(key, asset, cap), transport=httpx.ASGITransport(app))
        async with httpx.AsyncClient(transport=transport, base_url="http://seller") as http:
            return await http.get(url)
    return asyncio.run(go())


@pytest.mark.parametrize("asset,address,amount", [("EURC", None, "5000000"), ("tCZK", TCZK, "12500")])
def test_paid_request_settles_and_returns_tx_hash(seller, asset, address, amount):
    app, fac, client = seller
    o = offer(client)
    buyer = Account.create()
    r = _pay(app, f"/data/{o['offer_id']}", buyer.key.hex(), asset)
    assert r.status_code == 200 and len(r.json()["items"]) == 20
    settle = json.loads(base64.b64decode(r.headers["payment-response"]))
    assert settle["transaction"] == FAKE_TX and settle["success"] is True
    payload, req = fac.settled[0]
    assert req.asset == (address or config.EURC) and req.amount == amount and req.pay_to == BETA
    assert payload.payload["authorization"]["from"] == buyer.address


def test_spend_cap_blocks_overpriced_payment(seller):
    app, fac, client = seller
    o = offer(client)
    with pytest.raises(Exception):  # 5 € quote, 1 € cap: the client refuses to sign
        _pay(app, f"/data/{o['offer_id']}", Account.create().key.hex(), "EURC", cap=100)
    assert fac.settled == []


def test_spend_controls_are_atomic_caps(monkeypatch):
    monkeypatch.setenv("TCZK", TCZK)
    config.reload()
    caps = {a["asset"]: a["max_amount_per_payment"] for a in x402_buyer.spend_controls()["allowed_assets"]}
    assert caps == {config.EURC: "10000000", TCZK: "25000"}


def test_fake_data_breaks_three_items():
    items = seller_mod.make_items(20, "fake_data")
    assert sum(1 for i in items if not i["website"] or i["city"] != "Brno") == 2  # + one company closed by ARES
    assert all(i["ico"] and i["name"] for i in items)
    assert not any("phone" in k for i in items for k in i)


def _pi(o, **over):
    from bank import escrow
    now = int(time.time())
    auth = now + 300 + int(o["window_s"]) + 30 * 86400
    pi = {"operator": "0x0469ceC178789072bc61435835D0BB38214D4e53", "payer": "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140",
          "receiver": BETA, "token": config.EURC, "maxAmount": 5_000_000, "preApprovalExpiry": now + 600,
          "authorizationExpiry": auth, "refundExpiry": auth + 30 * 86400, "minFeeBps": 120, "maxFeeBps": 120,
          "feeReceiver": config.BANK_CLEARING_ADDR, "salt": str(escrow.salt("d-1", 0))}
    return {**pi, **over}


@pytest.mark.parametrize("over,capturable,reason", [
    ({}, 5_000_000, None),
    ({"receiver": seller_mod.SWAP_PAYTO}, 5_000_000, "receiver_not_our_pay_to"),
    ({"token": TCZK}, 5_000_000, "token_not_quoted_asset"),
    ({"maxAmount": 4_000_000}, 4_000_000, "amount_not_offer_price"),
    ({"authorizationExpiry": int(time.time()) + 86400}, 5_000_000, "authorization_expires_too_early"),
    ({}, 0, "escrow_capturable_not_price"),
    ({"operator": "0x000000000000000000000000000000000000bEEF"}, 5_000_000, "operator_not_the_bank"),
    ({"feeReceiver": "0x" + "0" * 40}, 5_000_000, "fee_receiver_not_the_bank"),
    ({"maxFeeBps": 1000, "minFeeBps": 1000}, 5_000_000, "fee_not_tariff"),
    ({"minFeeBps": 200}, 5_000_000, "fee_not_tariff"),
    ({"salt": "7"}, 5_000_000, "salt_not_this_deal"),
    ({"refundExpiry": 1}, 5_000_000, "refund_expiry_before_authorization_expiry"),
    ({"payer": BETA}, 5_000_000, "payer_not_a_buyer"),
])
def test_seller_checks_escrow_before_delivering(seller, monkeypatch, over, capturable, reason):
    _, _, client = seller
    o = offer(client)
    monkeypatch.setattr(seller_mod.escrow, "read_state", lambda h: {"collected": False, "capturable": capturable,
                                                                    "refundable": 0})
    monkeypatch.setattr(seller_mod, "read_retry", lambda fn, ok: fn())
    posted = []
    monkeypatch.setattr(seller_mod, "bank_post", lambda path, body, seller_id: posted.append((path, body)))
    body = {"deal_id": "d-1", "asset": "EURC", "payment_info": _pi(o, **over), "n": 0, "tier": "T1", "rule": "B"}
    assert client.post(f"/order/{o['offer_id']}/escrow", json=body).status_code == 401  # only the bank may announce
    r = client.post(f"/order/{o['offer_id']}/escrow", json=body, headers={"x-bank-token": config.BANK_TO_SELLER_TOKEN})
    if reason is None:
        assert r.status_code == 200
        assert posted[0][0] == "/api/sellers/s-beta/keys"  # the manifest key first, then the signed delivery
        path, payload = posted[-1]
        assert path == "/api/deals/d-1/delivery" and len(payload["items"]) == 20
        assert payload["manifest"]["merkle_root"] == receipts.merkle_root(payload["items"])
        assert receipts.manifest_signer(payload["manifest"], payload["signature"]) == posted[0][1]["address"]
    else:
        assert r.status_code == 409 and r.json()["detail"] == reason
        assert posted == []


def test_seller_registers_its_key_before_every_delivery(seller, monkeypatch):
    """A bank restarted on a fresh DB forgets the seller's manifest key: registering it (idempotent) before each
    delivery keeps deliveries from failing BAD_SIGNATURE (seen when the scenario runner switched bank profiles)."""
    _, _, client = seller
    monkeypatch.setattr(seller_mod.escrow, "read_state", lambda h: {"collected": False, "capturable": 5_000_000,
                                                                    "refundable": 0})
    monkeypatch.setattr(seller_mod, "read_retry", lambda fn, ok: fn())
    posted = []
    monkeypatch.setattr(seller_mod, "bank_post", lambda path, body, seller_id: posted.append(path))
    for deal_id in ("d-1", "d-2"):
        o = offer(client, deal_id=deal_id)
        body = {"deal_id": deal_id, "asset": "EURC", "payment_info": _pi(o, salt=str(seller_mod.escrow.salt(deal_id, 0))),
                "n": 0, "tier": "T1", "rule": "B"}
        r = client.post(f"/order/{o['offer_id']}/escrow", json=body, headers={"x-bank-token": config.BANK_TO_SELLER_TOKEN})
        assert r.status_code == 200
    keys = [p for p in posted if p.endswith("/keys")]
    assert len(keys) == 2 and posted[0].endswith("/keys") and posted[1].endswith("/delivery")
