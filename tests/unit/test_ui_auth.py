# Human actions in the bank UI are same-origin only (bank/ui_auth.py): a form on another site cannot make the logged-in
# demo person confirm a payment or change the mandate; a client without Origin/Referer needs the human token. Every
# POST route of the UI router is covered, so a route added later (ramp, approve) is checked by the same test.
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import mandate, ui

EVIL = [{"origin": "https://evil.example"}, {"referer": "https://evil.example/form.html"},
        {"origin": "http://testserver.evil.example"}, {"sec-fetch-site": "cross-site", "origin": "http://testserver"},
        {"sec-fetch-site": "same-site"}, {}, {"x-human-token": "wrong"}, {"origin": "null"}]
FORM = {"per_deal_max": "3", "confirm_above": "2,50", "budget": "30", "task_budget_max": "", "asset_EURC": "1"}


@pytest.fixture
def client(seeded):
    mandate.ensure_demo()
    app = FastAPI()
    app.include_router(ui.router)
    return TestClient(app)


def post_routes():
    out = []
    for r in ui.router.routes:
        if "POST" in getattr(r, "methods", ()):
            out.append(r.path.replace("{deal_id}", "d-x").replace("{version}", "1").replace("{idx}", "0")
                       .replace("{action}", "confirm").replace("{scenario}", "x"))
    return out


def test_every_ui_post_route_is_listed():
    assert set(post_routes()) >= {"/mandate/edit", "/mandate/deals/d-x/confirm", "/deciders/d-x", "/deals/d-x/accept",
                                  "/deals/d-x/accept-item/1/0", "/deals/d-x/retry-delivery", "/demo/custom",
                                  "/demo/x", "/timer/tick"}


@pytest.mark.parametrize("headers", EVIL)
def test_cross_site_or_anonymous_post_is_refused_on_every_route(client, headers):
    v0 = mandate.current(mandate.DEMO_MANDATE_ID)["version"]
    for path in post_routes():
        r = client.post(path, data=FORM, headers=headers, follow_redirects=False)
        assert r.status_code == 403 and "Refused" in r.text, (path, headers)
    assert mandate.current(mandate.DEMO_MANDATE_ID)["version"] == v0  # nothing changed


@pytest.mark.parametrize("headers", [{"origin": "http://testserver"}, {"referer": "http://testserver/mandate"},
                                     {"origin": "http://testserver", "sec-fetch-site": "same-origin"},
                                     {"sec-fetch-site": "same-origin"}, {"x-human-token": "dev-human"}])
def test_same_origin_post_or_the_human_token_is_accepted(client, headers):
    r = client.post("/mandate/edit", data=FORM, headers=headers, follow_redirects=False)
    assert r.status_code == 303 and "Version%202%20is%20in%20force" in r.headers["location"]
    m = mandate.current(mandate.DEMO_MANDATE_ID)
    assert m["version"] == 2 and m["approved_by"] == "human:alfa"


def test_pages_stay_open_without_a_token(client):
    assert client.get("/mandate").status_code == 200 and client.get("/bank").status_code == 200


def test_start_warns_on_dev_tokens_with_mock_off_but_never_refuses(monkeypatch, caplog):
    from bank import app as app_mod, config
    monkeypatch.setattr(config, "HUMAN_TOKEN", "dev-human")
    monkeypatch.setattr(config, "DECIDER_TOKENS", "")
    monkeypatch.setattr(config, "DECIDER_SECRET", "dev-human")
    monkeypatch.setattr(config, "MOCK", False)
    with caplog.at_level("WARNING", logger="bank"):
        app_mod.warn_dev_tokens()  # returns: the stand keeps starting with its current environment
    assert "HUMAN_TOKEN" in caplog.text and "decider page tokens" in caplog.text
    caplog.clear()
    monkeypatch.setattr(config, "MOCK", True)
    app_mod.warn_dev_tokens()
    assert caplog.text == ""  # MOCK=1: the printed note only
