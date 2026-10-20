# scripts/tatum_subscribe.py without the network: the dry run never shows the secret or the key, live mode refuses
# without a public https URL, and a live run (fake Tatum transport) sets the HMAC secret first, then subscribes each
# seller wallet once on base-sepolia with the enriched template.
import json

import httpx
import pytest

from bank import config
from scripts import tatum_subscribe as ts

SECRET, KEY = "unit-hmac-secret-xyz", "unit-tatum-key-abc"


@pytest.fixture
def env(monkeypatch):
    def set_(**kv):
        for k, v in kv.items():
            monkeypatch.setenv(k, v)
        config.reload()
    set_(TATUM_HMAC_SECRET=SECRET, TATUM_API_KEY=KEY, PUBLIC_BASE_URL="")
    return set_


@pytest.fixture
def fake_tatum(monkeypatch):
    calls, existing = [], []

    def handler(req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body, req.headers.get("x-api-key")))
        if req.method == "GET":
            return httpx.Response(200, json=existing)
        return httpx.Response(200, json={"id": f"sub-{len(calls)}"})

    monkeypatch.setattr(ts, "client", lambda: httpx.Client(base_url=ts.API, headers={"x-api-key": config.TATUM_API_KEY},
                                                           transport=httpx.MockTransport(handler)))
    return calls, existing


def test_dry_run_prints_every_request_without_the_secret_or_the_key(env, capsys, fake_tatum):
    assert ts.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert SECRET not in out and KEY not in out and fake_tatum[0] == []  # nothing sent
    assert out.count("POST https://api.tatum.io/v4/subscription") == len(ts.seller_wallets()) == 4
    assert "PUT https://api.tatum.io/v4/subscription" in out and '"hmacSecret": "***"' in out
    assert '"chain": "base-sepolia"' in out and '"templateId": "enriched"' in out and "INCOMING_FUNGIBLE_TX" in out
    for addr in ts.seller_wallets().values():
        assert addr in out
    for argv in (["list", "--dry-run"], ["chains", "--dry-run"], ["delete", "--all", "--dry-run"]):
        assert ts.main(argv) == 0
    assert SECRET not in capsys.readouterr().out


def test_live_refuses_without_a_public_url(env, fake_tatum):
    with pytest.raises(SystemExit, match="PUBLIC_BASE_URL"):
        ts.main([])
    for bad in ("http://abc.trycloudflare.com", "https://localhost:8000", "https://bank.test", "https://127.0.0.1"):
        env(PUBLIC_BASE_URL=bad)
        with pytest.raises(SystemExit, match="public https URL"):
            ts.main([])
    assert fake_tatum[0] == []


def test_live_refuses_without_the_hmac_secret(env, fake_tatum):
    env(PUBLIC_BASE_URL="https://abc.trycloudflare.com", TATUM_HMAC_SECRET="")
    with pytest.raises(SystemExit, match="TATUM_HMAC_SECRET"):
        ts.main([])
    assert fake_tatum[0] == []


def test_live_sets_the_secret_then_subscribes_each_seller_once(env, fake_tatum, capsys):
    calls, existing = fake_tatum
    env(PUBLIC_BASE_URL="https://abc.trycloudflare.com/")
    wallets = list(ts.seller_wallets().values())
    url = "https://abc.trycloudflare.com/webhooks/tatum"
    existing.append({"id": "old", "type": "INCOMING_FUNGIBLE_TX",
                     "attr": {"chain": "base-sepolia", "address": wallets[0].lower(), "url": url}})
    assert ts.main([]) == 0
    assert [c[0] for c in calls] == ["GET", "PUT"] + ["POST"] * (len(wallets) - 1)  # the first is subscribed
    assert calls[1][2] == {"hmacSecret": SECRET} and all(c[3] == KEY for c in calls)
    posted = [c[2] for c in calls if c[0] == "POST"]
    assert [p["attr"]["address"] for p in posted] == wallets[1:]
    assert all(p["attr"]["url"] == url and p["finality"] == "confirmed" for p in posted)
    assert SECRET not in capsys.readouterr().out


def test_delete_all_removes_only_ours(env, fake_tatum):
    calls, existing = fake_tatum
    wallet = list(ts.seller_wallets().values())[1]
    existing += [{"id": "mine", "type": "INCOMING_FUNGIBLE_TX", "attr": {"chain": "base-sepolia", "address": wallet}},
                 {"id": "other", "type": "INCOMING_FUNGIBLE_TX", "attr": {"chain": "base-sepolia", "address": "0x" + "1" * 40}},
                 {"id": "mainnet", "type": "INCOMING_FUNGIBLE_TX", "attr": {"chain": "base-mainnet", "address": wallet}}]
    assert ts.main(["delete", "--all"]) == 0
    assert [(c[0], c[1]) for c in calls if c[0] == "DELETE"] == [("DELETE", "/v4/subscription/mine")]
    with pytest.raises(SystemExit):
        ts.main(["delete"])
