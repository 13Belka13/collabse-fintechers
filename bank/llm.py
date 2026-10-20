# Claude on Amazon Bedrock (SPEC §7): AnthropicBedrock (InvokeModel; Mantle via BEDROCK_CLIENT=mantle), models
# from .env, every call logged
# with tokens and cost. MOCK=1 -> scripted replies registered per stage (offline dev, CI, stage fallback).
# No messages.parse / structured outputs on Bedrock: json_call = JSON in the answer + pydantic + one retry.
# Opus 5.5 / Haiku 5.5: no `thinking` param, no forced tool_choice, effort set explicitly.
# Claude 4.5 (the only Claude in the hackathon account) takes no output_config.effort. Never send assistant prefill.
# The prompt cache sits on the system block: it works on both the InvokeModel and the Mantle path.
# A slow or failing model never breaks a caller: every call has a wall-clock budget per stage (config.LLM_BUDGET_S),
# per-attempt connect/read timeouts, bounded retries with backoff + jitter on throttling / 5xx / timeouts / connection
# errors, no retry on 4xx; then LLMUnavailable, and each caller falls back to code (seller: template reply, buyer: stops,
# explain: template). After a failure the (model, stage) pair cools down (COOLDOWN_S): its calls fail fast.
# Above the daily spend cap (config.LLM_DAILY_USD) every call is LLMUnavailable('budget'): the same fallbacks.
import itertools
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from types import SimpleNamespace
from typing import Any, Callable

from pydantic import TypeAdapter, ValidationError

from bank import config, timing

_client = None
_mock_ids = itertools.count(1)
MOCKS: dict[str, Callable[[list, list | None], dict]] = {}


_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bedrock")  # wall-clock cap: the caller stops waiting
_down: dict[tuple[str, str], tuple[float, str]] = {}  # (model, stage) -> (cooldown until, kind)
_down_lock = threading.Lock()
COOLDOWN_S = {"throttled": 10.0, "server": 10.0, "connection": 10.0}  # timeout, credentials: 30 s
MIN_ATTEMPT_S = 1.0  # no new attempt with less budget left than this
RETRYABLE = {"timeout", "throttled", "server", "connection"}
MESSAGES = {
    "timeout": "the model did not answer in time",
    "throttled": "Bedrock is throttling requests (429)",
    "server": "Bedrock returned a server error (5xx)",
    "connection": "Bedrock could not be reached",
    "credentials": "Bedrock credentials expired or missing: refresh the AWS keys in .env and restart, or run MOCK=1",
    "access": "Bedrock denied access to the model (check the model id and the account's model access)",
    "bad_request": "Bedrock rejected the request (400)",
    "error": "the model call failed",
    "budget": "today's model budget (LLM_DAILY_USD) is used up",
}


class LLMRefusal(Exception):
    """stop_reason == 'refusal' — show a plain message to the human, never move money on it."""


class LLMUnavailable(RuntimeError):
    """No usable answer: timeout, throttling, 5xx, connection, credentials, a 4xx, or a cool-down after one.
    kind is one of MESSAGES; callers fall back to code and never move money on it."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class _Fault(Exception):
    """A simulated Bedrock failure (FAULTS=llm_*), classified like the real one."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def classify(exc: BaseException) -> str:
    """Exception from the SDK, botocore or the wall-clock cap -> a kind in MESSAGES (RETRYABLE ones are retried)."""
    if isinstance(exc, (_Fault, LLMUnavailable)):
        return exc.kind
    if isinstance(exc, (FutureTimeout, TimeoutError)):
        return "timeout"
    name, text = type(exc).__name__, str(exc).lower()
    status = getattr(exc, "status_code", None)
    if type(exc).__module__.startswith("botocore") or "credential" in text or "token" in name.lower() \
            or "expired" in text or "security token" in text:
        return "credentials"
    if "Timeout" in name or status in (408,):
        return "timeout"
    if status == 429 or "throttl" in text:
        return "throttled"
    if isinstance(status, int) and status >= 500:
        return "server"
    if status in (401, 403):
        return "access"
    if isinstance(status, int) and 400 <= status < 500:
        return "bad_request"
    if "Connection" in name:
        return "connection"
    return "error"


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
        # retries are ours (budget-aware); per-attempt timeouts are set on each request
        _client = cls(aws_region=config.AWS_REGION, aws_profile=profile, max_retries=0)
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


def budget_s(stage: str) -> float:
    return config.LLM_BUDGET_S.get(stage, config.LLM_BUDGET_S["*"])


def _fault(name: str, stage: str) -> bool:
    return name in config.FAULTS or f"{name}@{stage}" in config.FAULTS


def _inject(stage: str, attempt: int, deadline: float):
    """FAULTS for the model, in MOCK and live mode (tests and the demo): llm_slow (answers LLM_FAULT_SLOW_S late),
    llm_timeout (never answers), llm_throttle / llm_throttle_once (429 on every / the first attempt), llm_error (500),
    llm_expired (expired credentials), llm_refusal. 'name@stage' limits a fault to one stage (llm_timeout@seller).
    Returns a refusal response or None; raises _Fault for the failures. Nothing is sent to Bedrock on a fault."""
    if _fault("llm_expired", stage):
        raise _Fault("credentials", "FAULTS=llm_expired: the security token included in the request is expired")
    if _fault("llm_throttle", stage) or (_fault("llm_throttle_once", stage) and attempt == 0):
        raise _Fault("throttled", "FAULTS=llm_throttle: ThrottlingException (429)")
    if _fault("llm_error", stage):
        raise _Fault("server", "FAULTS=llm_error: InternalServerException (500)")
    hang = config.LLM_FAULT_SLOW_S if _fault("llm_slow", stage) else float("inf") if _fault("llm_timeout", stage) else 0
    if hang:
        left = deadline - time.monotonic()
        time.sleep(max(0.0, min(hang, left)))
        if hang > left:
            raise _Fault("timeout", f"FAULTS=llm_{'slow' if hang != float('inf') else 'timeout'}: no answer in time")
    if _fault("llm_refusal", stage):
        return SimpleNamespace(content=[], stop_reason="refusal", usage={}, model="fault",
                               stop_details=SimpleNamespace(explanation="FAULTS=llm_refusal"))
    return None


def _late(stage: str, deal_id: str | None, model: str, t0: float):
    """The caller stopped waiting (wall-clock cap); an answer that still comes is billed: log its tokens."""
    def done(fut):
        if fut.cancelled() or fut.exception() is not None:
            return
        try:
            timing.log_llm(stage, deal_id, model, getattr(fut.result(), "usage", None) or {},
                           (time.perf_counter() - t0) * 1000, False, error="late")
        except Exception:  # noqa: BLE001 — accounting only; the caller has long moved on
            pass
    return done


def _attempt(role: str, model: str, kwargs: dict, stage: str, deal_id: str | None, deadline: float, attempt: int,
             messages: list, tools: list | None):
    if deadline - time.monotonic() <= 0:
        raise _Fault("timeout", "the stage's time budget is spent")
    injected = _inject(stage, attempt, deadline)
    if injected is not None:
        return injected
    if config.MOCK:
        return _mock_response(stage, messages, tools)
    import anthropic
    left = deadline - time.monotonic()
    read = max(0.5, min(config.LLM_READ_S.get(role, 60.0), left))
    t0 = time.perf_counter()
    fut = _pool.submit(lambda: client().messages.create(
        **kwargs, timeout=anthropic.Timeout(read, connect=min(config.LLM_CONNECT_S, read))))
    try:
        return fut.result(timeout=left)
    except FutureTimeout:
        fut.add_done_callback(_late(stage, deal_id, model, t0))
        raise


def create(role: str, system: str, messages: list, *, stage: str, deal_id: str | None = None,
           tools: list | None = None, effort: str | None = "low", max_tokens: int = 4096, deadline: float | None = None):
    """One Messages call within the stage's wall-clock budget (or `deadline`, time.monotonic()), with retries.
    Returns the SDK response (or a mock with the same shape). Logs latency, tokens and cost of every attempt,
    failed ones included. Raises LLMRefusal on a refusal and LLMUnavailable when no answer came in time."""
    model = "mock" if config.MOCK else model_for(role)
    deadline = deadline or time.monotonic() + budget_s(stage)
    kwargs: dict[str, Any] = {}
    if not config.MOCK:
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages,
                  "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]}
        if tools:
            kwargs["tools"] = tools  # tool_choice stays "auto": forced choice is a 400 on Opus 5.5
        if effort and accepts_effort(model):
            kwargs["output_config"] = {"effort": effort}
    if config.LLM_DAILY_USD > 0 and timing.llm_spent_today() >= config.LLM_DAILY_USD:
        timing.log_llm(stage, deal_id, model, {}, 0.0, False, error="budget")
        raise LLMUnavailable("budget", f"{MESSAGES['budget']}: ${config.LLM_DAILY_USD:g} a day")
    with _down_lock:
        until, kind = _down.get((model, stage), (0.0, ""))
    if until > time.monotonic():
        timing.log_llm(stage, deal_id, model, {}, 0.0, False, error="cooldown")
        raise LLMUnavailable(kind, f"{MESSAGES[kind]}; not retried for another {until - time.monotonic():.0f} s")
    attempt = 0
    while True:
        t0 = time.perf_counter()
        try:
            resp = _attempt(role, model, kwargs, stage, deal_id, deadline, attempt, messages, tools)
        except Exception as e:  # noqa: BLE001 — classified below; nothing escapes but LLMUnavailable
            kind = classify(e)
            timing.log_llm(stage, deal_id, model, {}, (time.perf_counter() - t0) * 1000, False, error=kind)
            pause = config.LLM_BACKOFF_S * 2 ** attempt + random.uniform(0, config.LLM_BACKOFF_S)
            if kind in RETRYABLE and attempt < config.LLM_RETRIES \
                    and deadline - time.monotonic() - pause >= MIN_ATTEMPT_S:
                time.sleep(pause)
                attempt += 1
                continue
            if kind in RETRYABLE or kind == "credentials":
                with _down_lock:
                    _down[(model, stage)] = (time.monotonic() + COOLDOWN_S.get(kind, 30.0), kind)
            detail = "" if kind in RETRYABLE else str(e)[:200]  # SDK/botocore texts carry no keys
            raise LLMUnavailable(kind, MESSAGES[kind] + (f": {detail}" if detail else "")) from e
        timing.log_llm(stage, deal_id, model, getattr(resp, "usage", None) or {},
                       (time.perf_counter() - t0) * 1000, True)
        break
    with _down_lock:
        _down.pop((model, stage), None)
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
    then `default` (a safe value such as 'flag everything to a human'); a refusal or no answer in the stage's
    budget -> `default` at once. Returns (value, ok)."""
    adapter = TypeAdapter(schema)
    messages = [{"role": "user", "content": prompt}]
    deadline = time.monotonic() + budget_s(stage)  # both attempts share the stage's budget
    for attempt in range(2):
        try:
            resp = create(role, system, messages, stage=stage, deal_id=deal_id, effort=effort, max_tokens=max_tokens,
                          deadline=deadline)
        except (LLMRefusal, LLMUnavailable):
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
