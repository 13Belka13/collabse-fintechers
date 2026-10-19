# Counterparty check (SPEC §6.4, L4): checksum, each registry parser on recorded answers, fail-closed
# deadline, sanctions near-match hidden from the client, Delta blocked, cache. No network unless LIVE=1.
import json
import time

import httpx
import pytest

from bank import codes, config, db, screening
from bank.screening import ares, datasets, isir, sanctions, vat, website
from bank.screening.ares import RegistryError

REC = config.ROOT / "fixtures" / "registries" / "recorded"
CEZ = "45274649"
DELTA, BETA, GAMA = "54763363", "30181020", "59093994"


def rec(name):
    return (REC / name).read_text(encoding="utf-8")


def mock_registries(monkeypatch, handler):
    """Route the live code path (httpx.Client inside screening) to a fake transport."""
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def cez_handler(request: httpx.Request) -> httpx.Response:
    if "ares.gov.cz" in request.url.host:
        return httpx.Response(200, json=json.loads(rec("ares_45274649.json")))
    if "adisrws" in request.url.host:
        assert b"<roz:dic>45274649</roz:dic>" in request.content  # DIČ without "CZ"
        return httpx.Response(200, text=rec("vat_45274649.xml"))
    if "isir" in request.url.host:
        assert request.url.port == 8443
        return httpx.Response(200, text=rec("isir_empty_ws2.xml"))
    raise AssertionError(f"unexpected call {request.url}")


# --- IČO checksum ---

@pytest.mark.parametrize("ico", [CEZ, "45317054", "64146103", BETA, GAMA, DELTA, "14645441"])
def test_checksum_valid(ico):
    assert screening.ico_checksum_ok(ico)


@pytest.mark.parametrize("ico", ["45274648", "1234567", "123456789", "4527464a", "", None])
def test_checksum_invalid(ico):
    assert not screening.ico_checksum_ok(ico)


def test_bad_checksum_blocks_without_network(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("network used"))
    r = screening.check("45274648")
    assert r["level"] == "blocked"
    assert [x["code"] for x in r["reasons"]] == ["ico_checksum_invalid"]
    assert r["reasons"][0]["public"] and r["reasons"][0]["source_url"]


# --- accounts (§6.5) ---

def test_normalize_account_formats():
    n = screening.normalize_account
    assert n("CZ65 0800 0000 1920 0014 5399") == "19-2000145399/0800"
    assert n("000019-2000145399/0800") == "19-2000145399/0800"
    assert n("5638829718/9999") == n("CZ1399990000005638829718") == "5638829718/9999"
    assert n(" 71504011 / 0100 ") == "71504011/0100"
    assert screening.to_iban("19-2000145399/0800") == "CZ6508000000192000145399"
    for bad in ("CZ6508000000192000145398", "DE89370400440532013000", "12345", "0/0100"):
        with pytest.raises(ValueError):
            n(bad)


# --- ARES ---

def test_ares_parse_recorded_cez():
    p = ares.parse({"status": 200, "body": json.loads(rec("ares_45274649.json"))})
    assert p["legal_entity"] and p["legal_form"] == "121"
    assert p["name"] == "ČEZ, a. s." and p["dic"] == "CZ45274649" and p["vat_registered"]
    assert not p["insolvency_flag"] and p["dissolved"] is None
    assert ares.reasons(p, CEZ) == []


def test_ares_not_found_and_errors():
    p = ares.parse({"status": 404, "body": json.loads(rec("ares_404.json"))})
    assert p == {"found": False}
    assert ares.reasons(p, DELTA)[0]["code"] == "ares_not_found"
    with pytest.raises(RegistryError):
        ares.parse({"status": 400, "body": {"kod": "CHYBA_VSTUPU"}})


def test_live_check_only_for_legal_entities(monkeypatch):
    body = json.loads(rec("ares_45274649.json")) | {"pravniForma": "101", "obchodniJmeno": "Jan Novák"}

    def handler(request):
        if "ares.gov.cz" in request.url.host:
            return httpx.Response(200, json=body)
        return cez_handler(request)

    mock_registries(monkeypatch, handler)
    r = screening.check(CEZ, live=True)
    assert r["level"] == "blocked" and [x["code"] for x in r["reasons"]] == ["legal_entities_only"]
    assert r["reasons"][0]["public"]
    assert r["name"] is None and "Novák" not in db.dumps(r)  # a sole trader's name is personal data: not kept


# --- VAT payers ---

def test_vat_parse_recorded_cez():
    p = vat.parse(rec("vat_45274649.xml"))
    assert p["unreliable"] == "NE"
    assert len(p["published_accounts"]) == 17
    assert "27-5868650297/0100" in p["published_accounts"]          # standardniUcet with predcisli
    assert "2001268200/2600" in p["published_accounts"]             # nestandardniUcet IBAN -> internal
    assert vat.reasons(p) == []


def test_vat_not_found_unreliable_and_status_errors():
    assert vat.parse(rec("vat_nenalezen.xml")) == {"unreliable": "NENALEZEN", "published_accounts": [],
                                                   "answered_on": "2026-10-19"}
    ano = rec("vat_nenalezen.xml").replace('nespolehlivyPlatce="NENALEZEN"', 'nespolehlivyPlatce="ANO"')
    r = vat.reasons(vat.parse(ano))
    assert r[0]["code"] == "vat_unreliable_payer" and r[0]["public"] and r[0]["source_url"]
    window = rec("vat_nenalezen.xml").replace('statusCode="0"', 'statusCode="2"')
    with pytest.raises(RegistryError):
        vat.parse(window)
    assert vat.plain_dic("CZ45274649") == "45274649"


# --- ISIR ---

def test_isir_parse_recorded_record_and_empty():
    p = isir.parse(rec("isir_26863154.xml"))
    (r,) = p["records"]
    assert r["case"] == "INS 10525/2016" and r["state"] == "REORGANIZ" and r["running"]
    assert r["started"] == "2016-05-09"                              # "2016-05-09Z" -> date
    reason = isir.reasons(p)[0]
    assert reason["code"] == "insolvency_record" and reason["public"]
    assert reason["source_url"].startswith("https://isir.justice.cz/")
    assert isir.parse(rec("isir_empty_ws2.xml")) == {"records": []}


def test_isir_finished_case_is_history_and_errors_raise():
    finished = rec("isir_26863154.xml").replace("REORGANIZ", "VYRIZENA")
    assert isir.reasons(isir.parse(finished)) == []
    with pytest.raises(RegistryError):
        isir.parse(rec("isir_empty_ws2.xml").replace("WS2", "WS1"))


# --- EU sanctions ---

def test_sanctions_no_hit_for_demo_and_real_names():
    for name in ("Alfa s.r.o.", "Beta s.r.o.", "Gama s.r.o.", "Delta s.r.o.", "ČEZ, a. s."):
        assert sanctions.screen(name)["matches"] == []


def test_sanctions_exact_near_and_id():
    assert sanctions.normalize("ČEZ, a. s.") == "cez"
    exact = sanctions.screen("BANK ROSSIYA")
    assert exact["reasons"][0]["code"] == "sanctions_match" and exact["reasons"][0]["public"] is False
    near = sanctions.screen("Arab Revolutionary Brigade s.r.o.")
    (r,) = near["reasons"]
    assert r["code"] == "sanctions_near_match" and r["public"] is False
    assert r["evidence"]["match"]["score"] >= sanctions.FUZZY_MIN and r["source_url"] == sanctions.XML_URL
    ent = next(e for e in sanctions.index()["entities"] if e["id_digits"])
    number = next(iter(ent["id_digits"]))
    assert sanctions.screen(None, ico=number)["matches"][0]["kind"] == "id"


def test_sanctions_near_match_hidden_from_client(monkeypatch):
    fx = screening.fixture(GAMA)
    fx["ares"]["body"]["obchodniJmeno"] = "Arab Revolutionary Brigade s.r.o."
    monkeypatch.setattr(screening, "fixture", lambda ico: fx)
    r = screening.check(GAMA)
    assert r["level"] == "review" and [x["code"] for x in r["reasons"]] == ["sanctions_near_match"]
    view = codes.client_view(codes.refuse(codes.Code.COUNTERPARTY_NOT_TRUSTED, r["reasons"]))
    assert view["code"] == "UNDER_REVIEW" and view["reasons"] == []
    assert "sanction" not in json.dumps(view).lower() and "Arab" not in json.dumps(view)
    assert screening.public_view(r) == {"level": "review", "public_reasons": [],
                                        "explanation": codes.NEUTRAL_CLIENT_TEXT}


# --- orchestration: demo fixtures ---

def test_delta_blocked_with_public_sourced_reason():
    r = screening.check(DELTA)
    assert r["demo_fixture"] and r["level"] == "blocked"
    (reason,) = r["reasons"]
    assert reason["code"] == "ico_mismatch_on_website" and reason["public"]
    assert reason["source_url"] == "https://delta-data.example/kontakt"
    assert reason["evidence"]["website_icos"] == ["14645441"]
    view = codes.client_view(codes.refuse(codes.Code.COUNTERPARTY_NOT_TRUSTED, r["reasons"]))
    assert view["code"] == "COUNTERPARTY_NOT_TRUSTED" and view["reasons"][0]["code"] == "ico_mismatch_on_website"


def test_beta_vat_payer_with_its_account_published_and_gama_ordinary():
    beta = screening.check(BETA)
    assert beta["level"] == "ok" and beta["vat"]["payer"] and beta["vat"]["unreliable"] == "NE"
    assert screening.normalize_account("CZ1399990000005638829718") in beta["vat"]["published_accounts"]
    gama = screening.check(GAMA)
    assert gama["level"] == "ok" and gama["vat"] == {"payer": False, "unreliable": "NENALEZEN",
                                                     "published_accounts": []}
    assert beta["legal_form"] == gama["legal_form"] == "112"


# --- fail-closed ---

def test_registry_timeout_fault_is_fail_closed(monkeypatch):
    monkeypatch.setenv("FAULTS", "registry_timeout")
    config.reload()
    r = screening.check(BETA)
    assert r["level"] == "review"
    timeouts = [x for x in r["reasons"] if x["code"] == "registry_timeout"]
    assert {x["evidence"]["source"] for x in timeouts} == {"ares", "vat", "isir"}
    assert all(x["public"] is False for x in timeouts)
    assert codes.client_view(codes.refuse(codes.Code.UNDER_REVIEW, r["reasons"]))["reasons"] == []


def test_slow_registry_hits_the_deadline(monkeypatch):
    monkeypatch.setattr(screening, "TIMEOUT_S", 0.2)

    def handler(request):
        if "isir" in request.url.host:
            time.sleep(1.0)
        return cez_handler(request)

    mock_registries(monkeypatch, handler)
    t0 = time.perf_counter()
    r = screening.check(CEZ, live=True)
    assert time.perf_counter() - t0 < 0.9
    assert r["level"] == "review"
    assert [(x["code"], x["evidence"]["source"]) for x in r["reasons"]] == [("registry_timeout", "isir")]
    assert r["vat"]["unreliable"] == "NE"  # the registries that answered still count


def test_registry_5xx_is_fail_closed(monkeypatch):
    def handler(request):
        if "adisrws" in request.url.host:
            return httpx.Response(503, text="down")
        return cez_handler(request)

    mock_registries(monkeypatch, handler)
    r = screening.check(CEZ, live=True)
    assert r["level"] == "review" and r["reasons"][0]["code"] == "registry_error"
    assert r["vat"]["payer"] is True and r["vat"]["unreliable"] is None  # ARES stavZdrojeDph still says payer


def test_live_path_on_recorded_cez(monkeypatch):
    mock_registries(monkeypatch, cez_handler)
    r = screening.check(CEZ, live=True, website_url="https://www.cez.cz")
    assert r["live"] and not r["demo_fixture"] and r["level"] == "ok" and r["reasons"] == []
    assert r["name"] == "ČEZ, a. s." and r["vat"]["payer"] and len(r["vat"]["published_accounts"]) == 17
    assert any("APIFY_TOKEN" in s for s in r["skipped"]) and any("MOSTLYRIGHT" in s for s in r["skipped"])


# --- cache ---

def test_cache_ttl_and_no_network(monkeypatch):
    screening.check(BETA)
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("network used on the money path"))
    assert screening.cached(BETA)["vat"]["payer"] is True
    assert screening.cached("14645441") is None
    now = time.time()
    monkeypatch.setattr(screening.time, "time", lambda: now + screening.TTL_S + 1)
    assert screening.cached(BETA) is None


def test_fail_closed_result_expires_sooner(monkeypatch):
    monkeypatch.setenv("FAULTS", "registry_timeout")
    config.reload()
    screening.check(GAMA)
    assert screening.cached(GAMA)["level"] == "review"
    now = time.time()
    monkeypatch.setattr(screening.time, "time", lambda: now + screening.TTL_FAIL_S + 1)
    assert screening.cached(GAMA) is None


def test_prefetch_fills_cache():
    screening.prefetch(DELTA)
    deadline = time.time() + 5
    while screening.cached(DELTA) is None and time.time() < deadline:
        time.sleep(0.02)
    assert screening.cached(DELTA)["level"] == "blocked"
    rows = db.rows("SELECT event, payload_json FROM journal WHERE event='screening.checked'")
    assert rows and json.loads(rows[-1]["payload_json"])["ico"] == DELTA


# --- small sources ---

def test_website_signal_mapping():
    def out(**vals):
        return {"ico": BETA, "websiteUrl": "https://x.example",
                "signals": [{"name": k, "value": v, "source_url": "https://x.example/kontakt"} for k, v in vals.items()]}
    assert [r["code"] for r in website.reasons(out(website_reachable=False))] == ["website_unreachable"]
    assert [r["code"] for r in website.reasons(out(website_reachable=True, website_mentions_ico=False))] == [
        "ico_missing_on_website"]
    assert website.reasons(out(website_reachable=True, website_mentions_ico=True, maps_permanently_closed=False)) == []
    assert [r["code"] for r in website.reasons(out(maps_permanently_closed=True))] == ["maps_permanently_closed"]


def test_datasets_flags():
    raw = {"company": [{"ico": BETA, "in_liquidation": True, "companies_at_seat": 250}],
           "records": [{"record_type": "regulator_warning"}, {"record_type": "sanction"}]}
    got = {(r["code"], r["public"]) for r in datasets.reasons(raw)}
    assert got == {("in_liquidation", True), ("mass_registered_address", False), ("regulator_warning", True),
                   ("sanctions_match", False)}
    assert datasets.reasons({"company": [], "records": []}) == []
    assert screening.level_of([{"code": "something_new"}]) == "review"  # unknown reason: fail closed


@pytest.mark.live
def test_live_cez_registries():
    r = screening.check(CEZ, live=True)
    assert r["live"] and r["legal_form"] == "121" and r["name"].startswith("ČEZ")
    assert r["vat"]["payer"] and r["vat"]["unreliable"] == "NE" and r["level"] == "ok"
