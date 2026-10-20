# Predicate engine eval (replaces the LLM judge eval, MODEL_V2 §6): labelled items in predicates.jsonl must get
# exactly the expected autocode. There is no model on this path, so the result is the same in MOCK and live.
from tests.evals import harness


def test_every_labelled_item_gets_the_expected_autocode():
    out = harness.predicates()
    assert out["items"] >= 25
    misses = [r for r in out["rows"] if not r["ok"]]
    assert misses == [], misses
    assert out["injection_items"] >= 2 and out["injection_as_expected"] == out["injection_items"]


def test_labels_are_mixed():
    rows = harness.load("predicates.jsonl")
    expects = {r["expect"] for r in rows}
    assert {"accepted", "CRITERIA_FAIL", "REGISTRY_CONTRADICTION", "SCHEMA_FAIL"} <= expects
