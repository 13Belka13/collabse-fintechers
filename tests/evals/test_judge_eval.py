# Judge eval (SPEC §19.3): labelled items in judge.jsonl (seed of 20; target 40). In MOCK the harness checks
# shape only (valid JSON for every item, flags never move money); with LIVE=1 and Bedrock credentials it
# measures precision/recall of the "not a warehouse" flag on the real fast model.
import json
from pathlib import Path

import pytest

from bank import config, verifier

ITEMS = [json.loads(line) for line in (Path(__file__).parent / "judge.jsonl").read_text(encoding="utf-8").splitlines()
         if line.strip()]


def run_judge():
    batch = [{k: it[k] for k in verifier.JUDGE_FIELDS} for it in ITEMS]
    verdicts = {v["placeId"]: v for v in verifier.judge(batch, {}, deal_id="eval-judge")}
    tp = fp = fn = 0
    for it in ITEMS:
        flagged = verdicts[it["placeId"]]["flagged"]
        bad = not it["label_plausible_warehouse"]
        tp += flagged and bad
        fp += flagged and not bad
        fn += bad and not flagged
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return verdicts, precision, recall


def test_seed_is_labelled_and_mixed():
    assert len(ITEMS) >= 20
    labels = [it["label_plausible_warehouse"] for it in ITEMS]
    assert labels.count(True) >= 8 and labels.count(False) >= 8
    assert any("SYSTEM:" in it["title"] for it in ITEMS)  # injection case present


def test_judge_answers_every_item_in_mock():
    verdicts, precision, recall = run_judge()
    assert set(verdicts) == {it["placeId"] for it in ITEMS}
    assert all(v["reason"] not in ("judge_invalid_json", "judge_missing_item") for v in verdicts.values())
    print(f"\nMOCK judge (keyword heuristic): precision {precision:.2f} recall {recall:.2f}")


@pytest.mark.live
def test_judge_quality_live(monkeypatch):
    monkeypatch.setenv("MOCK", "0")
    config.reload()
    verdicts, precision, recall = run_judge()
    injected = next(it for it in ITEMS if "SYSTEM:" in it["title"])
    assert verdicts[injected["placeId"]]["flagged"]
    assert precision >= 0.8 and recall >= 0.8, (precision, recall)
