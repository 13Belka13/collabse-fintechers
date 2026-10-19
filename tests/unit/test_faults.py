# FAULTS injection at unit level (SPEC §19.2); the same faults run end to end on a chain in scripts/scenarios.py
# (F01-F05). settle_fail_once is covered on anvil in test_facilitator_chain.py.
import pytest

from bank import codes, config, db, screening, verifier
from bank.codes import Code

GAMA_ICO = "59093994"


def _faults(monkeypatch, value):
    monkeypatch.setenv("FAULTS", value)
    config.reload()


def test_registry_timeout_fails_closed_and_the_client_sees_only_the_neutral_text(seeded, monkeypatch):
    _faults(monkeypatch, "registry_timeout")
    res = screening.check(GAMA_ICO)
    assert res["level"] == "review"
    timeouts = [r for r in res["reasons"] if r["code"] == "registry_timeout"]
    assert timeouts and all(r["public"] is False for r in timeouts)
    view = codes.client_view(codes.refuse(Code.UNDER_REVIEW, res["reasons"]))
    assert view["code"] == "UNDER_REVIEW" and view["explanation"] == codes.NEUTRAL_CLIENT_TEXT and view["reasons"] == []
    assert screening.cached(GAMA_ICO)["level"] == "review"  # the money path reads the fail-closed result


def test_stale_screening_cache_fails_closed(seeded, clock):
    """After the 1 h TTL the money path has no check result -> pay() refuses UNDER_REVIEW (no network on that path)."""
    assert screening.check(GAMA_ICO)["level"] == "ok"
    clock.tick(3599)
    assert screening.cached(GAMA_ICO) is not None
    clock.tick(2)
    assert screening.cached(GAMA_ICO) is None


def test_llm_bad_json_flags_everything_to_a_human_and_captures_nothing(seeded, monkeypatch):
    from seller.app import make_items
    _faults(monkeypatch, "llm_bad_json")
    db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, criteria_json, asset, amount, state, created_at)"
           " VALUES ('d-badjson','alfa','s-gama',NULL,'EURC',5000000,'delivered',0)")
    verifier.record_delivery("d-badjson", make_items(20, "honest"))
    plan = verifier.settlement("d-badjson")
    assert plan["capture_now"] == 0 and plan["void"] == 0 and plan["held"] == 5_000_000  # all waits in escrow
    assert all(it["status"] == "held" and it["judge"]["reason"] == "judge_invalid_json" for it in plan["items"])
    calls = db.one("SELECT COUNT(*) n FROM timings WHERE stage='judge' AND model IS NOT NULL")["n"]
    assert calls == 2  # one retry, then the safe default


def _routes():
    from bank import app
    return [getattr(r, "path", "") for r in app.app.routes]


@pytest.mark.xfail(reason="D3: Tatum webhook /webhooks/tatum not built yet (L7)", strict=False)
def test_webhook_duplicate_is_counted_once():
    assert any("webhook" in p for p in _routes())
    raise AssertionError("write the duplicate-delivery check once the webhook route exists")


@pytest.mark.xfail(reason="D3: Tatum webhook /webhooks/tatum not built yet (L7)", strict=False)
def test_webhook_bad_hmac_is_rejected():
    assert any("webhook" in p for p in _routes())
    raise AssertionError("write the bad-HMAC check once the webhook route exists")
