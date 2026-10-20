# Slow / failing Bedrock never breaks a caller (bank/llm.py): per-stage wall-clock budget, retries with backoff on
# throttling / 5xx / timeouts only, no retry on expired credentials, a cool-down after a failure, every attempt
# logged; FAULTS=llm_* (MOCK and live) and a fake SDK client (no network). Then each caller's fallback.
import threading
import time
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from agents import buyer, offline, seller_agent
from bank import config, db, explain, journal, llm

TASK = "20 warehouses in Brno with rating above 4.0 and a website"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setenv("LLM_BACKOFF_S", "0.01")
    monkeypatch.setattr(llm, "_down", {})
    config.reload()
    llm.register_mock("t", lambda m, t: {"text": "hi"})


def faults(monkeypatch, value, **env):
    monkeypatch.setenv("FAULTS", value)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.reload()


def attempts(stage):
    return [(r["ok"], r["error"]) for r in db.rows("SELECT ok, error FROM timings WHERE stage=? ORDER BY id", (stage,))]


def ask(stage="t", role="fast"):
    llm.MOCKS.setdefault(stage, lambda m, t: {"text": "hi"})
    return llm.create(role, "sys", [{"role": "user", "content": "x"}], stage=stage)


# ---- FAULTS=llm_* ----------------------------------------------------------------------------------------------

def test_throttle_once_is_retried_and_both_attempts_are_logged(monkeypatch):
    from bank import timing
    faults(monkeypatch, "llm_throttle_once")
    assert llm.text_of(ask()) == "hi"
    assert attempts("t") == [(0, "throttled"), (1, None)]
    stats = next(s for s in timing.llm_stats() if s["stage"] == "t")
    assert stats["n"] == 1 and stats["failed"] == {"throttled": 1} and stats["p95"] is not None


def test_persistent_throttle_and_5xx_give_up_after_the_retries_then_cool_down(monkeypatch):
    faults(monkeypatch, "llm_throttle,llm_error")
    with pytest.raises(llm.LLMUnavailable) as e:
        ask()
    assert e.value.kind == "throttled" and len(attempts("t")) == 1 + config.LLM_RETRIES
    t0 = time.monotonic()
    with pytest.raises(llm.LLMUnavailable, match="not retried"):  # cool-down: fails fast, no new attempt
        ask()
    assert time.monotonic() - t0 < 0.05 and attempts("t")[-1] == (0, "cooldown")
    monkeypatch.setenv("FAULTS", "llm_error")
    config.reload()
    with pytest.raises(llm.LLMUnavailable) as e:
        ask("t2")
    assert e.value.kind == "server" and len(attempts("t2")) == 1 + config.LLM_RETRIES


def test_expired_credentials_are_one_clear_error_without_retry(monkeypatch):
    faults(monkeypatch, "llm_expired")
    with pytest.raises(llm.LLMUnavailable, match="credentials expired") as e:
        ask()
    assert e.value.kind == "credentials" and attempts("t") == [(0, "credentials")]


def test_timeout_ends_at_the_stage_budget_and_slow_answers_within_it(monkeypatch):
    faults(monkeypatch, "llm_timeout")
    monkeypatch.setitem(config.LLM_BUDGET_S, "t", 0.3)
    t0 = time.monotonic()
    with pytest.raises(llm.LLMUnavailable) as e:
        ask()
    assert e.value.kind == "timeout" and 0.25 < time.monotonic() - t0 < 0.6
    faults(monkeypatch, "llm_slow", LLM_FAULT_SLOW_S="0.05")
    assert llm.text_of(ask("t_slow")) == "hi" and attempts("t_slow") == [(1, None)]


def test_a_fault_can_target_one_stage_and_refusal_is_a_refusal(monkeypatch):
    faults(monkeypatch, "llm_expired@seller,llm_refusal@t_ref")
    assert llm.text_of(ask("t")) == "hi"
    with pytest.raises(llm.LLMRefusal):
        ask("t_ref")


def test_json_call_returns_the_safe_default_on_any_failure(monkeypatch):
    for f in ("llm_timeout", "llm_throttle", "llm_error", "llm_expired", "llm_refusal"):
        faults(monkeypatch, f)
        monkeypatch.setitem(config.LLM_BUDGET_S, "t_json", 0.2)
        monkeypatch.setattr(llm, "_down", {})
        llm.MOCKS.setdefault("t_json", lambda m, t: {"text": "{}"})
        assert llm.json_call("fast", "s", "x", dict, "SAFE", stage="t_json") == ("SAFE", False)


# ---- a fake SDK client: real exception classes, no network -------------------------------------------------------

def _status(cls, code, text):
    req = httpx2.Request("POST", "https://bedrock-runtime.eu-central-1.amazonaws.com/model/x/invoke")
    return cls(text, response=httpx2.Response(code, request=req), body=None)


def _ok():
    return NS(content=[NS(type="text", text="live")], stop_reason="end_turn",
              usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0))


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("MOCK", "0")
    monkeypatch.setenv("MODEL_FAST", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    monkeypatch.setenv("MODEL_MAIN", "eu.anthropic.claude-opus-4-5-20251101-v1:0")
    config.reload()
    script = []
    sent = []

    class Messages:
        def create(self, **kw):
            sent.append(kw)
            step = script.pop(0) if script else _ok
            return step()
    monkeypatch.setattr(llm, "_client", NS(messages=Messages()))
    return script, sent


def test_live_429_then_answer_with_per_attempt_timeouts(live):
    script, sent = live

    def throttled():
        raise _status(anthropic.RateLimitError, 429, "ThrottlingException: Too many requests")
    script.append(throttled)
    assert llm.text_of(ask("live1")) == "live"
    assert attempts("live1") == [(0, "throttled"), (1, None)]
    timeout = sent[-1]["timeout"]
    assert timeout.connect == config.LLM_CONNECT_S and timeout.read <= config.LLM_READ_S["fast"]
    row = db.one("SELECT input_tokens, cost_usd FROM timings WHERE stage='live1' AND ok=1")
    assert row["input_tokens"] == 100 and row["cost_usd"] > 0


def test_live_expired_token_and_bad_request_are_not_retried(live):
    script, _ = live

    def expired():
        raise _status(anthropic.PermissionDeniedError, 403, "The security token included in the request is expired")

    def bad():
        raise _status(anthropic.BadRequestError, 400, "messages: field required")
    script.append(expired)
    with pytest.raises(llm.LLMUnavailable, match="credentials expired") as e:
        ask("live2")
    assert e.value.kind == "credentials" and attempts("live2") == [(0, "credentials")]
    script.append(bad)
    with pytest.raises(llm.LLMUnavailable) as e:
        ask("live3")
    assert e.value.kind == "bad_request" and attempts("live3") == [(0, "bad_request")]


def test_live_hanging_call_is_cut_at_the_wall_clock_and_its_late_tokens_are_logged(live, monkeypatch):
    script, _ = live
    release = threading.Event()

    def hang():
        release.wait(5)
        return _ok()
    script.append(hang)
    monkeypatch.setitem(config.LLM_BUDGET_S, "live4", 0.3)
    t0 = time.monotonic()
    with pytest.raises(llm.LLMUnavailable) as e:
        ask("live4")
    assert e.value.kind == "timeout" and time.monotonic() - t0 < 0.6
    release.set()
    for _ in range(50):
        if any(err == "late" for _, err in attempts("live4")):
            break
        time.sleep(0.02)
    late = db.one("SELECT input_tokens FROM timings WHERE stage='live4' AND error='late'")
    assert late and late["input_tokens"] == 100  # billed even though nobody waited for it


def test_classify_botocore_and_connection_errors():
    from botocore.exceptions import NoCredentialsError
    assert llm.classify(NoCredentialsError()) == "credentials"
    req = httpx2.Request("POST", "https://x")
    assert llm.classify(anthropic.APIConnectionError(request=req)) == "connection"
    assert llm.classify(anthropic.APITimeoutError(request=req)) == "timeout"
    assert llm.classify(_status(anthropic.InternalServerError, 503, "ServiceUnavailableException")) == "server"
    assert llm.classify(ValueError("x")) == "error"


# ---- callers fall back to code -----------------------------------------------------------------------------------

def test_seller_turn_falls_back_to_the_code_reply_at_its_budget(monkeypatch):
    faults(monkeypatch, "llm_timeout@seller")
    monkeypatch.setitem(config.LLM_BUDGET_S, "seller", 0.3)
    t0 = time.monotonic()
    out = seller_agent.negotiate_turn("Can you do 5 €?", list_cents=600, min_cents=500, turn=2, last_cents=600,
                                      opening_cents=600)
    assert time.monotonic() - t0 < 0.6
    assert out == {"reply_text": "We can deliver for 5.00 €.", "price_eur_cents": 500, "ai_generated": False}
    assert seller_agent.price_turn("price?", list_cents=600, min_cents=500, turn=1, last_cents=None,
                                   opening_cents=600) == ("We can deliver for 6.00 €.", 600)


def test_seller_ai_label_only_on_live_model_text(live):
    script, _ = live
    script.append(lambda: NS(content=[NS(type="text", text='{"reply_text": "Deal at 5 €.", "price_eur_cents": 500}')],
                             stop_reason="end_turn", usage={}))
    out = seller_agent.negotiate_turn("5 €?", list_cents=600, min_cents=500, turn=2, last_cents=600, opening_cents=600)
    assert out == {"reply_text": "Deal at 5 €.", "price_eur_cents": 500, "ai_generated": True}


def test_buyer_stops_cleanly_when_its_model_fails_mid_loop(seeded, monkeypatch):
    bank = offline.OfflineBank.setup()
    calls = []
    original = llm.MOCKS["buyer"]

    def flaky(messages, tools):  # two good turns, then Bedrock throttles for good
        calls.append(1)
        if len(calls) > 2:
            raise llm._Fault("throttled", "ThrottlingException")
        return original(messages, tools)
    monkeypatch.setitem(llm.MOCKS, "buyer", flaky)
    r = buyer.Buyer(TASK, transport=bank.call, seller_hint="s-beta").run()
    assert r["outcome"] == "agent_unavailable" and r["payments"] == [] and "Nothing more is paid" in r["text"]
    assert db.one("SELECT COUNT(*) n FROM reservations")["n"] == 0
    stop = [e for e in journal.entries() if e["event"] == "agent_unavailable"]
    assert stop and stop[0]["payload"]["kind"] == "throttled"


def test_buyer_without_model_criteria_reads_them_by_code_or_stops(seeded, monkeypatch):
    bank = offline.OfflineBank.setup()
    faults(monkeypatch, "llm_expired@criteria")
    r = buyer.Buyer(TASK, transport=bank.call, seller_hint="s-beta").run()
    assert r["criteria"] == {"count": 20, "city": "Brno", "min_rating_exclusive": 4.0,
                             "category_keywords": ["sklad", "warehouse"], "require_website": True}
    assert r["outcome"] == "done"
    r = buyer.Buyer("buy some good warehouse data please", transport=bank.call).run()
    assert r["outcome"] == "criteria_unavailable" and r["deals"] == {} and r["tool_calls"] == []
    assert sorted(str(e["payload"]["by"]) for e in journal.entries() if e["event"] == "criteria_fallback") == \
        ["None", "code"]


def test_explanation_falls_back_to_the_template(monkeypatch):
    d = {"status": "refused", "code": "AMOUNT_ABOVE_LIMIT", "next_tool": "amend_mandate",
         "reasons": [{"code": "per_deal_max", "public": True, "text": "12 € > 10 €", "source_url": None}]}
    faults(monkeypatch, "llm_error@explain")
    out = explain.render(d, "client", ai=True)
    assert out["ai_generated"] is False and out["text"] == explain.template(d, "client")


# ---- the daily spend cap -----------------------------------------------------------------------------------------

def spent(usd, ts=None):
    db.run("INSERT INTO timings (ts, stage, ms, ok, model, cost_usd) VALUES (?, 'buyer', 1, 1, 'm', ?)",
           (time.time() if ts is None else ts, usd))


def test_daily_budget_makes_every_call_unavailable_and_the_fallbacks_apply(seeded, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_USD", "20")
    config.reload()
    spent(12.0, ts=time.time() - 86400)  # yesterday's spend does not count
    spent(19.0)
    assert ask() and llm.timing.llm_spent_today() == pytest.approx(19.0)
    spent(1.5)
    with pytest.raises(llm.LLMUnavailable) as e:
        ask()
    assert e.value.kind == "budget" and attempts("t")[-1] == (0, "budget")
    out = seller_agent.negotiate_turn("Can you do 5 €?", list_cents=600, min_cents=500, turn=2, last_cents=600,
                                      opening_cents=600)
    assert out == {"reply_text": "We can deliver for 5.00 €.", "price_eur_cents": 500, "ai_generated": False}
    r = buyer.Buyer(TASK, transport=offline.OfflineBank.setup().call, seller_hint="s-beta").run()
    assert (r["outcome"], r["payments"]) == ("agent_unavailable", []), r["outcome"]
    assert db.one("SELECT COUNT(*) n FROM reservations")["n"] == 0
    from bank import ui
    page = ui.templates.get_template("_feed.html").render(panel=ui.panel(), rows=[], alarms=[])
    assert "model budget used today: $20.50 of $20" in page
    monkeypatch.setenv("LLM_DAILY_USD", "0")  # 0 = no cap
    config.reload()
    assert ask()
