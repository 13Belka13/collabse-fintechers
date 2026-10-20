# FAULTS injection at unit level (SPEC §19.2); the same faults run end to end on a chain in scripts/scenarios.py
# (F01-F05). settle_fail_once is covered on anvil in test_facilitator_chain.py; webhook_duplicate and
# webhook_bad_hmac in test_webhooks.py.
from bank import codes, config, db, screening
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


def test_llm_bad_json_cannot_reach_the_money_path(seeded, monkeypatch):
    """MODEL_V2 §6: no model decides an item. With every model answer broken, the delivery is checked and paid
    exactly as without the fault, and no model is called at all."""
    from tests.unit import dealkit as k
    _faults(monkeypatch, "llm_bad_json")
    w = k.world(monkeypatch)
    deal_id = k.make_deal(w)
    out = k.deliver(w, deal_id, mode="fake_data")
    assert out["accepted_items"] == 17 and k.captures(w) == [3_825_000]
    assert db.one("SELECT COUNT(*) n FROM timings WHERE model IS NOT NULL")["n"] == 0
