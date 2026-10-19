# Claude on Amazon Bedrock (SPEC §7): AnthropicBedrock (InvokeModel; Mantle via BEDROCK_CLIENT=mantle), models
# from .env, every call logged
# with tokens and cost. MOCK=1 -> scripted replies registered per stage (offline dev, CI, stage fallback).
# No messages.parse / structured outputs on Bedrock: json_call = JSON in the answer + pydantic + one retry.
# Opus 5.5 / Haiku 5.5: no `thinking` param, no forced tool_choice, effort set explicitly.
# Claude 4.5 (the only Claude in the hackathon account) takes no output_config.effort. Never send assistant prefill.
# The prompt cache sits on the system block: it works on both the InvokeModel and the Mantle path.
import itertools
import json
import os
import time
from types import SimpleNamespace
from typing import Any, Callable

from pydantic import TypeAdapter, ValidationError

from bank import config, timing

_client = None
_mock_ids = itertools.count(1)
MOCKS: dict[str, Callable[[list, list | None], dict]] = {}


class LLMRefusal(Exception):
    """stop_reason == 'refusal' — show a plain message to the human, never move money on it."""


def model_for(role: str) -> str:
    return config.MODEL_MAIN if role == "main" else config.MODEL_FAST


def accepts_effort(model: str) -> bool:
    return not any(f"{fam}-4-5" in model for fam in ("haiku", "sonnet", "opus"))


def client():
    global _client
    if _client is None:
        from anthropic import AnthropicBedrock, AnthropicBedrockMantle
        if not config.AWS_REGION:
            raise RuntimeError("AWS_REGION is empty (TODO(credential)): run with MOCK=1 until the AWS Toolbox")
        # credentials: temporary keys from the access portal (AWS_ACCESS_KEY_ID/_SECRET_ACCESS_KEY/_SESSION_TOKEN,
        # read by boto3), an SSO profile (aws sso login), or a Bedrock API key — the profile only when it is set
        profile = None if config.BEDROCK_API_KEY else (config.AWS_PROFILE or None)
        for k in [k for k, v in os.environ.items() if k.startswith("AWS_") and not v]:
            del os.environ[k]  # an empty AWS_PROFILE= line in .env is a ProfileNotFound in boto3
        cls = AnthropicBedrockMantle if config.BEDROCK_CLIENT == "mantle" else AnthropicBedrock
        _client = cls(aws_region=config.AWS_REGION, aws_profile=profile)
    return _client


def register_mock(stage: str, fn: Callable[[list, list | None], dict]):
    """fn(messages, tools) -> {"text": str} and/or {"tool_use": [{"name": ..., "input": {...}}]}."""
    MOCKS[stage] = fn


def _mock_response(stage: str, messages: list, tools: list | None):
    fn = MOCKS.get(stage)
    if fn is None:
        raise RuntimeError(f"MOCK=1 but no mock registered for stage {stage!r}")
    out = fn(messages, tools)
    content = []
    if out.get("text"):
        content.append(SimpleNamespace(type="text", text=out["text"]))
    for call in out.get("tool_use", []):
        content.append(SimpleNamespace(type="tool_use", id=f"toolu_mock_{next(_mock_ids)}",
                                       name=call["name"], input=call["input"]))
    stop = out.get("stop_reason") or ("tool_use" if out.get("tool_use") else "end_turn")
    usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage, model="mock")


def create(role: str, system: str, messages: list, *, stage: str, deal_id: str | None = None,
           tools: list | None = None, effort: str | None = "low", max_tokens: int = 4096):
    """One Messages call. Returns the SDK response (or a mock with the same shape). Logs cost."""
    model = "mock" if config.MOCK else model_for(role)
    t0 = time.perf_counter()
    ok = False
    resp = None
    try:
        if config.MOCK:
            resp = _mock_response(stage, messages, tools)
        else:
            kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages,
                                      "system": [{"type": "text", "text": system,
                                                  "cache_control": {"type": "ephemeral"}}]}
            if tools:
                kwargs["tools"] = tools  # tool_choice stays "auto": forced choice is a 400 on Opus 5.5
            if effort and accepts_effort(model):
                kwargs["output_config"] = {"effort": effort}
            resp = client().messages.create(**kwargs)
        ok = True
    finally:
        usage = getattr(resp, "usage", None) or {}
        timing.log_llm(stage, deal_id, model, usage, (time.perf_counter() - t0) * 1000, ok)
    if resp.stop_reason == "refusal":
        raise LLMRefusal(getattr(getattr(resp, "stop_details", None), "explanation", None) or "model refused")
    return resp


def text_of(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")


def extract_json(text: str):
    """First JSON object/array in the text (models sometimes wrap it in prose or ``` fences)."""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in the answer")
    obj, _ = json.JSONDecoder().raw_decode(text[min(starts):])
    return obj


def json_call(role: str, system: str, prompt: str, schema, default, *, stage: str, deal_id: str | None = None,
              effort: str | None = "low", max_tokens: int = 4096):
    """Ask for JSON matching `schema` (pydantic model or type); validate; one retry with the error;
    then `default` (a safe value such as 'flag everything to a human'). Returns (value, ok)."""
    adapter = TypeAdapter(schema)
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        try:
            resp = create(role, system, messages, stage=stage, deal_id=deal_id, effort=effort, max_tokens=max_tokens)
        except LLMRefusal:
            return default, False
        text = text_of(resp)
        if "llm_bad_json" in config.FAULTS:  # SPEC §19.2: garbage twice -> safe default, money never moves
            text = "this is not json"
        try:
            return adapter.validate_python(extract_json(text)), True
        except (ValueError, ValidationError) as e:
            messages = messages + [{"role": "assistant", "content": text or "(empty)"},
                                   {"role": "user", "content": f"Your answer failed validation: {str(e)[:500]}. "
                                                               "Reply with the JSON only, matching the schema."}]
    return default, False
