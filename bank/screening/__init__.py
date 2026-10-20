# Counterparty check (SPEC §6.4, layer L4). Order cheap -> expensive: IČO checksum locally; then ARES,
# VAT payers, ISIR (and Mostly Right datasets if keyed) in parallel with one 2 s deadline, fail-closed
# (no answer -> level "review" with a non-public registry_timeout reason); EU sanctions locally on the ARES
# name; web footprint from the Apify actor. Result cached in `screenings` for 1 h.
# Money path uses cached() only — never the network. Synthetic demo companies answer from recorded
# fixtures (fixtures/registries/<ico>.json, demo_fixture=true) through the same parsers as live answers.
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import httpx

from bank import codes, config, db, journal, timing
from bank.screening import ares, datasets, isir, sanctions, vat, website
from bank.screening.accounts import normalize_account, to_iban  # noqa: F401  (public API)

TIMEOUT_S = 2.0
TTL_S = 3600
TTL_FAIL_S = 60  # a fail-closed answer is retried by the next prefetch after a minute, not after an hour
FIXTURES = config.ROOT / "fixtures" / "registries"
CHECKSUM_URL = "https://cs.wikipedia.org/wiki/Identifika%C4%8Dn%C3%AD_%C4%8D%C3%ADslo_osoby"
LEVELS = ("ok", "review", "blocked")
# Fine-grained reason -> level. Anything not listed counts as "review" (fail closed).
REASON_LEVEL = {
    "ico_checksum_invalid": "blocked", "ares_not_found": "blocked", "legal_entities_only": "blocked",
    "ares_dissolved": "blocked", "insolvency_record": "blocked", "in_liquidation": "blocked",
    "vat_unreliable_payer": "blocked", "sanctions_match": "blocked", "ico_mismatch_on_website": "blocked",
    "maps_permanently_closed": "blocked",
    "sanctions_near_match": "review", "registry_timeout": "review", "registry_error": "review",
    "ico_missing_on_website": "review", "website_unreachable": "review", "website_check_failed": "review",
    "website_risk_high": "review",
    "enforcement_record": "review", "regulator_warning": "review", "mass_registered_address": "review",
}
FAIL_CLOSED = {"registry_timeout", "registry_error", "website_check_failed"}
REGISTRY_NAMES = {"ares": ("ARES", ares.URL.split("{")[0]), "vat": ("VAT payer register", vat.SOURCE_URL),
                  "isir": ("ISIR", isir.SOURCE_URL), "datasets": ("Mostly Right datasets", datasets.SOURCE_URL)}

_inflight: set[str] = set()
_lock = threading.Lock()


def ico_checksum_ok(ico: str) -> bool:
    """Weights 8..2 on the first 7 digits; c = (11 - s mod 11) mod 10 must equal the 8th digit (ARES does not check)."""
    if not (isinstance(ico, str) and len(ico) == 8 and ico.isdigit()):
        return False
    s = sum(int(d) * w for d, w in zip(ico[:7], range(8, 1, -1)))
    return (11 - s % 11) % 10 == int(ico[7])


def level_of(reasons: list[dict]) -> str:
    return max((REASON_LEVEL.get(r["code"], "review") for r in reasons), key=LEVELS.index, default="ok")


def fixture(ico: str) -> dict | None:
    """A recorded demo company, or one of the synthetic item companies (fixtures/registries/items.json: ARES only;
    VAT and ISIR answer "not found" in the recorded live format)."""
    p = FIXTURES / f"{ico}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    body = _items().get(ico)
    if body is None:
        return None
    rec = FIXTURES / "recorded"
    return {"demo_fixture": True, "ico": ico, "ares": {"status": 200, "body": body},
            "vat_xml": (rec / "vat_nenalezen.xml").read_text(encoding="utf-8"),
            "isir_xml": (rec / "isir_empty_ws2.xml").read_text(encoding="utf-8")}


_items_cache: dict = {}


def _items() -> dict:
    if not _items_cache:
        _items_cache.update(json.loads((FIXTURES / "items.json").read_text(encoding="utf-8"))["ares"])
    return _items_cache


def _registry_fail(source: str, exc: BaseException | None) -> dict:
    name, url = REGISTRY_NAMES[source]
    if exc is None or isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return codes.reason("registry_timeout", False, f"{name} did not answer within {TIMEOUT_S:g} s "
                            "(fail-closed: the payment waits for review).", url, source=source)
    return codes.reason("registry_error", False, f"{name} gave no usable answer: {str(exc)[:200]}", url,
                        source=source)


def _fetchers(ico: str, fx: dict | None, client) -> dict:
    """source -> zero-arg callable returning the raw answer (recorded or live)."""
    def faulty(fn):
        def run():
            if "registry_timeout" in config.FAULTS:  # SPEC §19.2 fault injection
                raise httpx.TimeoutException("FAULTS=registry_timeout")
            return fn()
        return run

    if fx is not None:
        out = {"ares": lambda: fx["ares"], "vat": lambda: fx["vat_xml"], "isir": lambda: fx["isir_xml"]}
    else:
        out = {"ares": lambda: ares.fetch(ico, client), "vat": lambda: vat.fetch(ico, client),
               "isir": lambda: isir.fetch(ico, client)}
        if datasets.enabled():
            out["datasets"] = lambda: datasets.fetch(ico, client)
    return {k: faulty(v) for k, v in out.items()}


def _check(ico: str, wallet: str | None, website_url: str | None, live: bool) -> dict:
    res = {"ico": ico, "name": None, "legal_form": None, "level": "ok", "reasons": [], "sources": [],
           "vat": {"payer": None, "unreliable": None, "published_accounts": [], "source_url": vat.SOURCE_URL},
           "isir": [],
           "sanctions": [], "website": None, "wallet": wallet, "website_url": website_url,
           "fetched_at": time.time(), "demo_fixture": False, "live": False, "skipped": []}
    if not ico_checksum_ok(ico):
        res["reasons"] = [codes.reason("ico_checksum_invalid", True, f"IČO {ico!r} fails the mod-11 checksum.",
                                       CHECKSUM_URL)]
        return res
    fx = None if live else fixture(ico)
    res["demo_fixture"], res["live"] = fx is not None, fx is None
    if fx is None and not datasets.enabled():
        res["skipped"].append("datasets: no MOSTLYRIGHT_API_KEY (TODO(credential))")

    client = httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)
    pool = ThreadPoolExecutor(max_workers=4)
    futs = {name: pool.submit(fn) for name, fn in _fetchers(ico, fx, client).items()}
    done, _ = wait(futs.values(), timeout=TIMEOUT_S)
    pool.shutdown(wait=False, cancel_futures=True)
    if len(done) == len(futs):
        client.close()  # otherwise a late thread still holds it; it is dropped with the thread

    raw, reasons = {}, []
    for name, f in futs.items():
        if f not in done:
            reasons.append(_registry_fail(name, None))
        elif f.exception() is not None:
            reasons.append(_registry_fail(name, f.exception()))
        else:
            raw[name] = f.result()
    res["sources"] = [ares.url(ico), vat.SOURCE_URL, isir.SOURCE_URL, sanctions.XML_URL]

    a = None
    if "ares" in raw:
        try:
            a = ares.parse(raw["ares"])
        except Exception as e:  # noqa: BLE001  — malformed answer: fail closed, never "ok"
            reasons.append(_registry_fail("ares", e))
    if a is not None:
        res["legal_form"] = a.get("legal_form")
        if not a["found"] or not a["legal_entity"]:
            # Not found, or not a legal entity: keep nothing else (a sole trader's data is personal data).
            res["reasons"] = ares.reasons(a, ico)
            res["sources"] = [ares.url(ico)]
            res["registry"] = {"found": a["found"], "legal_entity": bool(a.get("legal_entity"))}
            return res
        res["name"] = a["name"]
        res["registry"] = {k: a.get(k) for k in ("found", "legal_entity", "name", "address", "street", "psc",
                                                  "founded", "dissolved")}
        reasons += ares.reasons(a, ico)

    v = None
    if "vat" in raw:
        try:
            v = vat.parse(raw["vat"])
            reasons += vat.reasons(v)
        except Exception as e:  # noqa: BLE001
            reasons.append(_registry_fail("vat", e))
    ares_vat = a["vat_registered"] if a else None
    if v is not None:
        res["vat"] = {"payer": bool(ares_vat) or v["unreliable"] in ("ANO", "NE"), "unreliable": v["unreliable"],
                      "published_accounts": v["published_accounts"], "source_url": vat.SOURCE_URL}
    elif ares_vat:
        res["vat"]["payer"] = True  # §6.5: ARES stavZdrojeDph alone makes it a payer; accounts unknown

    if "isir" in raw:
        try:
            i = isir.parse(raw["isir"])
            res["isir"] = i["records"]
            reasons += isir.reasons(i)
        except Exception as e:  # noqa: BLE001
            reasons.append(_registry_fail("isir", e))

    if "datasets" in raw:
        reasons += datasets.reasons(raw["datasets"])
        res["sources"].append(datasets.SOURCE_URL)

    s = sanctions.screen(res["name"], ico, a.get("dic") if a else None)
    res["sanctions"] = s["matches"]
    reasons += s["reasons"]

    out = None
    if fx is not None:
        out = fx.get("company_check")
    elif website_url and config.APIFY_TOKEN:
        try:
            out = website.run_actor(ico, website_url, res["name"], city=website.city_of(
                (res.get("registry") or {}).get("address")))
        except Exception as e:  # noqa: BLE001
            reasons.append(codes.reason("website_check_failed", False, f"company-check actor failed: {str(e)[:200]}",
                                        website_url))
    elif website_url:
        res["skipped"].append("company_check: no APIFY_TOKEN (TODO(credential))")
    if out is not None:
        res["website"] = {"ico": ico, "level": out.get("level"), "score": out.get("score"),
                          "checkedAt": out.get("checkedAt"), "signals": out.get("signals", []),
                          "run": out.get("_run") or ({"origin": "recorded"} if fx is not None else None)}
        reasons += website.verdict(out)
        if out.get("websiteUrl"):
            res["sources"].append(out["websiteUrl"])

    res["reasons"] = reasons
    return res


def check(ico: str, wallet: str | None = None, website_url: str | None = None, live: bool = False) -> dict:
    """Full check, writes the cache. live=True ignores recorded fixtures (real registries, forms 112/121 only)."""
    ico = (ico or "").strip()
    with timing.timed("screening"):
        res = _check(ico, wallet, website_url, live)
    for r in res["reasons"]:
        if r.get("source_url") and r["source_url"] not in res["sources"]:
            res["sources"].append(r["source_url"])
    res["level"] = level_of(res["reasons"])
    db.run("INSERT INTO screenings (subject, source, result_json, fetched_at) VALUES (?,?,?,?)",
           (ico, "check", db.dumps(res), res["fetched_at"]))
    journal.append("bank", "screening.checked",
                   {"ico": ico, "level": res["level"], "reasons": [r["code"] for r in res["reasons"]],
                    "demo_fixture": res["demo_fixture"], "live": res["live"]})
    return res


def cached(ico: str) -> dict | None:
    """Money path: the latest cached check within its TTL, or None. Never touches the network."""
    row = db.one("SELECT result_json, fetched_at FROM screenings WHERE subject=? AND source='check'"
                 " ORDER BY id DESC LIMIT 1", ((ico or "").strip(),))
    if not row:
        return None
    res = db.loads(row["result_json"])
    ttl = TTL_FAIL_S if any(r["code"] in FAIL_CLOSED for r in res["reasons"]) else TTL_S
    return res if time.time() - row["fetched_at"] <= ttl else None


def prefetch(ico: str, wallet: str | None = None, website_url: str | None = None) -> None:
    """Start a background check during negotiation (one at a time per IČO); errors go to the journal."""
    ico = (ico or "").strip()
    with _lock:
        if ico in _inflight:
            return
        _inflight.add(ico)

    def run():
        try:
            check(ico, wallet, website_url)
        except Exception as e:  # noqa: BLE001
            journal.append("bank", "screening.error", {"ico": ico, "error": str(e)[:300]})
        finally:
            with _lock:
                _inflight.discard(ico)

    threading.Thread(target=run, name=f"prefetch-{ico}", daemon=True).start()


def public_view(res: dict) -> dict:
    """{level, public_reasons} for the client and its agent (§6.7): any non-public reason -> neutral answer only."""
    if any(r.get("public") is not True for r in res["reasons"]):
        return {"level": "review", "public_reasons": [], "explanation": codes.NEUTRAL_CLIENT_TEXT}
    return {"level": res["level"], "public_reasons": [{k: r.get(k) for k in codes.CLIENT_REASON_FIELDS}
                                                      for r in res["reasons"]], "explanation": ""}
