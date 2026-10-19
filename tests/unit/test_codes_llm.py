# Decision codes (closed list, two-level view) and the LLM wrapper in MOCK mode.
import pytest
from pydantic import BaseModel

from bank import codes, config, db, llm
from bank.codes import Code


def test_every_code_has_a_status_and_next_tool():
    assert set(codes.SPEC) == set(Code)
    for code, (status, next_tool) in codes.SPEC.items():
        assert status in ("refused", "requires_user_action") and next_tool


def test_refuse_shape_and_requires_user_action():
    d = codes.refuse(Code.VAT_LIABILITY_RISK, [codes.reason("paid_in_virtual_asset", True, "EURC to a VAT payer")])
    assert d["status"] == "requires_user_action" and d["code"] == "VAT_LIABILITY_RISK"
    assert d["next_tool"] == "confirm_or_pay_in_tczk"
    assert codes.ok()["status"] == "ok"


def test_client_view_hides_non_public_reasons_entirely():
    d = codes.refuse(Code.COUNTERPARTY_NOT_TRUSTED, [
        codes.reason("ico_mismatch_on_website", True, "IČO on the website differs from ARES", "https://ares.gov.cz"),
        codes.reason("sanctions_near_match", False, "Name 92% similar to a listed entity")])
    view = codes.client_view(d)
    assert view["code"] == "UNDER_REVIEW" and view["reasons"] == []
    assert "sanction" not in str(view).lower()
    public_only = codes.refuse(Code.COUNTERPARTY_NOT_TRUSTED, [d["reasons"][0]])
    view = codes.client_view(public_only)
    assert view["code"] == "COUNTERPARTY_NOT_TRUSTED"
    assert view["reasons"] == [{"code": "ico_mismatch_on_website", "text": "IČO on the website differs from ARES",
                                "source_url": "https://ares.gov.cz"}]


def test_client_view_is_an_allowlist():
    d = codes.refuse(Code.AMOUNT_ABOVE_LIMIT, [codes.reason("per_deal_max", True, "12 € > 10 €", limit=1000)],
                     compliance_note="SAR filed", screening={"sanctions": "near match"}, deal_id="d1")
    view = codes.client_view(d)
    assert set(view) == {"status", "code", "explanation", "next_tool", "deal_id", "reasons"}
    assert "evidence" not in view["reasons"][0] and "SAR" not in str(view)


def test_client_view_under_review_is_always_neutral_and_unflagged_fails_closed():
    timeout = codes.refuse(Code.UNDER_REVIEW, [codes.reason("registry_timeout", True, "ARES timed out")])
    assert codes.client_view(timeout)["explanation"] == codes.NEUTRAL_CLIENT_TEXT
    assert codes.client_view(timeout)["reasons"] == []
    unflagged = codes.refuse(Code.COUNTERPARTY_NOT_TRUSTED, [{"code": "x", "text": "secret"}])
    assert codes.client_view(unflagged)["code"] == "UNDER_REVIEW"


class Flag(BaseModel):
    placeId: str
    plausible_warehouse: bool
    reason: str


def test_json_call_valid_answer_logged_as_mock():
    llm.register_mock("t_judge", lambda m, t: {"text": 'Sure:\n```json\n[{"placeId":"p1","plausible_warehouse":true,'
                                                      '"reason":"sklad"}]\n```'})
    value, ok = llm.json_call("fast", "sys", "judge these", list[Flag], default=[], stage="t_judge", deal_id="d1")
    assert ok and value[0].placeId == "p1"
    row = db.one("SELECT model, cost_usd, ok FROM timings WHERE stage='t_judge'")
    assert row == {"model": "mock", "cost_usd": 0.0, "ok": 1}


def test_json_call_retries_once_then_default():
    answers = iter(["garbage", "still garbage"])
    llm.register_mock("t_bad", lambda m, t: {"text": next(answers)})
    value, ok = llm.json_call("fast", "sys", "x", list[Flag], default="FLAG_ALL", stage="t_bad")
    assert (value, ok) == ("FLAG_ALL", False)
    assert db.one("SELECT COUNT(*) n FROM timings WHERE stage='t_bad'")["n"] == 2


def test_json_call_second_attempt_sees_the_error():
    seen = []

    def mock(messages, tools):
        seen.append(messages)
        return {"text": "nope" if len(seen) == 1 else '{"placeId":"p","plausible_warehouse":false,"reason":"r"}'}

    llm.register_mock("t_retry", mock)
    value, ok = llm.json_call("fast", "sys", "x", Flag, default=None, stage="t_retry")
    assert ok and value.placeId == "p"
    assert "failed validation" in seen[1][-1]["content"] and seen[1][-2]["role"] == "assistant"


def test_fault_llm_bad_json_ends_in_safe_default(monkeypatch):
    monkeypatch.setenv("FAULTS", "llm_bad_json")
    config.reload()
    llm.register_mock("t_fault", lambda m, t: {"text": '{"placeId":"p","plausible_warehouse":true,"reason":"r"}'})
    value, ok = llm.json_call("fast", "sys", "x", Flag, default="FLAG_ALL", stage="t_fault")
    assert (value, ok) == ("FLAG_ALL", False)
    assert db.one("SELECT COUNT(*) n FROM timings WHERE stage='t_fault'")["n"] == 2


def test_tool_use_mock_and_refusal():
    llm.register_mock("t_tool", lambda m, t: {"text": "paying", "tool_use": [{"name": "pay", "input": {"deal_id": "d1"}}]})
    r = llm.create("main", "sys", [{"role": "user", "content": "go"}], stage="t_tool", tools=[{"name": "pay"}])
    assert r.stop_reason == "tool_use" and r.content[1].name == "pay" and r.content[1].id.startswith("toolu_mock_")
    llm.register_mock("t_refuse", lambda m, t: {"text": "", "stop_reason": "refusal"})
    with pytest.raises(llm.LLMRefusal):
        llm.create("main", "sys", [{"role": "user", "content": "x"}], stage="t_refuse")
    llm.register_mock("t_refuse_json", lambda m, t: {"stop_reason": "refusal"})
    assert llm.json_call("fast", "s", "x", Flag, default="SAFE", stage="t_refuse_json") == ("SAFE", False)


def test_missing_mock_and_missing_region_are_clear(monkeypatch):
    with pytest.raises(RuntimeError, match="no mock registered"):
        llm.create("main", "s", [], stage="nobody_registered_this")
    monkeypatch.setenv("MOCK", "0")
    monkeypatch.setenv("AWS_REGION", "")
    config.reload()
    monkeypatch.setattr(llm, "_client", None)
    with pytest.raises(RuntimeError, match="TODO\\(credential\\)"):
        llm.create("main", "s", [{"role": "user", "content": "x"}], stage="live")
    assert db.one("SELECT ok FROM timings WHERE stage='live'")["ok"] == 0


def test_real_request_shape_respects_model_rules(monkeypatch):
    sent = []

    class FakeMessages:
        def create(self, **kw):
            sent.append(kw)
            from types import SimpleNamespace as NS
            return NS(content=[NS(type="text", text="hi")], stop_reason="end_turn",
                      usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0))

    monkeypatch.setenv("MOCK", "0")
    monkeypatch.setenv("MODEL_FAST", "anthropic.claude-haiku-4-5")
    config.reload()
    monkeypatch.setattr(llm, "_client", type("C", (), {"messages": FakeMessages()})())
    llm.create("main", "sys", [{"role": "user", "content": "x"}], stage="s", effort="low", tools=[{"name": "pay"}])
    llm.create("fast", "sys", [{"role": "user", "content": "x"}], stage="s", effort="low")
    main, fast = sent
    assert main["model"] == "anthropic.claude-opus-5-5" and main["output_config"] == {"effort": "low"}
    assert "tool_choice" not in main and "thinking" not in main and main["cache_control"] == {"type": "ephemeral"}
    assert fast["model"] == "anthropic.claude-haiku-4-5" and "output_config" not in fast
    assert db.one("SELECT SUM(cost_usd) c FROM timings WHERE stage='s'")["c"] > 0


def test_extract_json_handles_prose():
    assert llm.extract_json('Result: {"a": [1, 2]} trailing') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        llm.extract_json("no json here")
