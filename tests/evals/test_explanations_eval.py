# Explanation evals (SPEC §19.3, §6.7): over every closed-enum code and decisions with non-public reasons,
# template and AI text (MOCK), client and compliance: cited_sources ⊆ evidence 100 %, no non-public reason or
# forbidden word in any client text, every AI text labelled (AI Act Art. 50).
from tests.evals import harness


def test_explanations_meet_the_three_rules():
    r = harness.explanations()
    assert r["texts"] >= 4 * 14
    assert r["sources_subset"] == r["texts"]
    assert r["client_clean"] == r["client_texts"]
    assert r["ai_texts"] > 0 and r["ai_labelled"] == r["ai_texts"]


def test_aml_and_sanctions_reasons_never_reach_the_client():
    r = harness.explanations()
    hidden = [x for x in r["rows"] if x["audience"] == "client" and x["code"] in ("COUNTERPARTY_NOT_TRUSTED", "UNDER_REVIEW")]
    assert hidden and all(x["no_hidden_reason"] and x["no_forbidden_word"] for x in hidden)
