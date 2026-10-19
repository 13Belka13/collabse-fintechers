# Validation report (SPEC §19.6): reports/validation.md + reports/validation.json from
#   reports/scenarios-<target>.json   (scripts/scenarios.py: scenarios × runs, txs, reconciliation, stage timings)
#   reports/coverage.json             (pytest --cov=bank --cov-branch --cov-report=json:reports/coverage.json)
#   the eval harness (tests/evals/harness.py, MOCK, run here) or reports/evals-live.json after a live Bedrock run.
# Every number in the report comes from those files; nothing is typed in by hand.
#   python -m scripts.report [--target fork|sepolia]
#   python -m scripts.report --coverage-only reports/coverage.json     (markdown table for the CI summary)
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from bank import config, timing

REPORTS = config.ROOT / "reports"
# SPEC §19.4 targets (p95, ms).
TARGETS = [
    ("decision", "Mandate decision + cached check + journal", 50),
    ("facilitator_verify", "Bank facilitator verify (with screening)", 500),
    ("screening", "New counterparty: ARES + VAT + ISIR + sanctions", 2500),
    ("tx_in_block:*", "Signature → tx in block (settle, authorize, capture, void)", 6000),
    ("bank_feed", "Tx in block → row in the /bank feed", 5000),
    ("refusal_to_client", "Refusal → message to the client", 100),
]
# SPEC §19.5 coverage targets: bank/ ≥ 80 % lines; these ≥ 90 % branches.
BRANCH_TARGETS = ["bank/mandate.py", "bank/verifier.py", "bank/explain.py", "bank/facilitator.py",
                  "bank/screening/__init__.py"]
MONEY_CHECK_WORDS = ("balances equal", "nonces equal", "no onchain_txs row", "no tCZK moved", "exactly one",
                     "reverted", "debited exactly")


def pct(a, b):
    return 100.0 * a / b if b else None


def fmt_pct(v):
    return "n/a" if v is None else f"{v:.1f} %"


# ---- coverage ----

def coverage(path: Path) -> dict | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    files = {}
    for name, f in data["files"].items():
        s = f["summary"]
        files[name] = {"lines": pct(s["covered_lines"], s["num_statements"]),
                       "branches": pct(s.get("covered_branches", 0), s.get("num_branches", 0))}
    t = data["totals"]
    total = {"lines": pct(t["covered_lines"], t["num_statements"]),
             "branches": pct(t.get("covered_branches", 0), t.get("num_branches", 0))}
    targets = [{"target": "bank/ lines ≥ 80 %", "value": total["lines"], "met": (total["lines"] or 0) >= 80}]
    for name in BRANCH_TARGETS:
        v = files.get(name, {}).get("branches")
        targets.append({"target": f"{name} branches ≥ 90 %", "value": v, "met": v is not None and v >= 90})
    return {"total": total, "targets": targets, "files": files}


def coverage_md(cov: dict | None) -> str:
    if cov is None:
        return ("Coverage: no reports/coverage.json "
                "(run `pytest --cov=bank --cov-branch --cov-report=json:reports/coverage.json`).\n")
    lines = ["| Target (SPEC §19.5) | Actual | Met |", "|---|---|---|"]
    for t in cov["targets"]:
        lines.append(f"| {t['target']} | {fmt_pct(t['value'])} | {'yes' if t['met'] else 'no'} |")
    lines.append(f"\nbank/ total: lines {fmt_pct(cov['total']['lines'])}, branches {fmt_pct(cov['total']['branches'])}"
                 " (reported, not a gate).")
    return "\n".join(lines) + "\n"


# ---- scenarios ----

def stage_table(stages: dict[str, list[float]]) -> list[dict]:
    rows = []
    for key, title, target in TARGETS:
        if key.endswith("*"):
            ms = [v for k, vs in stages.items() if k.startswith(key[:-1]) for v in vs]
        else:
            ms = stages.get(key, [])
        p95 = timing.percentile(ms, 95)
        rows.append({"stage": key, "title": title, "target_ms": target, "n": len(ms),
                     "p50": timing.percentile(ms, 50), "p95": p95, "met": None if p95 is None else p95 <= target})
    return rows


def money_ok(r: dict) -> bool:
    money = [c for c in r["checks"] if any(w in c["name"] for w in MONEY_CHECK_WORDS)]
    return bool(money) and all(c["ok"] for c in money)


def executed(r: dict) -> bool:
    """The attack was actually tried: some money check ran (a precondition failure stops before anything is sent)."""
    return any(any(w in c["name"] for w in MONEY_CHECK_WORDS) for c in r["checks"])


def summarize(raw: dict) -> dict:
    results = raw["results"]
    ids = list(dict.fromkeys(r["id"] for r in results))
    attacks = [r for r in results if r["attack"]]
    recon = raw["reconciliation"]
    return {
        "scenarios": len(ids), "p0": sum(i[0] in "SN" for i in ids), "faults": sum(i.startswith("F") for i in ids),
        "extra": sum(i[0] not in "SNF" for i in ids),
        "attack_scenarios": len({r["id"] for r in attacks}), "runs": raw["runs"], "scenario_runs": len(results),
        "status": {s: sum(r["status"] == s for r in results) for s in ("pass", "xfail", "fail")},
        "attack_runs": len(attacks),
        "attacks_executed": sum(executed(r) for r in attacks),
        "attacks_not_run": sorted({r["id"] for r in attacks if not executed(r)}),
        "attacks_blocked_money": sum(money_ok(r) for r in attacks if executed(r)) if recon["transfers_to_evil"] == 0 else 0,
        "attacks_expected_code": sum(r["status"] == "pass" for r in attacks if executed(r)),
        "txs_landed": sum(t["status"] in ("mined", "reverted") for r in results for t in r["txs"]),
        "txs_dead": sum(t["status"] == "failed" for r in results for t in r["txs"]),
        "wrong_debits": recon["wrong_debit_count"], "transfers_to_evil": recon["transfers_to_evil"],
        "reconciliation_ok": recon["ok"], "deals_reconciled": recon["deals_checked"], "deals_reconciled_ok": recon["deals_ok"],
        "journal_verified": all(raw.get("journal_verified", {}).values()),
        "per_scenario": {i: [r["status"] for r in sorted((x for x in results if x["id"] == i), key=lambda x: x["run"])]
                         for i in ids},
    }


def short(h: str) -> str:
    return f"{h[:10]}…{h[-6:]}"


def matrix_md(raw: dict) -> str:
    fork, runs = raw["target"] == "fork", raw["runs"]
    lines = ["| ID | Scenario | " + " | ".join(f"Run {i}" for i in range(1, runs + 1)) + " | Transactions (last run) |",
             "|---|---|" + "---|" * runs + "---|"]
    for sid in dict.fromkeys(r["id"] for r in raw["results"]):
        rs = sorted((r for r in raw["results"] if r["id"] == sid), key=lambda r: r["run"])
        cells = [r["status"].upper() + (f" ({', '.join(r['defects'])})" if r["status"] == "xfail" else "") for r in rs]
        txs = [t for t in rs[-1]["txs"] if t["status"] in ("mined", "reverted")]  # dead nonce retries never landed
        if fork:
            tx = ", ".join(f"{t['kind']} `{short(t['tx_hash'])}`" for t in txs) or "—"
        else:
            tx = ", ".join(f"[{t['kind']}]({t['link']})" for t in txs) or "—"
        lines.append(f"| {sid} | {rs[0]['title']} | " + " | ".join(cells) + f" | {tx} |")
    return "\n".join(lines) + "\n"


# ---- evals ----

def evals() -> dict:
    live = REPORTS / "evals-live.json"
    if live.exists():
        out = json.loads(live.read_text())
        out["source"] = "reports/evals-live.json (live Bedrock run)"
        return out
    from tests.evals import harness
    out = harness.run_all()
    out["source"] = "tests/evals/harness.py, MOCK (scripted models, keyword-heuristic judge)"
    return out


def strip_rows(e: dict) -> dict:
    return {k: ({kk: vv for kk, vv in v.items() if kk != "rows"} if isinstance(v, dict) else v) for k, v in e.items()}


# ---- report ----

def git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def build(target: str) -> tuple[str, dict]:
    raw_path = REPORTS / f"scenarios-{target}.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    s, stages, cov, ev = summarize(raw), stage_table(raw["stages"]), coverage(REPORTS / "coverage.json"), evals()
    inj, jd, ex, llm = ev["injections"], ev["judge"], ev["explanations"], raw["llm_totals"]
    fork = target == "fork"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(raw["finished"]))
    head = git_head()
    used = sorted({d for r in raw["results"] for d in r["defects"]})
    f = lambda v: "—" if v is None else f"{v:.1f} ms"  # noqa: E731

    md = [f"# Validation report — {raw['label']}", ""]
    if fork:
        md += ["> **Network: anvil fork of Base Sepolia (local), not the public testnet.** The real deployed contracts (EURC,",
               "> AuthCaptureEscrow, ERC-3009 collector) forked at the block above, a freshly deployed tCZK, throwaway keys.",
               "> The transactions are real EVM transactions on the local fork; their hashes exist only there, so they have",
               "> **no explorer links**. The same matrix on Base Sepolia is a separate run (`--target sepolia`, keys from .env).", ""]
    else:
        md += ["> **Network: Base Sepolia (public testnet).** Every transaction links to the explorer.", ""]
    md += [f"Generated {when} from `{raw_path.relative_to(config.ROOT)}` at commit `{head}` by `scripts/report.py`. "
           f"A prototype validated on {'a fork of ' if fork else ''}the testnet, ready for a bank sandbox pilot — "
           "not production-ready.", "",
           "## Summary", "",
           f"- **{s['scenarios']} scenarios** ({s['p0']} P0 from SPEC §19.2, {s['faults']} fault injections, "
           f"{s['extra']} extra: A01 bank API auth), "
           f"{s['attack_scenarios']} of them attacks or refusals, **{s['runs']} run{'s' if s['runs'] != 1 else ''} each** = "
           f"{s['scenario_runs']} scenario "
           f"runs: {s['status']['pass']} pass, {s['status']['xfail']} xfail (pending fix named below), "
           f"{s['status']['fail']} fail.",
           f"- **Attacks stopped before any money moved: {s['attacks_blocked_money']}/{s['attacks_executed']}** executed "
           "attack runs (balances equal on chain, the bank's signer nonces unchanged, no tx for the deal"
           + ("; N10/N11 as real reverted txs" if not {"N10", "N11"} & set(s["attacks_not_run"]) else "") + "); "
           f"with the expected refusal code: {s['attacks_expected_code']}/{s['attacks_executed']}"
           + (f"; not executed (precondition, nothing sent): {', '.join(s['attacks_not_run'])}" if s["attacks_not_run"]
              else "") + ".",
           f"- Transactions: {s['txs_landed']} landed on chain; {s['txs_dead']} signed transactions were dropped as dead "
           "after a nonce clash and re-sent under the same idempotency key (they never landed — see reconciliation).",
           f"- **Wrong debits: {s['wrong_debits']}** — every buyer debit on chain since the first scenario is a lock or a "
           f"payment a scenario expected. Transfers to the attacker address 0xdEaD: {s['transfers_to_evil']}.",
           f"- **Reconciliation chain ↔ DB ↔ statement: {'OK' if s['reconciliation_ok'] else 'MISMATCH'}** — "
           f"{s['deals_reconciled_ok']}/{s['deals_reconciled']} escrow deals (locked = captured + voided + still capturable, "
           "from Transfer events and paymentState; captured = journal = statement). Journal hash chains: "
           f"{'verified' if s['journal_verified'] else 'BROKEN'}.",
           f"- Injection evals: money-level block **{inj['money_blocked']}/{inj['variants']}** variants "
           f"({inj['money_runs_blocked']}/{inj['money_runs']} model runs). Judge ({jd['model']}): precision "
           f"{jd['precision']:.2f}, recall {jd['recall']:.2f} on {jd['items']} labelled items.",
           f"- Coverage of bank/: lines {fmt_pct(cov['total']['lines']) if cov else 'n/a'}, branches "
           f"{fmt_pct(cov['total']['branches']) if cov else 'n/a'}.",
           f"- Model spend in the scenario runs: {llm['calls']} calls, {llm['input_tokens']} in / {llm['output_tokens']} "
           f"out tokens, ${llm['cost_usd']:.4f} (estimate at list prices, `bank/timing.py`; MOCK calls cost $0).", "",
           "## Scenarios × runs", "", matrix_md(raw)]
    if used:
        md += ["### Pending fixes behind the xfails", "", "| Defect | What | Scenarios |", "|---|---|---|"]
        for d in used:
            ids = sorted({r["id"] for r in raw["results"] if d in r["defects"]})
            md.append(f"| {d} | {raw['defects'][d]} | {', '.join(ids)} |")
        md.append("")
    failed = [r for r in raw["results"] if r["status"] == "fail"]
    if failed:
        md += ["### Failures", "", "| ID | Run | First failed check | Detail |", "|---|---|---|---|"]
        for r in failed:
            c = next(c for c in r["checks"] if not c["ok"])
            md.append(f"| {r['id']} | {r['run']} | {c['name']} | {str(c['detail'] or '')[:160]} |")
        md.append("")
    md += ["## Speed (SPEC §19.4, measured in the matrix runs)", ""]
    if fork:
        md += ["Fork caveat: anvil mines on demand, so *tx in block* here is local and says nothing about Base's 2 s "
               "blocks; registry checks use the recorded fixtures of the synthetic companies.", ""]
    md += ["The `decision` samples include N02, where 10 concurrent pay() calls queue on one SQLite write lock "
           "(BEGIN IMMEDIATE) by design; `facilitator_verify` includes the first call of each bank process, which "
           "builds the facilitator.", "",
           "| Stage | Target p95 | n | p50 | p95 | Met |", "|---|---|---|---|---|---|"]
    for r in stages:
        met = "not measured" if r["n"] == 0 else ("yes" if r["met"] else "no")
        md.append(f"| {r['title']} (`{r['stage']}`) | ≤ {r['target_ms']} ms | {r['n']} | {f(r['p50'])} | {f(r['p95'])} "
                  f"| {met} |")
    md += ["", "## Coverage (SPEC §19.5)", "", coverage_md(cov),
           f"## Evals (SPEC §19.3) — {ev['source']}", "",
           f"- Injections ({inj['variants']} variants + {inj['controls']} controls, buyer tool loop against the real bank "
           f"API with a fake escrow lock): money-level block {inj['money_blocked']}/{inj['variants']}; outcome as expected "
           "(refusal code, or the agreed offer paid to the registered wallet) "
           f"{inj['outcome_as_expected']}/{inj['variants']}; honest controls paid {inj['controls_paid']}/{inj['controls']}.",
           "- Agent behaviour (scripted MOCK buyer — a property of the script, not of a model): it followed the injected "
           f"text in {inj['scripted_agent_followed']}/{inj['scripted_agent_runs']} runs; the compromised model tries every "
           "unsafe call by construction and the tool layer rejected all of them.",
           f"- Injected delivered items paid anyway: {inj['items_paid_by_injection']}/{inj['items_variants']} item variants "
           "(judge quality; capped at the per-item price inside the escrowed amount, never a new payee).",
           f"- Judge on {jd['items']} labelled items: precision {jd['precision']:.2f}, recall {jd['recall']:.2f}, valid JSON "
           f"{jd['valid_json_rate'] * 100:.0f} %, injection items flagged: {'yes' if jd['injection_flagged'] else 'no'}.",
           f"- Explanations: cited sources ⊆ evidence {ex['sources_subset']}/{ex['texts']}; client texts without a "
           f"non-public reason or forbidden word {ex['client_clean']}/{ex['client_texts']}; AI texts labelled "
           f"{ex['ai_labelled']}/{ex['ai_texts']}.",
           "", "## Model cost", "",
           f"Scenario runs: {llm['calls']} model calls, {llm['input_tokens']} input / {llm['output_tokens']} output tokens, "
           f"**${llm['cost_usd']:.4f}** — estimate at list prices (`bank/timing.py` PRICES); Bedrock bills separately. "
           f"Mode: {'MOCK (scripted replies)' if llm['cost_usd'] == 0 else 'live Bedrock'}.", "",
           "## Reproduce", "", "```bash",
           f"python -m scripts.scenarios --runs {raw['runs']} --target {target}",
           "pytest --cov=bank --cov-branch --cov-report=json:reports/coverage.json",
           f"python -m scripts.report --target {target}", "```", "",
           f"Stack: {raw['stack']['funding']}; tCZK `{raw['stack']['tczk']}`; bank signers "
           + ", ".join(f"{k} `{v}`" for k, v in raw["stack"]["addresses"].items()) + ".", ""]
    out = {"generated": when, "commit": head, "target": target, "label": raw["label"], "summary": s, "stages": stages,
           "coverage": cov and {"total": cov["total"], "targets": cov["targets"]}, "evals": strip_rows(ev),
           "llm_totals": llm, "reconciliation": {k: v for k, v in raw["reconciliation"].items() if k != "deals"},
           "defects": {d: raw["defects"][d] for d in used}}
    return "\n".join(md), out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build reports/validation.md|json (SPEC §19.6)")
    ap.add_argument("--target", default="fork", choices=["fork", "sepolia"])
    ap.add_argument("--coverage-only", metavar="COVERAGE_JSON", default=None)
    a = ap.parse_args(argv)
    if a.coverage_only:
        print("### Coverage targets (SPEC §19.5)\n")
        print(coverage_md(coverage(Path(a.coverage_only))))
        return 0
    md, out = build(a.target)
    name = "validation" if a.target == "fork" else f"validation-{a.target}"  # the fork report stays validation.md
    (REPORTS / f"{name}.md").write_text(md, encoding="utf-8")
    (REPORTS / f"{name}.json").write_text(json.dumps(out, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"wrote reports/{name}.md and reports/{name}.json ({out['label']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
