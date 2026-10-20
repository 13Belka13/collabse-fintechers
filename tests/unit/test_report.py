# scripts/report.py on synthetic inputs: attack counting, stage percentiles vs targets, coverage targets, and
# fork runs labelled without explorer links.
import json

from scripts import report


def _result(sid, run, status, attack=False, money_ok=True, defects=(), txs=()):
    checks = [{"name": "decision ok", "ok": status == "pass"}]
    if attack:
        checks.append({"name": "no money moved: balances equal (chain)", "ok": money_ok})
    return {"id": sid, "title": sid, "run": run, "status": status, "attack": attack, "defects": list(defects),
            "checks": checks, "txs": list(txs)}


def _raw(target="fork"):
    tx = {"kind": "authorize", "status": "mined", "tx_hash": "0x" + "ab" * 32, "link": None if target == "fork" else "https://x/tx/1"}
    return {"target": target, "runs": 2, "label": "anvil fork of Base Sepolia (chain id 84532) at block 1",
            "results": [_result("S02", 1, "pass", txs=[tx]), _result("S02", 2, "pass", txs=[tx]),
                        _result("N03", 1, "pass", attack=True), _result("N03", 2, "xfail", attack=True, defects=["D1"]),
                        _result("N13", 1, "xfail", defects=["D2"]), _result("N13", 2, "xfail", defects=["D2"])],
            "reconciliation": {"wrong_debit_count": 0, "transfers_to_evil": 0, "ok": True, "deals_checked": 2,
                               "deals_ok": 2},
            "journal_verified": {"bank-main.db": True}}  # no "db" paths: cost gas is n/a


def test_summary_counts_attacks_blocked_at_money_level_and_with_the_code():
    s = report.summarize(_raw())
    assert (s["scenarios"], s["scenario_runs"], s["attack_runs"]) == (3, 6, 2)
    assert s["attacks_blocked_money"] == 2 and s["attacks_expected_code"] == 1 and s["attacks_not_run"] == []
    assert s["status"] == {"pass": 3, "xfail": 3, "fail": 0}
    assert s["per_scenario"]["N03"] == ["pass", "xfail"]


def test_an_attack_stopped_at_its_precondition_is_not_run_not_blocked():
    raw = _raw()
    raw["results"].append({"id": "N10", "title": "N10", "run": 1, "status": "fail", "attack": True, "defects": [],
                           "checks": [{"name": "tCZK is the stage token with strict mode", "ok": False}], "txs": []})
    s = report.summarize(raw)
    assert s["attacks_not_run"] == ["N10"] and s["attacks_executed"] == 2 and s["attacks_blocked_money"] == 2


def test_money_reaching_the_attacker_zeroes_the_blocked_count():
    raw = _raw()
    raw["reconciliation"]["transfers_to_evil"] = 1
    assert report.summarize(raw)["attacks_blocked_money"] == 0


def test_stage_table_uses_p95_against_the_spec_targets():
    stages = {"decision": [1.0] * 19 + [60.0], "tx_in_block:authorize": [100.0], "tx_in_block:capture": [7000.0],
              "block_to_feed": [900.0], "block_to_feed:tatum": [1200.0]}
    rows = {r["stage"]: r for r in report.stage_table(stages)}
    assert rows["decision"]["p95"] == 1.0 and rows["decision"]["met"] is True  # 19 of 20 under 50 ms
    assert rows["tx_in_block:*"]["n"] == 2 and rows["tx_in_block:*"]["met"] is False
    assert rows["block_to_feed*"]["n"] == 2 and rows["block_to_feed*"]["met"] is True  # the stage webhooks.py times
    assert rows["facilitator_verify"]["n"] == 0 and rows["facilitator_verify"]["met"] is None


def test_fork_matrix_has_hashes_without_links_and_sepolia_has_links():
    assert "](" not in report.matrix_md(_raw("fork")) and "`0xabababab…ababab`" in report.matrix_md(_raw("fork"))
    assert "[authorize](https://x/tx/1)" in report.matrix_md(_raw("sepolia"))
    assert "XFAIL (D1)" in report.matrix_md(_raw())


def test_coverage_targets(tmp_path):
    p = tmp_path / "coverage.json"
    p.write_text(json.dumps({"files": {
        "bank/mandate.py": {"summary": {"covered_lines": 95, "num_statements": 100, "covered_branches": 46, "num_branches": 50}},
        "bank/verifier.py": {"summary": {"covered_lines": 50, "num_statements": 100, "covered_branches": 10, "num_branches": 20}}},
        "totals": {"covered_lines": 145, "num_statements": 200, "covered_branches": 56, "num_branches": 70}}))
    cov = report.coverage(p)
    targets = {t["target"]: t for t in cov["targets"]}
    assert targets["bank/mandate.py branches ≥ 90 %"]["met"] and not targets["bank/verifier.py branches ≥ 90 %"]["met"]
    assert not targets["bank/ lines ≥ 80 %"]["met"] and round(cov["total"]["lines"], 1) == 72.5
    assert "| bank/mandate.py branches ≥ 90 % | 92.0 % | yes |" in report.coverage_md(cov)
    assert report.coverage(tmp_path / "missing.json") is None


def test_build_renders_the_report_from_a_raw_run(tmp_path, monkeypatch):
    raw = {**_raw("sepolia"), "stages": {"decision": [2.0]}, "finished": 0, "defects": {"D1": "x", "D2": "y"},
           "llm_totals": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
           "stack": {"funding": "faucets", "tczk": "0x1", "addresses": {"operator": "0x2"}}}
    (tmp_path / "scenarios-sepolia.json").write_text(json.dumps(raw))
    inj = {k: 1 for k in ("money_blocked", "variants", "money_runs_blocked", "money_runs", "outcome_as_expected",
                          "controls", "controls_paid", "scripted_agent_followed", "scripted_agent_runs",
                          "items_paid_by_injection", "items_variants")}
    ex = {k: 1 for k in ("sources_subset", "texts", "client_clean", "client_texts", "ai_labelled", "ai_texts")}
    pr = {"items": 9, "as_expected": 9, "injection_as_expected": 1, "injection_items": 1}
    monkeypatch.setattr(report, "REPORTS", tmp_path)
    monkeypatch.setattr(report.config, "ROOT", tmp_path)
    monkeypatch.setattr(report, "evals", lambda: {"source": "test", "injections": inj, "explanations": ex,
                                                  "predicates": pr})
    md, out = report.build("sepolia")
    assert "Predicate engine (code, no model): 9/9" in md and out["summary"]["scenario_runs"] == 6
