# Every number about the solution, measured offline and re-runnable: code size, tests and coverage, closed lists
# (refusal codes, predicates, journal events), scenario and eval results from the committed reports, stage
# timings from the testnet runs, fresh micro-benchmarks (mandate decision, predicate engine, Merkle / manifest,
# journal), UI render times and deals per minute in DEMO_SIM (simulated chain, in-process seller, MOCK model),
# the budget race, model spend per deal and per stage, gas and on-chain cost per deal against the bank tariff.
# No keys, no .env, no transactions; the only network call is eth_gasPrice on Base mainnet (2 s, with a fallback).
# Every number carries "how" (method) and "source". What needs the live stack is listed under "missing".
# Usage: python -m scripts.numbers [--pytest | --tests-from DIR]   -> reports/numbers.json, reports/numbers.md
#   --pytest          run the whole suite once with coverage (MOCK=1, FORK=0, LIVE=0, like CI)
#   --tests-from DIR  read junit.xml / coverage.json / pytest.log produced by that same command
#   neither           keep the tests section of the previous reports/numbers.json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="collabse-numbers-")
os.environ.update({"COLLABSE_NO_DOTENV": "1", "MOCK": "1", "FAULTS": "", "BANK_POLLER": "0",
                   "DB_PATH": os.path.join(_TMP, "sim.db")})
os.environ.pop("DEMO_SIM", None)

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
OUT_JSON, OUT_MD = REPORTS / "numbers.json", REPORTS / "numbers.md"
ETH_EUR = 3000.0
MAINNET_GAS_FALLBACK_WEI = 6_000_000  # 0.006 gwei
N: list[dict] = []  # flat list of numbers for the table: {group, metric, value, unit, how, source}
MISSING: list[dict] = []


def num(group, metric, value, unit="", how="", source=""):
    N.append({"group": group, "metric": metric, "value": value, "unit": unit, "how": how, "source": source})
    return value


def missing(what, why, how_live):
    MISSING.append({"what": what, "why": why, "how_to_measure_live": how_live})


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    rank = -(-p * len(s) // 100)
    return s[max(0, min(len(s) - 1, int(rank) - 1))]


def stats_ms(xs) -> dict:
    return {"n": len(xs), "p50": round(pct(xs, 50), 3) if xs else None, "p95": round(pct(xs, 95), 3) if xs else None,
            "max": round(max(xs), 3) if xs else None, "mean": round(statistics.fmean(xs), 3) if xs else None}


def bench(fn, n: int) -> list[float]:
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return out


def load(name: str):
    p = REPORTS / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ---------------------------------------------------------------------------------------------------- code size
COMPONENTS = {
    "bank (Python)": ["bank/**/*.py"], "bank UI templates": ["bank/templates/**/*"],
    "agents": ["agents/**/*.py", "agents/prompts/**/*", "prompts/**/*"], "seller": ["seller/**/*.py"],
    "scripts": ["scripts/**/*.py"], "tests": ["tests/**/*.py", "tests/**/*.jsonl"],
    "Apify actor": ["actors/company-check/**/*.py", "actors/company-check/Dockerfile"],
    "contracts (Solidity, own)": ["contracts/src/**/*.sol", "contracts/test/**/*.sol", "contracts/script/**/*.sol"],
    "fixtures": ["fixtures/**/*.json"], "infra/deploy": ["infra/**/*", "deploy/**/*", "Dockerfile"],
}


def code_size() -> dict:
    out = {}
    for comp, globs in COMPONENTS.items():
        files, lines, code = set(), 0, 0
        for g in globs:
            files |= {p for p in ROOT.glob(g) if p.is_file() and "__pycache__" not in p.parts}
        for p in files:
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            ls = text.splitlines()
            lines += len(ls)
            code += sum(1 for x in ls if x.strip() and not x.strip().startswith(("#", "//")))
        out[comp] = {"files": len(files), "lines": lines, "code_lines": code}
        num("code", f"{comp}: lines (non-blank, non-comment)", f"{lines} ({code})", "lines",
            "line count of tracked source files", "+".join(globs))
    total = sum(v["lines"] for v in out.values())
    num("code", "total lines (all components above)", total, "lines", "sum", "")
    try:
        commits = int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=ROOT, capture_output=True,
                                     text=True, timeout=10).stdout.strip())
        num("code", "git commits on this branch", commits, "", "git rev-list --count HEAD", "git")
        out["commits"] = commits
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------------------------- closed lists
def closed_lists() -> dict:
    from bank import codes, verifier
    out = {"refusal_codes": [c.value for c in codes.Code],
           "requires_user_action": [c.value for c, (s, _) in codes.SPEC.items() if s == "requires_user_action"],
           "predicate_ops": list(verifier.OPS), "item_codes": list(verifier.ITEM_CODES),
           "delivery_codes": list(verifier.DELIVERY_CODES), "bank_error_codes": list(verifier.BANK_ERROR_CODES),
           "personal_fields_dropped": list(verifier.PERSONAL_FIELDS)}
    num("lists", "decision codes (closed enum, SPEC §6.3)", len(out["refusal_codes"]), "codes",
        "len(bank.codes.Code)", "bank/codes.py")
    num("lists", "  of them requires_user_action (confirm / VAT)", len(out["requires_user_action"]), "codes",
        "codes.SPEC status", "bank/codes.py")
    num("lists", "predicate operators", len(out["predicate_ops"]), "ops", "len(verifier.OPS)", "bank/verifier.py")
    num("lists", "item autocodes (per item)", len(out["item_codes"]), "codes", "len(verifier.ITEM_CODES)",
        "bank/verifier.py")
    num("lists", "delivery autocodes (per delivery)", len(out["delivery_codes"]), "codes",
        "len(verifier.DELIVERY_CODES)", "bank/verifier.py")
    num("lists", "bank-error codes (refunded from the bank's money)", len(out["bank_error_codes"]), "codes",
        "len(verifier.BANK_ERROR_CODES)", "bank/verifier.py")
    num("lists", "personal-data fields never stored", len(out["personal_fields_dropped"]), "fields",
        "len(verifier.PERSONAL_FIELDS)", "bank/verifier.py")
    # journal event types written anywhere in the code (static) — the run below adds the ones observed
    pat = re.compile(r"""journal\.append\(\s*[^,]+,\s*f?["']([A-Za-z_.:-]+)["']""")
    pat_set = re.compile(r"""\b_set\(\s*[^,]+,\s*["'][a-z_]+["'],\s*["']([A-Za-z_.:-]+)["']""")
    events = set()
    for p in list(ROOT.glob("bank/**/*.py")) + [x for x in ROOT.glob("scripts/*.py") if x.name != "numbers.py"] + list(ROOT.glob("agents/*.py")) \
            + list(ROOT.glob("seller/*.py")):
        t = p.read_text(encoding="utf-8")
        events |= set(pat.findall(t)) | set(pat_set.findall(t))
    out["journal_events_static"] = sorted(events)
    num("lists", "journal event types (literal in code)", len(events), "types",
        "regex over journal.append(...) and deals._set(...) literals", "bank/ scripts/ agents/ seller/")
    return out


# ----------------------------------------------------------------------------------- scenarios, evals, reports
SC_GROUP = {"S": "success", "N": "negative/attack", "F": "fault injection", "A": "API auth"}


def scenario_reports() -> dict:
    out = {}
    for f in sorted(REPORTS.glob("scenarios-*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        res = d["results"]
        by = {}
        for r in res:
            g = SC_GROUP.get(r["id"][0], r["id"][0])
            e = by.setdefault(g, {"scenarios": set(), "runs": 0, "pass": 0, "xfail": 0, "fail": 0})
            e["scenarios"].add(r["id"])
            e["runs"] += 1
            e[r["status"] if r["status"] in ("pass", "xfail", "fail") else "fail"] += 1
        by = {g: {**v, "scenarios": len(v["scenarios"])} for g, v in by.items()}
        txs = [t for r in res for t in r.get("txs") or []]
        out[f.name] = {"label": d["label"], "runs": d["runs"], "by_group": by, "txs": len(txs),
                       "seconds": round(d["finished"] - d["started"], 1),
                       "scenario_seconds": stats_ms([r["seconds"] for r in res if r.get("seconds") is not None]),
                       "reconciliation_ok": d["reconciliation"].get("ok"),
                       "deals_reconciled": f'{d["reconciliation"].get("deals_ok")}/{d["reconciliation"].get("deals_checked")}',
                       "wrong_debits": d["reconciliation"].get("wrong_debit_count"),
                       "journals_verified": f'{sum(1 for v in d.get("journal_verified", {}).values() if v)}/'
                                            f'{len(d.get("journal_verified", {}))}'}
        note = " (N10/N11: precondition not met in the full run, re-run alone in scenarios-sepolia-n10n11: pass)" \
            if f.name == "scenarios-sepolia.json" else ""
        for g, v in by.items():
            num("scenarios", f"{f.stem}: {g}" + (note if v["fail"] else ""), f'{v["scenarios"]} scen., {v["runs"]} runs: {v["pass"]} pass, '
                f'{v["xfail"]} xfail, {v["fail"]} fail', "", "status per result row", f"reports/{f.name}")
        num("scenarios", f"{f.stem}: reconciliation chain↔DB↔statement", out[f.name]["deals_reconciled"]
            + (" ok" if out[f.name]["reconciliation_ok"] else " NOT ok") + f', wrong debits {out[f.name]["wrong_debits"]}',
            "deals", "reconciliation block", f"reports/{f.name}")
        num("scenarios", f"{f.stem}: scenario wall time p50/p95", f'{out[f.name]["scenario_seconds"]["p50"]} / '
            f'{out[f.name]["scenario_seconds"]["p95"]}', "s", "results[].seconds", f"reports/{f.name}")
    # the Sepolia full matrix: N10/N11 failed there for a precondition and were re-run alone (n10n11 report)
    return out


def stage_timings() -> dict:
    """p50/p95 per stage from the raw samples in the scenario reports (real Base Sepolia and the fork)."""
    out = {}
    wanted = ["decision", "screening", "facilitator_verify", "facilitator_settle", "refusal_to_client", "api_pay",
              "quote", "negotiate_proxy", "notify_seller", "escrow_authorize", "escrow_capture", "escrow_void"]
    for name, label in (("scenarios-sepolia.json", "Base Sepolia"), ("scenarios-fork.json", "fork")):
        d = load(name)
        if not d:
            continue
        st = d["stages"]
        rows = {}
        for k in wanted:
            if st.get(k):
                rows[k] = stats_ms([float(x) for x in st[k]])
        tib = [float(x) for k, v in st.items() if k.startswith("tx_in_block:") for x in v]
        if tib:
            rows["tx_in_block (all kinds)"] = stats_ms(tib)
        for k, v in st.items():
            if k.startswith("tx_in_block:") or k.startswith("broadcast:"):
                rows[k] = stats_ms([float(x) for x in v])
        out[label] = rows
        for k, s in rows.items():
            if k.startswith("broadcast:"):
                continue
            num(f"timings ({label})", k, f'{s["p50"]} / {s["p95"]}', "ms p50/p95", f"n={s['n']} samples, nearest rank",
                f"reports/{name} stages")
    return out


def evals() -> dict:
    d = load("evals-live.json") or {}
    inj, j, ex = d.get("injections", {}), d.get("judge", {}), d.get("explanations", {})
    src = "reports/evals-live.json"
    num("evals", "injection variants (+ controls)", f'{inj.get("variants")} (+{inj.get("controls")})', "",
        "tests/evals/injections.jsonl rows", src)
    num("evals", "money blocked: variants / model runs", f'{inj.get("money_blocked")}/{inj.get("variants")} · '
        f'{inj.get("money_runs_blocked")}/{inj.get("money_runs")}', "", "lock went only to the registered payee", src)
    num("evals", "live model runs: money blocked / unsafe tool attempts", f'{inj.get("live_money_blocked")}/'
        f'{inj.get("live_runs")} · {inj.get("live_agent_tried_unsafe")}', "", "Opus 4.5 buyer, live Bedrock", src)
    num("evals", "outcome as expected (all / live)", f'{inj.get("outcome_as_expected")}/{len(inj.get("rows", []))} · '
        f'{inj.get("live_outcome_as_expected")}/{inj.get("live_runs")}', "", "per-variant expectation", src)
    num("evals", "item-level injections paid", f'{inj.get("items_paid_by_injection")} of {inj.get("items_variants")} variants',
        "", "injection inside delivered items", src)
    num("evals", "judge: items, precision, recall", f'{j.get("items")}, {j.get("precision")}, {j.get("recall")}', "",
        f'tp {j.get("tp")} fp {j.get("fp")} fn {j.get("fn")} tn {j.get("tn")}; {j.get("model")}', src)
    num("evals", "judge valid JSON rate", j.get("valid_json_rate"), "", "pydantic validation of the answer", src)
    num("evals", "explanations: texts / client texts clean / AI texts labelled",
        f'{ex.get("texts")} / {ex.get("client_clean")}/{ex.get("client_texts")} / {ex.get("ai_labelled")}/{ex.get("ai_texts")}',
        "", "no hidden AML reason, sources subset, AI Act Art. 50 label", src)
    for f in ("injections.jsonl", "predicates.jsonl"):
        p = ROOT / "tests" / "evals" / f
        if p.exists():
            num("evals", f"eval cases in tests/evals/{f}", sum(1 for x in p.read_text(encoding="utf-8").splitlines()
                                                              if x.strip()), "cases", "non-empty lines", str(p.relative_to(ROOT)))
    act = ROOT / "tests" / "evals" / "test_company_check.py"
    if act.exists():
        t = act.read_text(encoding="utf-8")
        n_tests = len(re.findall(r"^def test_", t, re.M))
        n_param = sum(len(re.findall(r'"[^"]*"', m)) for m in re.findall(r"parametrize\([^\[]*\[([^\]]*)\]", t))
        num("evals", "Apify actor company-check: eval tests (+ parametrized cases)", f"{n_tests} (+{n_param})", "",
            "def test_ in tests/evals/test_company_check.py", "tests/evals/test_company_check.py")
    v = load("validation.json") or {}
    cov = (v.get("coverage") or {}).get("total") or {}
    if cov:
        num("tests (committed report)", "coverage of bank/ in the committed validation report (lines / branches)",
            f'{round(cov.get("lines") or 0, 1)} / {round(cov.get("branches") or 0, 1)}', "%", "pytest --cov at commit " + str(v.get("commit")),
            "reports/validation.json")
    return {"injections": {k: v for k, v in inj.items() if k != "rows"}, "judge": {k: v for k, v in j.items()
                                                                                    if k != "injection_items"},
            "explanations": {k: v for k, v in ex.items() if k != "rows"}}


def model_spend(sim_calls: dict) -> dict:
    """Live Bedrock spend (evals-live) per call and stage; calls per deal per stage counted in the DEMO_SIM run."""
    d = load("evals-live.json") or {}
    tot = d.get("llm_totals") or {}
    per_call = {}
    for s in tot.get("by_stage", []):
        per_call[s["stage"]] = {"model": s["model"], "calls": s["calls"], "usd_per_call": s["cost_usd"] / s["calls"],
                                "in_per_call": s["input_tokens"] / s["calls"], "out_per_call": s["output_tokens"] / s["calls"],
                                "mean_ms": s["ms"] / s["calls"]}
        num("model", f"{s['stage']} ({s['model'].split('.')[-1].split('-v')[0]}): calls, tokens in/out, $",
            f"{s['calls']}, {s['input_tokens']}/{s['output_tokens']}, ${s['cost_usd']:.4f}", "",
            "live eval run, tokens logged per call x list price (+10 % EU profile)", "reports/evals-live.json")
        num("model", f"{s['stage']}: mean latency per call", round(s["ms"] / s["calls"]), "ms",
            "stage wall ms / calls (no per-call samples kept -> no p50/p95)", "reports/evals-live.json")
    num("model", "eval run total: calls, tokens in/out, $", f"{tot.get('calls')}, {tot.get('input_tokens')}/"
        f"{tot.get('output_tokens')}, ${tot.get('cost_usd', 0):.4f}", "", "sum", "reports/evals-live.json")
    live_deals = (d.get("injections") or {}).get("live_runs") or 1
    est = {}
    for stage, n_calls in sim_calls.get("calls_per_deal_by_stage", {}).items():
        pc = per_call.get(stage)
        if pc:
            est[stage] = n_calls * pc["usd_per_call"]
    # the buyer's live call count per deal differs from MOCK's scripted turns: use the live average per deal
    if "buyer" in per_call:
        est["buyer"] = per_call["buyer"]["calls"] * per_call["buyer"]["usd_per_call"] / live_deals
        num("model", "buyer agent: live calls per deal", round(per_call["buyer"]["calls"] / live_deals, 2), "calls",
            f"67 buyer calls / {live_deals} live deals", "reports/evals-live.json")
    jd = d.get("judge") or {}
    if "judge" in per_call and jd.get("items"):  # the judge flags items of a delivery: cost per item x 20 items
        est["judge (20-item delivery)"] = per_call["judge"]["calls"] * per_call["judge"]["usd_per_call"] / jd["items"] * 20
    out = {"per_call": per_call, "usd_per_deal_by_stage": est, "usd_per_deal": sum(est.values())}
    num("model", "$ per deal (estimate: live $/call x calls per deal)", round(out["usd_per_deal"], 4), "USD",
        "buyer: live avg per deal; other stages: MOCK call count per deal in DEMO_SIM x live $/call", "evals-live + sim")
    for k, v in est.items():
        num("model", f"  $ per deal: {k}", round(v, 5), "USD", "as above", "")
    return out


# ------------------------------------------------------------------------------------------ DEMO_SIM run
CAPTURED: dict = {}


def spy(module, name, key):
    orig = getattr(module, name)

    def wrapper(*a, **kw):
        CAPTURED.setdefault(key, (a, kw))
        if key == "evaluate" and len(a[0]) >= 20:
            CAPTURED["evaluate20"] = (a, kw)
        return orig(*a, **kw)
    setattr(module, name, wrapper)
    return orig


def sim_run() -> dict:
    from bank import config, db, mandate, receipts, verifier
    from scripts import demo_scenarios, demo_sim, seed
    seed.seed()
    originals = {"decide": spy(mandate, "decide", "decide"), "evaluate": spy(verifier, "evaluate", "evaluate"),
                 "merkle_root": spy(receipts, "merkle_root", "merkle_root"),
                 "sign_manifest": spy(receipts, "sign_manifest", "sign_manifest")}
    demo_sim.install()
    runs, t_all = {}, time.perf_counter()
    from bank import deadlines, deals as deals_mod
    for name in demo_scenarios.SCENARIOS:
        t0 = time.perf_counter()
        out = demo_scenarios.run_safe(name)
        runs[name] = {"seconds": round(time.perf_counter() - t0, 3), "outcome": (out or {}).get("outcome")
                      or demo_scenarios.RUN.get("outcome"), "deals": list(((out or {}).get("deals") or {}).values())}
        num("DEMO_SIM", f"scenario '{name}': wall time, outcome", f"{runs[name]['seconds']} s, {runs[name]['outcome']}",
            "", "demo_scenarios.run_safe in-process (simulated chain, MOCK model)", "scripts/numbers.py")
        # the human's part of the demo on /mandate: acknowledge the §109 VAT risk once, confirm above the threshold
        for code, fn, what in (("VAT_LIABILITY_RISK", deals_mod.ack_vat, "VAT acknowledgement"),
                               ("CONFIRMATION_REQUIRED", deals_mod.confirm, "confirmation")):
            if runs[name]["outcome"] == code and runs[name]["deals"]:
                t1 = time.perf_counter()
                r = fn(runs[name]["deals"][-1], "human:demo")
                runs[name]["after_human"] = {"what": what, "seconds": round(time.perf_counter() - t1, 3),
                                             "outcome": r.get("code") or r.get("status")}
                runs[name]["outcome"] = runs[name]["after_human"]["outcome"]
                num("DEMO_SIM", f"scenario '{name}': human {what} on /mandate -> bank pays",
                    f"{runs[name]['after_human']['seconds']} s, {runs[name]['after_human']['outcome']}", "",
                    f"deals.{fn.__name__} (what the /mandate button calls)", "scripts/numbers.py")
    total_s = time.perf_counter() - t_all
    # rule B: the last 10 % is captured at window end (20 s at demo scale): one timer pass a day later closes all
    t2 = time.perf_counter()
    closed = deadlines.tick(now=time.time() + 86400)
    num("DEMO_SIM", "timer pass after the windows (rule B rest, voids)", f"{round((time.perf_counter() - t2) * 1000, 1)} ms, "
        f"{len(closed)} actions", "", "deadlines.tick(now + 1 day)", "bank/deadlines.py")
    deals = db.rows("SELECT deal_id, state, asset FROM deals")
    num("DEMO_SIM", "five scenarios end to end (incl. the two human clicks as calls)", round(total_s, 2), "s",
        "wall time of the loop above", "scripts/numbers.py")
    num("DEMO_SIM", "deals per minute (end to end, sequential, one bank process)", round(len(deals) / total_s * 60, 1),
        "deals/min", f"{len(deals)} deals / {total_s:.1f} s; depends on machine load (see header)", "scripts/numbers.py")
    # stage timings recorded by the bank's own timed() during the sim run
    st = {}
    for r in db.rows("SELECT stage, ms FROM timings WHERE model IS NULL AND ok=1"):
        st.setdefault(r["stage"], []).append(r["ms"])
    stages = {k: stats_ms(v) for k, v in st.items()}
    for k in ("decision", "api_pay", "quote", "negotiate_proxy", "escrow_authorize", "escrow_capture",
              "refusal_to_client", "screening"):
        if k in stages:
            num("DEMO_SIM", f"stage {k} p50/p95", f"{stages[k]['p50']} / {stages[k]['p95']}", "ms",
                f"bank timed(), n={stages[k]['n']}", "timings table of the sim run")
    # model calls per deal and stage (MOCK: structure only, $0)
    calls = db.rows("SELECT stage, deal_id, COUNT(*) n FROM timings WHERE model IS NOT NULL GROUP BY stage, deal_id")
    by_stage = {}
    for r in calls:
        by_stage.setdefault(r["stage"], []).append(r["n"])
    n_deals = max(1, len(deals))
    calls_per_deal = {k: round(sum(v) / n_deals, 2) for k, v in by_stage.items()}
    for k, v in calls_per_deal.items():
        num("DEMO_SIM", f"model calls per deal: {k}", v, "calls", f"MOCK calls in the timings table / {n_deals} deals",
            "timings table of the sim run")
    events = [r["event"] for r in db.rows("SELECT event FROM journal")]
    num("DEMO_SIM", "journal entries written by the five scenarios / distinct event types",
        f"{len(events)} / {len(set(events))}", "", "SELECT FROM journal", "sim db")
    ver = __import__("bank.journal", fromlist=["verify"]).verify()
    num("DEMO_SIM", "journal hash chain after the run", "ok" if ver[0] else f"BROKEN at {ver[1]}", "",
        "journal.verify()", "bank/journal.py")
    txs = db.rows("SELECT kind, COUNT(*) n FROM onchain_txs GROUP BY kind")
    num("DEMO_SIM", "simulated escrow txs by kind (five scenarios)", ", ".join(f"{r['kind']} {r['n']}" for r in txs),
        "", "onchain_txs rows (simulated)", "sim db")
    pages = render_pages(deals)
    return {"runs": runs, "seconds": round(total_s, 2), "deals": len(deals), "stages": stages,
            "calls_per_deal_by_stage": calls_per_deal, "journal_events_observed": sorted(set(events)),
            "journal_entries": len(events), "txs": {r["kind"]: r["n"] for r in txs}, "pages": pages,
            "originals": originals, "config_divisors": [config.MONEY_DIVISOR, config.TIME_DIVISOR]}


def render_pages(deals: list[dict], n: int = 50) -> dict:
    from fastapi.testclient import TestClient

    from bank.app import app
    client = TestClient(app)
    deal_id = next((d["deal_id"] for d in deals if d["state"] in ("settled", "closed", "delivered")),
                   deals[0]["deal_id"] if deals else "d-none")
    out = {}
    for path in ("/story", "/bank", "/mandate", "/deals", f"/deals/{deal_id}", "/statement", "/statement/reconcile"):
        codes_seen, sizes = set(), []

        def get():
            r = client.get(path)
            codes_seen.add(r.status_code)
            sizes.append(len(r.content))
        ms = bench(get, n)
        key = "/deals/{id}" if path.startswith("/deals/d-") else path
        out[key] = {**stats_ms(ms), "status": sorted(codes_seen), "bytes": sizes[-1] if sizes else None}
        num("UI render", f"GET {key}", f"{out[key]['p50']} / {out[key]['p95']}", "ms p50/p95",
            f"TestClient, {n} requests after the five scenarios, HTTP {sorted(codes_seen)}, {out[key]['bytes']} bytes",
            "scripts/numbers.py")
    return out


# --------------------------------------------------------------------------------------- micro-benchmarks
def micro(sim: dict) -> dict:
    from eth_account import Account

    from bank import receipts, verifier
    out = {}
    orig = sim["originals"]
    if "decide" in CAPTURED:
        a, kw = CAPTURED["decide"]
        res = orig["decide"](*a, **kw)
        ms = bench(lambda: orig["decide"](*a, **kw), 1000)
        out["mandate_decide"] = stats_ms(ms)
        num("bench", "mandate decision (decide: mandate + cached check + explanation + timing row)",
            f"{out['mandate_decide']['p50']} / {out['mandate_decide']['p95']}", "ms p50/p95",
            f"1000 calls with the arguments of the first DEMO_SIM decision (result: {res.get('code') or res.get('status')}), "
            "SQLite on local disk", "bank/mandate.py")
    ev = CAPTURED.get("evaluate20") or CAPTURED.get("evaluate")
    if ev:
        a, kw = ev
        items = a[0]
        ms = bench(lambda: orig["evaluate"](*a, **kw), 200)
        out["predicates_delivery"] = {**stats_ms(ms), "items": len(items), "predicates": len(a[1])}
        num("bench", f"predicate engine per {len(items)}-item delivery ({len(a[1])} predicates)",
            f"{out['predicates_delivery']['p50']} / {out['predicates_delivery']['p95']}", "ms p50/p95",
            "200 calls of verifier.evaluate with a real delivery's arguments (registry from fixtures)", "bank/verifier.py")
        one = ((items[:1],) + tuple(a[1:]), {**kw, "ordered": 1})
        ms1 = bench(lambda: orig["evaluate"](*one[0], **one[1]), 1000)
        out["predicates_item"] = stats_ms(ms1)
        num("bench", "predicate engine per item", f"{out['predicates_item']['p50']} / {out['predicates_item']['p95']}",
            "ms p50/p95", "1000 calls on one item", "bank/verifier.py")
        ms = bench(lambda: orig["merkle_root"](items), 500)
        out["merkle_root"] = {**stats_ms(ms), "items": len(items)}
        num("bench", f"Merkle root of {len(items)} items (JCS + SHA-256)", f"{out['merkle_root']['p50']} / "
            f"{out['merkle_root']['p95']}", "ms p50/p95", "500 calls of receipts.merkle_root", "bank/receipts.py")
        root = orig["merkle_root"](items)
        ms = bench(lambda: receipts.verify_proof(items[7], 7, receipts.merkle_proof(items, 7), root), 500)
        out["merkle_proof"] = stats_ms(ms)
        num("bench", "Merkle proof build + verify (one item)", f"{out['merkle_proof']['p50']} / {out['merkle_proof']['p95']}",
            "ms p50/p95", "500 calls merkle_proof + verify_proof", "bank/receipts.py")
    if "sign_manifest" in CAPTURED:
        (m, _key), _ = CAPTURED["sign_manifest"]
        key = Account.create().key.hex()
        ms = bench(lambda: orig["sign_manifest"](m, key), 300)
        out["manifest_sign"] = stats_ms(ms)
        sig = orig["sign_manifest"](m, key)
        ms = bench(lambda: receipts.manifest_signer(m, sig), 300)
        out["manifest_verify"] = stats_ms(ms)
        num("bench", "manifest EIP-712 signing", f"{out['manifest_sign']['p50']} / {out['manifest_sign']['p95']}",
            "ms p50/p95", "300 calls, throwaway key", "bank/receipts.py")
        num("bench", "manifest signature recovery (verify)", f"{out['manifest_verify']['p50']} / "
            f"{out['manifest_verify']['p95']}", "ms p50/p95", "300 calls of manifest_signer", "bank/receipts.py")
    return out


def journal_bench() -> dict:
    from bank import config, db, journal
    out = {}
    for n in (1000, 10000):
        os.environ["DB_PATH"] = os.path.join(_TMP, f"journal-{n}.db")
        db.close()
        config.reload()
        payload = {"amount": 500000, "asset": "EURC", "tx_hash": "0x" + "ab" * 32}
        t0 = time.perf_counter()
        ms = []
        for i in range(n):
            t = time.perf_counter()
            journal.append("bank", "bench.event", {**payload, "i": i}, deal_id=f"d-{i % 50:04d}")
            ms.append((time.perf_counter() - t) * 1000)
        total = time.perf_counter() - t0
        t1 = time.perf_counter()
        ok, bad = journal.verify()
        verify_s = time.perf_counter() - t1
        out[n] = {"append": stats_ms(ms), "append_total_s": round(total, 3), "verify_s": round(verify_s, 4),
                  "verify_ok": ok, "verify_us_per_entry": round(verify_s / n * 1e6, 2)}
        num("bench", f"journal append ({n} entries)", f"{out[n]['append']['p50']} / {out[n]['append']['p95']}",
            "ms p50/p95", "journal.append (hash chain + SQLite insert) on a fresh DB", "bank/journal.py")
        num("bench", f"journal full-chain verify ({n} entries)", f"{out[n]['verify_s']} s ({out[n]['verify_us_per_entry']} µs/entry), ok={ok}",
            "", "journal.verify() recomputes every hash", "bank/journal.py")
    return out


def budget_race(rounds: int = 20) -> dict:
    from agents import offline
    from bank import config, db, mandate
    overspend, ok_counts, wall = 0, [], []
    for i in range(rounds):
        os.environ["DB_PATH"] = os.path.join(_TMP, f"race-{i}.db")
        db.close()
        config.reload()
        bank = offline.OfflineBank.setup()
        mandate.amend(mandate.DEMO_MANDATE_ID, {"budget": {"value": "1500", "currency": "EUR", "period": "month"}},
                      "human:bench")
        deal_ids = []
        for _ in range(10):
            d = bank.create_deal({"buyer_id": "alfa", "seller_id": "s-gama", "mandate_id": mandate.DEMO_MANDATE_ID,
                                  "task": {"budget": {"value": "500", "currency": "EUR"}}})["deal_id"]
            bank.negotiate(d, "Can you do 5 €?")
            deal_ids.append(d)
        start, results = threading.Barrier(10), {}

        def pay(deal_id):
            start.wait()
            try:
                results[deal_id] = bank.pay(deal_id)
            finally:
                db.close()
        t0 = time.perf_counter()
        th = [threading.Thread(target=pay, args=(d,)) for d in deal_ids]
        for t in th:
            t.start()
        for t in th:
            t.join()
        wall.append((time.perf_counter() - t0) * 1000)
        s = db.one("SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM reservations")
        ok_counts.append(sum(1 for r in results.values() if not r.get("code")))
        if s["s"] > 1500:
            overspend += 1
    out = {"rounds": rounds, "parallel_pays": 10, "budget_for": 3, "overspend_rounds": overspend,
           "paid_per_round": sorted(set(ok_counts)), "wall_ms": stats_ms(wall)}
    num("bench", "budget race: 10 parallel pay(), budget for 3", f"{rounds} rounds, overspend {overspend}, "
        f"paid per round {out['paid_per_round']}", "", "threads behind a barrier on the offline bank (mandate.reserve, "
        "BEGIN IMMEDIATE); sum(reservations) <= budget", "agents/offline.py + bank/mandate.py")
    num("bench", "budget race: wall time for 10 parallel pay()", f"{out['wall_ms']['p50']} / {out['wall_ms']['p95']}",
        "ms p50/p95", f"{rounds} rounds", "scripts/numbers.py")
    return out


# --------------------------------------------------------------------------------------------- gas and tariff
GAS_FALLBACK = {"authorize": 161_439, "capture": 68_475, "void": 61_208, "x402_settle": 82_518, "mint": 71_222}


def gas_and_tariff() -> dict:
    import httpx

    from bank import config
    cr = load("credits.json") or {}
    by_kind = (cr.get("gas") or {}).get("by_kind") or {}
    gas = {k: (by_kind.get(k) or {}).get("gas_median") or v for k, v in GAS_FALLBACK.items()}
    src_gas = "reports/credits.json (median gasUsed of Base Sepolia receipts)" if by_kind else "fallback constants"
    for k, g in gas.items():
        n = (by_kind.get(k) or {}).get("n")
        num("on-chain", f"gas: {k}", g, "gas", f"median of {n} receipts" if n else "fallback", src_gas)
    try:
        r = httpx.post("https://mainnet.base.org", json={"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice",
                                                          "params": []}, timeout=2)
        gp, gp_src = int(r.json()["result"], 16), "eth_gasPrice https://mainnet.base.org (now)"
    except Exception:  # noqa: BLE001
        gp, gp_src = MAINNET_GAS_FALLBACK_WEI, "fallback 0.006 gwei (mainnet.base.org unreachable in 2 s)"
    num("on-chain", "Base mainnet gas price", round(gp / 1e9, 6), "gwei", gp_src, "")
    eur = lambda g: g * gp / 1e18 * ETH_EUR  # noqa: E731
    flows = {"rule A (authorize + capture)": ["authorize", "capture"],
             "rule B (authorize + 2 captures)": ["authorize", "capture", "capture"],
             "rule B with rejects (authorize + 2 captures + void)": ["authorize", "capture", "capture", "void"],
             "x402 direct (settle)": ["x402_settle"]}
    per_deal = {}
    for name, ks in flows.items():
        g = sum(gas[k] for k in ks)
        per_deal[name] = {"gas": g, "eur": eur(g)}
        num("on-chain", f"L2 execution cost per deal: {name}", f"{g:,} gas = {eur(g) * 100:.4f} ct", "EUR cents",
            f"gas x mainnet gas price x ETH {ETH_EUR:.0f} € (L1 data fee not included)", src_gas)
    tariff = {}
    # production scale: tiers T0 <= 20 €, T1 (20; 500], T2 > 500 (bank/config.py with divisor 1)
    for amount_eur, tier, rule, flow in ((5, "T0", "A", "rule A (authorize + capture)"),
                                         (100, "T1", "B", "rule B (authorize + 2 captures)"),
                                         (100, "T1", "A", "rule A (authorize + capture)"),
                                         (1000, "T2", "C", "rule B with rejects (authorize + 2 captures + void)")):
        bps = config.TARIFF_BPS.get((tier, rule), 0)
        fee = amount_eur * bps / 10_000
        cost = per_deal[flow]["eur"]
        tariff[f"{amount_eur} € {tier}/{rule}"] = {"bps": bps, "fee_eur": fee, "gas_eur": cost, "margin_eur": fee - cost}
        num("on-chain", f"tariff vs gas: {amount_eur} € deal, {tier} rule {rule} ({bps} bps)",
            f"fee {fee:.2f} € − gas {cost * 100:.4f} ct = margin {fee - cost:.4f} €"
            + (" (T0 billed monthly, nothing at capture)" if tier == "T0" else ""), "EUR",
            "config.TARIFF_BPS x amount − L2 gas cost of the flow", "bank/config.py")
    # demo scale (DEMO_MONEY_DIVISOR=100): the 5,00 € demo deal falls in T1, rule B
    from bank import deals as deals_mod
    t_demo = deals_mod.tier_of(500)
    bps = config.TARIFF_BPS.get((t_demo, "B"), 0)
    fee = 5 * bps / 10_000
    cost = per_deal["rule B (authorize + 2 captures)"]["eur"]
    tariff["5 € demo deal (demo scale)"] = {"tier": t_demo, "bps": bps, "fee_eur": fee, "gas_eur": cost,
                                           "margin_eur": fee - cost}
    num("on-chain", f"tariff vs gas: the 5 € demo deal at demo scale ({t_demo}, rule B, {bps} bps)",
        f"fee {fee * 100:.2f} ct − gas {cost * 100:.4f} ct = margin {(fee - cost) * 100:.2f} ct", "EUR",
        f"deals.tier_of(500 cents) with DEMO_MONEY_DIVISOR={config.MONEY_DIVISOR}", "bank/config.py")
    return {"gas": gas, "gas_price_wei": gp, "gas_price_source": gp_src, "eth_eur": ETH_EUR, "per_deal": per_deal,
            "tariff": tariff, "tariff_bps": {f"{t}/{r}": v for (t, r), v in config.TARIFF_BPS.items()}}


def apify_numbers() -> dict:
    cr = load("credits.json") or {}
    ap = cr.get("apify") or {}
    if ap.get("ok"):
        lr = ap.get("last_runs") or {}
        num("Apify", "company-check runs: n, $ median / max, runtime median",
            f"{lr.get('n')}, ${lr.get('usd_median')} / ${lr.get('usd_max')}, {lr.get('runtime_s_median')} s", "",
            "GET /v2/actor-runs (last runs of the account)", "reports/credits.json")
        num("Apify", "account usage this cycle / limit", f"${ap.get('monthly_usage_usd'):.4f} / ${ap.get('max_monthly_usage_usd')}",
            "USD", "GET /v2/users/me/limits", "reports/credits.json")
    checks = ROOT / "actors" / "company-check" / "my_actor" / "checks.py"
    if checks.exists():
        t = checks.read_text(encoding="utf-8")
        num("Apify", "actor check functions", len(re.findall(r"^def ", t, re.M)), "functions", "def in checks.py",
            "actors/company-check/my_actor/checks.py")
    return ap


# ------------------------------------------------------------------------------------------------- tests
def tests_section(args) -> dict:
    d = None
    if args.pytest:
        d = Path(_TMP) / "pytest"
        d.mkdir()
        env = {**os.environ, "MOCK": "1", "FAULTS": "", "LIVE": "0", "FORK": "0", "PYTHONUTF8": "1",
               "COVERAGE_FILE": str(d / ".coverage")}
        env.pop("DB_PATH", None)
        t0 = time.time()
        p = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--cov=bank", "--cov-branch",
                            f"--cov-report=json:{d / 'coverage.json'}", f"--junitxml={d / 'junit.xml'}",
                            "--durations=15"], cwd=ROOT, env=env, capture_output=True, text=True)
        (d / "pytest.log").write_text(p.stdout + p.stderr + f"\nreal {time.time() - t0:.1f}\n", encoding="utf-8")
    elif args.tests_from:
        d = Path(args.tests_from)
    try:
        env = {**os.environ, "MOCK": "1", "FAULTS": "", "LIVE": "0", "FORK": "0"}
        env.pop("DB_PATH", None)
        c = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        per_file = {a: int(b) for a, b in re.findall(r"^(tests/\S+\.py): (\d+)$", c.stdout, re.M)}
        if per_file:
            num("tests", "tests collected (unit + evals, incl. live/fork ones that skip offline)",
                f"{sum(per_file.values())} in {len(per_file)} files", "tests", "pytest --collect-only -q", "tests/")
    except Exception:  # noqa: BLE001
        pass
    if d is None:
        prev = load("numbers.json") or {}
        t = prev.get("tests") or {}
        for row in prev.get("numbers", []):
            if row["group"] == "tests" and "validation report" not in row["metric"] and "collect-only" not in row["how"]:
                N.append(row)
        return t
    out = {}
    jx = d / "junit.xml"
    if jx.exists():
        root = ET.parse(jx).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        tot = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
        secs = sum(float(s.get("time", 0)) for s in suites)
        cases = [(c.get("classname", ""), c.get("name"), float(c.get("time", 0))) for c in root.iter("testcase")]
        by_file = {}
        for cls, _, t in cases:
            f = cls.split(".")[1] if cls.startswith("tests.") else cls
            e = by_file.setdefault(f, [0, 0.0])
            e[0] += 1
            e[1] += t
        out.update(tot, seconds=round(secs, 1), slowest=sorted(cases, key=lambda c: -c[2])[:10],
                   by_dir={k: {"tests": v[0], "seconds": round(v[1], 1)} for k, v in sorted(by_file.items())})
        passed = tot["tests"] - tot["failures"] - tot["errors"] - tot["skipped"]
        num("tests", "tests collected / passed / failed / skipped",
            f"{tot['tests']} / {passed} / {tot['failures'] + tot['errors']} / {tot['skipped']}", "",
            "pytest (MOCK=1, FORK=0, LIVE=0 as in CI), junit.xml", "pytest run of this script")
        num("tests", "test suite runtime (with coverage)", round(secs, 1), "s", "junit.xml total time", "")
        for k, v in out["by_dir"].items():
            num("tests", f"  {k}: tests, seconds", f"{v['tests']}, {v['seconds']}", "", "junit.xml per class prefix", "")
    log = d / "pytest.log"
    if log.exists():
        t = log.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"([\d.]+)s user ([\d.]+)s system", t)
        if m:
            cpu = float(m.group(1)) + float(m.group(2))
            out["cpu_seconds"] = round(cpu, 1)
            num("tests", "test suite CPU time (user + system)", round(cpu, 1), "s",
                "shell `time`; wall time above is mostly waiting (timeouts, timers, a loaded shared machine)", "pytest.log")
        slow = re.findall(r"^([\d.]+)s (setup|call|teardown)\s+(\S+)$", t, re.M)
        if slow:
            out["slowest_durations"] = [{"s": float(a), "phase": b, "test": c} for a, b, c in slow[:15]]
            num("tests", "slowest test", f"{slow[0][0]} s ({slow[0][1]}) {slow[0][2]}", "", "pytest --durations=15", "")
    cj = d / "coverage.json"
    if cj.exists():
        c = json.loads(cj.read_text(encoding="utf-8"))
        t = c["totals"]
        out["coverage"] = {"lines_pct": round(100 * t["covered_lines"] / max(1, t["num_statements"]), 1),
                           "branches_pct": round(100 * t["covered_branches"] / max(1, t["num_branches"]), 1),
                           "statements": t["num_statements"], "branches": t["num_branches"],
                           "total_pct": round(t["percent_covered"], 1)}
        num("tests", "coverage of bank/: lines / branches", f"{out['coverage']['lines_pct']} / "
            f"{out['coverage']['branches_pct']} ({t['num_statements']} statements, {t['num_branches']} branches)", "%",
            "pytest --cov=bank --cov-branch, coverage.json totals", "")
        files = {}
        for f, v in c["files"].items():
            s = v["summary"]
            files[f] = {"lines": round(100 * s["covered_lines"] / max(1, s["num_statements"]), 1),
                        "branches": round(100 * s["covered_branches"] / max(1, s["num_branches"]), 1) if s["num_branches"] else None}
        out["coverage"]["files"] = files
        for f in ("bank/mandate.py", "bank/verifier.py", "bank/explain.py", "bank/escrow.py", "bank/deals.py",
                  "bank/facilitator.py", "bank/journal.py"):
            if f in files:
                num("tests", f"  coverage {f}: lines / branches", f"{files[f]['lines']} / {files[f]['branches']}", "%",
                    "coverage.json per file", "")
    return out


# --------------------------------------------------------------------------------------------- the report
def write(result: dict):
    result["numbers"], result["missing"] = N, MISSING
    OUT_JSON.write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    lines = ["# Numbers — every measurable part of Collabse", "",
             f"Generated {result['generated']} by `scripts/numbers.py` at commit `{result.get('commit')}`. "
             "Offline: DEMO_SIM (simulated chain, in-process seller), MOCK model, no keys, no transactions. "
             "Testnet and live-model figures are read from the committed reports named in *Source*. "
             "A prototype validated on testnet, not production-ready.", "",
             f"Machine: {result.get('machine')}. Micro-benchmarks ran on a shared laptop: p50 is the stable figure, "
             "p95 includes scheduler noise.", ""]
    group = None
    for r in N:
        if r["group"] != group:
            group = r["group"]
            lines += ["", f"## {group}", "", "| Metric | Value | How | Source |", "|---|---|---|---|"]
        val = f"{r['value']} {r['unit']}".strip()
        lines.append(f"| {r['metric'].replace('|', '/')} | {val.replace('|', '/')} | {r['how'].replace('|', '/')} | "
                     f"{r['source'].replace('|', '/')} |")
    lines += ["", "## Not measurable offline — needs the live run", "", "| What | Why | How to measure live |",
              "|---|---|---|"]
    lines += [f"| {m['what']} | {m['why']} | {m['how_to_measure_live']} |" for m in MISSING]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pytest", action="store_true")
    ap.add_argument("--tests-from")
    args = ap.parse_args()
    t0 = time.time()
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                                timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        commit = None
    import platform
    result = {"generated": time.strftime("%Y-%m-%d %H:%M %Z"), "commit": commit,
              "machine": f"{platform.machine()} {platform.system()} {platform.release()}, Python {platform.python_version()}, "
                         f"{os.cpu_count()} CPUs, load avg {os.getloadavg()[0]:.1f}"}
    result["code"] = code_size()
    result["tests"] = tests_section(args)
    result["lists"] = closed_lists()
    result["scenarios"] = scenario_reports()
    result["timings_reports"] = stage_timings()
    result["evals"] = evals()
    sim = sim_run()
    result["micro"] = micro(sim)
    sim.pop("originals", None)
    result["sim"] = sim
    result["lists"]["journal_events_observed_in_sim"] = sim["journal_events_observed"]
    result["model"] = model_spend(sim)
    result["journal"] = journal_bench()
    result["race"] = budget_race()
    result["onchain"] = gas_and_tariff()
    result["apify"] = apify_numbers()
    missing("block → /bank feed latency (SPEC target ≤ 5 s)", "needs a real block and the Tatum webhook or poller; "
            "bank_feed n=0 in both committed reports", "run the five scenarios on Base Sepolia with the bank's poller on; "
            "stage bank_feed in the timings table")
    missing("tx in block on Base Sepolia for the demo flow (not the matrix)", "simulated chain offline",
            "python -m scripts.demo_scenarios honest (no --sim) with funded keys; tx_in_block:* stages")
    missing("Bedrock latency p50/p95 per model", "evals-live.json keeps per-stage totals only; AWS creds expired",
            "LIVE=1 pytest tests/evals/test_live_bedrock.py, then timings rows with model IS NOT NULL")
    missing("seller-agent negotiation spend (AI seller haggles ≤ 3 turns)", "not part of the live eval run",
            "run the demo with SELLER_OPENING=list and MOCK=0; timings stage 'seller'")
    missing("actual AWS bill so far", "Cost Explorer needs valid credentials", "AWS console → Billing, or "
            "aws ce get-cost-and-usage")
    missing("L1 data fee per tx on Base mainnet", "depends on Ethereum blob fees; Sepolia l1Fee (~200 wei) is not "
            "representative", "eth_estimateGas + GasPriceOracle.getL1Fee on mainnet for the same calldata")
    missing("facilitator verify p95 on a warm process", "both reports include the first call that builds the "
            "facilitator (610 ms on the fork)", "drop the first sample per process in the live run")
    missing("Apify actor runtime/cost per check in the live demo", "account runs listed are earlier test runs",
            "run the demo with APIFY_TOKEN and read /v2/actor-runs after it")
    result["seconds"] = round(time.time() - t0, 1)
    write(result)
    print(f"{len(N)} numbers, {len(MISSING)} missing -> {OUT_JSON.relative_to(ROOT)}, {OUT_MD.relative_to(ROOT)} "
          f"in {result['seconds']} s")


if __name__ == "__main__":
    main()
