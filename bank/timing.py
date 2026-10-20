# Stage timings and model spend (SPEC §19.4). Every stage: `with timed("decision", deal_id): ...`.
# Every model call: log_llm(...) with the usage block -> tokens and estimated USD cost.
import re
import time
from contextlib import contextmanager

from bank import db

# USD per 1M tokens (input, output). Anthropic list prices as of 2026-10 (claude-api skill);
# Bedrock bills separately and may differ, so the report labels these as estimates. EU inference profiles
# (eu.anthropic.…) cost 10 % more: the hackathon account's guide lists Haiku 4.5 at 1.10 / 5.50.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
    "mock": (0.0, 0.0),
}
CACHE_WRITE, CACHE_READ = 1.25, 0.10  # multipliers of the input price
EU_PREMIUM = 1.10


def price_of(model: str) -> tuple[float, float]:
    name = re.sub(r"^(?:[a-z-]+\.)*?anthropic\.", "", model)  # us./eu./global. inference profiles, anthropic. prefix
    k = EU_PREMIUM if model.startswith("eu.") else 1.0
    for key in sorted(PRICES, key=len, reverse=True):
        if name.startswith(key):
            return tuple(round(p * k, 4) for p in PRICES[key])
    return PRICES["claude-opus-4-8"]  # unknown model: count it at a high price rather than free


def cost_usd(model, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0) -> float:
    pin, pout = price_of(model)
    return (input_tokens * pin + cache_write * pin * CACHE_WRITE + cache_read * pin * CACHE_READ
            + output_tokens * pout) / 1_000_000


@contextmanager
def timed(stage: str, deal_id: str | None = None):
    """Record wall time of a stage; ok=0 if it raised."""
    t0 = time.perf_counter()
    ok = 1
    try:
        yield
    except BaseException:
        ok = 0
        raise
    finally:
        ms = (time.perf_counter() - t0) * 1000
        db.run("INSERT INTO timings (ts, deal_id, stage, ms, ok) VALUES (?,?,?,?,?)",
               (time.time(), deal_id, stage, ms, ok))


def log_llm(stage: str, deal_id: str | None, model: str, usage, ms: float, ok: bool = True,
            error: str | None = None) -> float:
    """usage: response.usage from the Anthropic SDK (or a dict). Returns the estimated cost in USD.
    Failed attempts are logged too (ok=0, error = llm.classify kind, 'cooldown' or 'late')."""
    if isinstance(usage, dict):
        get = lambda k: usage.get(k) or 0  # noqa: E731
    else:
        get = lambda k: getattr(usage, k, None) or 0  # noqa: E731
    tin, tout = get("input_tokens"), get("output_tokens")
    cread, cwrite = get("cache_read_input_tokens"), get("cache_creation_input_tokens")
    cost = cost_usd(model, tin, tout, cread, cwrite)
    db.run("INSERT INTO timings (ts, deal_id, stage, ms, ok, model, input_tokens, output_tokens,"
           " cache_read_tokens, cache_write_tokens, cost_usd, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (time.time(), deal_id, stage, ms, int(ok), model, tin, tout, cread, cwrite, cost, error))
    return cost


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100)."""
    if not values:
        return None
    s = sorted(values)
    rank = -(-p * len(s) // 100)  # ceil(p/100 * n)
    return s[max(0, min(len(s) - 1, int(rank) - 1))]


def stage_stats(stage: str) -> dict:
    ms = [r["ms"] for r in db.rows("SELECT ms FROM timings WHERE stage=? AND ok=1", (stage,))]
    return {"stage": stage, "n": len(ms), "p50": percentile(ms, 50), "p95": percentile(ms, 95)}


def llm_totals() -> dict:
    return db.one("SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) input_tokens,"
                  " COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(cost_usd),0) cost_usd"
                  " FROM timings WHERE model IS NOT NULL")


def llm_stats() -> list[dict]:
    """Per (model, stage): answered calls, failed attempts by kind, latency p50 / p95 / max of answered calls,
    tokens and cost — failed attempts included, so a slow or failing Bedrock shows in the numbers."""
    out = []
    for g in db.rows("SELECT model, stage, COALESCE(SUM(input_tokens),0) input_tokens,"
                     " COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(cost_usd),0) cost_usd"
                     " FROM timings WHERE model IS NOT NULL GROUP BY model, stage ORDER BY model, stage"):
        rows = db.rows("SELECT ms, ok, error FROM timings WHERE model=? AND stage=?", (g["model"], g["stage"]))
        ms = [r["ms"] for r in rows if r["ok"]]
        failed: dict[str, int] = {}
        for r in rows:
            if not r["ok"]:
                failed[r["error"] or "error"] = failed.get(r["error"] or "error", 0) + 1
        out.append({**g, "n": len(ms), "failed": failed, "p50": percentile(ms, 50), "p95": percentile(ms, 95),
                    "max": max(ms) if ms else None})
    return out
