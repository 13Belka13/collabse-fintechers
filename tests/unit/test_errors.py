# Every failure is a clear answer, never a 500 with internals, and the bank keeps running (error-handling audit):
# a slow / failing seller behind /negotiate, a seller model that times out, a facilitator that throws, the last net.
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import config, db, deals, facilitator, journal
from tests.unit.test_deals import new_deal, world  # noqa: F401 — the fixture

TASK = {"buyer_id": "alfa", "seller_id": "s-beta", "mandate_id": "m-alfa-1", "task": {"text": "20 warehouses in Brno"}}


class Broken(httpx.Client):
    """A seller HTTP client whose /negotiate fails the given way."""

    def __init__(self, exc=None, status=None):
        super().__init__()
        self.exc, self.status = exc, status

    def post(self, path, json=None, timeout=None):
        self.waited = timeout
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, json={"detail": "boom"}, request=httpx.Request("POST", "http://s" + path))


def test_seller_timeout_on_negotiate_is_a_504_with_a_journal_line(world, monkeypatch):
    deal_id = world.bank.post("/api/deals", json=TASK).json()["deal_id"]
    seen = Broken(httpx.ReadTimeout("read timed out"))
    monkeypatch.setattr(deals, "seller_http", lambda base: seen)
    r = world.bank.post(f"/api/deals/{deal_id}/negotiate", json={"message": "price?"})
    assert r.status_code == 504 and "did not answer within 30 s" in r.json()["detail"]
    assert seen.waited == config.NEGOTIATE_TIMEOUT_S < config.BANK_HTTP_TIMEOUT_S  # timeouts nest
    assert config.LLM_BUDGET_S["seller"] < config.NEGOTIATE_TIMEOUT_S
    assert deals.get(deal_id)["state"] == "negotiating"
    assert any(e["event"] == "seller_unavailable" for e in journal.entries(deal_id))


def test_seller_error_on_negotiate_is_a_502(world, monkeypatch):
    deal_id = world.bank.post("/api/deals", json=TASK).json()["deal_id"]
    monkeypatch.setattr(deals, "seller_http", lambda base: Broken(status=500))
    r = world.bank.post(f"/api/deals/{deal_id}/negotiate", json={"message": "price?"})
    assert r.status_code == 502 and "could not be reached" in r.json()["detail"]


def test_seller_model_timeout_still_gives_a_code_offer_unlabelled(world, monkeypatch):
    monkeypatch.setenv("FAULTS", "llm_timeout@seller")
    monkeypatch.setenv("LLM_BUDGET_S_SELLER", "0.3")
    config.reload()
    deal_id, reply = new_deal(world)
    assert reply["offer"]["price"] == "5,00 €" and "We can deliver for 5.00 €." in reply["reply_text"]
    assert deals.get(deal_id)["state"] == "offered"
    r = world.seller.post("/negotiate", json={"deal_id": "d-x", "seller_id": "s-beta", "message": "price?"}).json()
    assert r["ai_generated"] is False
    assert db.one("SELECT error FROM timings WHERE stage='seller' AND ok=0")["error"] == "timeout"


def test_bank_last_net_answers_json_or_html_without_a_traceback(monkeypatch):
    from bank import app as bank_app
    app = FastAPI()
    app.add_exception_handler(Exception, bank_app.internal_error)

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("secret internals")

    @app.get("/page")
    def page():
        raise RuntimeError("secret internals")
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/boom")
    assert r.status_code == 500 and r.json()["error"] == "internal_error" and "secret" not in r.text
    r = c.get("/page")
    assert r.status_code == 500 and r.headers["content-type"].startswith("text/html") and "secret" not in r.text
    assert bank_app.app.exception_handlers.get(Exception) is bank_app.internal_error


def test_facilitator_settle_error_is_a_clean_failure_not_a_500(seeded, monkeypatch):
    from tests.unit.test_facilitator import ALFA, body, pay, req
    monkeypatch.setenv("BANK_RELAYER_PK", "0x" + "11" * 32)
    config.reload()

    class Exploding:
        def settle(self, payload, requirements):
            raise ConnectionError("RPC down")
    monkeypatch.setattr(facilitator, "facilitator", lambda: Exploding())
    app = FastAPI()
    app.include_router(facilitator.router)
    r = req()
    res = TestClient(app).post("/x402/settle", json=body(pay(r), r))
    assert res.status_code == 200
    out = res.json()
    assert out["success"] is False and out["errorReason"] == "facilitator_error" and out["payer"] == ALFA
    assert db.rows("SELECT * FROM onchain_txs") == []
    e = journal.entries()[0]
    assert e["event"] == "x402_settle_failed" and "RPC down" in e["payload"]["error"]


def test_supported_without_a_relayer_key_is_a_clear_503(monkeypatch):
    monkeypatch.setenv("BANK_RELAYER_PK", "")
    config.reload()
    facilitator.reset()
    app = FastAPI()
    app.include_router(facilitator.router)
    r = TestClient(app).get("/x402/supported")
    assert r.status_code == 503 and "BANK_RELAYER_PK missing" in r.json()["detail"]


def test_bank_feed_shows_a_stopped_agent_and_a_silent_seller(seeded):
    from bank import ui
    journal.append("agent:buyer", "agent_unavailable", {"kind": "timeout", "text": "stopped"}, deal_id="d-x")
    journal.append("bank", "seller_unavailable", {"step": "negotiate", "error": "ReadTimeout"}, deal_id="d-x")
    rows = [r for r in ui.feed("client") if r.get("event") in ("agent_unavailable", "seller_unavailable")]
    assert {r["event"] for r in rows} == {"agent_unavailable", "seller_unavailable"} and all(r["tone"] == "bad" for r in rows)
    app = FastAPI()
    app.include_router(ui.router)
    html = TestClient(app).get("/bank/feed").text
    assert "AI agent stopped (timeout): nothing more is paid" in html and "the offer is unchanged" in html
