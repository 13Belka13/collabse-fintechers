# Two-level explanations (SPEC §6.7, §19.3): template first, AI text only with cited sources ⊆ evidence,
# nothing non-public in a client text, AI label on every AI text.
import json

import pytest

from bank import codes, db, explain, llm
from bank.codes import Code

ARES = "https://ares.gov.cz/ekonomicke-subjekty?ico=54763363"
PUBLIC = codes.refuse(Code.COUNTERPARTY_NOT_TRUSTED, [
    codes.reason("ico_mismatch_on_website", True, "IČO on the website differs from ARES", ARES)], deal_id="d1")
HIDDEN = codes.refuse(Code.COUNTERPARTY_NOT_TRUSTED, [
    codes.reason("ico_mismatch_on_website", True, "IČO on the website differs from ARES", ARES),
    codes.reason("sanctions_near_match", False, "Name 92% similar to a sanctioned entity",
                 "https://webgate.ec.europa.eu/fsd/fsf")], deal_id="d1")


def test_template_is_instant_and_lists_reasons_and_next_step():
    out = explain.render(PUBLIC, "client")
    assert out == {"text": out["text"], "cited_sources": [ARES], "ai_generated": False}
    assert "did not pass" in out["text"] and "IČO on the website differs" in out["text"]
    assert "Next: a human decides." in out["text"]
    assert db.one("SELECT COUNT(*) n FROM timings WHERE model IS NOT NULL")["n"] == 0  # no model call


def test_client_never_sees_a_non_public_reason_even_with_ai():
    for ai in (False, True):
        out = explain.render(HIDDEN, "client", ai=ai)
        assert out["text"] == codes.NEUTRAL_CLIENT_TEXT and out["cited_sources"] == [] and not out["ai_generated"]
    assert db.one("SELECT COUNT(*) n FROM timings WHERE stage='explain'")["n"] == 0  # model never asked


def test_compliance_sees_everything_tagged():
    out = explain.render(HIDDEN, "compliance")
    assert "92% similar" in out["text"] and "[non-public, compliance only]" in out["text"]
    assert out["cited_sources"] == [ARES, "https://webgate.ec.europa.eu/fsd/fsf"]


def test_ai_text_is_labelled_and_cites_a_subset():
    out = explain.render(PUBLIC, "client", ai=True)
    assert out["ai_generated"] and out["label"] == explain.AI_LABEL
    assert set(out["cited_sources"]) <= {ARES} and "COUNTERPARTY_NOT_TRUSTED" in out["text"]
    row = db.one("SELECT model, deal_id FROM timings WHERE stage='explain'")
    assert row == {"model": "mock", "deal_id": "d1"}


def test_ai_citing_an_unknown_source_falls_back_to_template(monkeypatch):
    monkeypatch.setitem(llm.MOCKS, "explain", lambda m, t: {"text": json.dumps(
        {"text": "See evil.example", "cited_sources": ["https://evil.example"]})})
    out = explain.render(PUBLIC, "client", ai=True)
    assert not out["ai_generated"] and out["text"] == explain.template(PUBLIC, "client")


def test_ai_client_text_with_a_forbidden_word_falls_back(monkeypatch):
    monkeypatch.setitem(llm.MOCKS, "explain", lambda m, t: {"text": json.dumps(
        {"text": "Blocked because of a sanctions list hit.", "cited_sources": [ARES]})})
    assert not explain.render(PUBLIC, "client", ai=True)["ai_generated"]
    assert explain.render(PUBLIC, "compliance", ai=True)["ai_generated"]  # compliance may read it


def test_ai_bad_json_falls_back(monkeypatch):
    monkeypatch.setitem(llm.MOCKS, "explain", lambda m, t: {"text": "no json here"})
    out = explain.render(PUBLIC, "compliance", ai=True)
    assert not out["ai_generated"] and out["text"] == explain.template(PUBLIC)


def test_ok_decision_with_mark_and_unknown_audience():
    d = codes.ok(deal_id="d2", marks=["VAT risk acknowledged 2026-10-19"])
    out = explain.render(d, "client", ai=True)
    assert out["ai_generated"] and out["cited_sources"] == []
    assert "VAT risk acknowledged 2026-10-19" in explain.template(d, "client")
    with pytest.raises(ValueError):
        explain.render(d, "press")


def test_every_code_has_a_headline_and_next_step():
    assert set(explain.HEADLINES) == set(Code)
    assert set(explain.NEXT_STEP) == {nt for _, nt in codes.SPEC.values()}
