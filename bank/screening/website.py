# Web footprint from the Apify actor company-check (SPEC §10.1, actors/company-check/): IČO on the website
# and the Google Maps place. Registries are checked by the bank itself; the actor only adds what a registry
# cannot know. Its output is data about an untrusted site, so only typed signal values are read here.
# A live run is cached on disk for a day (data/company_check/): the demo re-runs the same seller many times.
import hashlib
import json
import re
import time
from pathlib import Path

from bank import codes, config

ACTOR_TIMEOUT_S = 120
CACHE_TTL_S = 24 * 3600
LEVELS = ("low", "medium", "high")
RUN_URL = "https://console.apify.com/view/runs/{}"


def _cache_path(ico: str, website_url: str):
    key = hashlib.sha256(f"{ico}|{website_url}".encode()).hexdigest()[:16]
    return Path(config.DB_PATH).parent / "company_check" / f"{ico}-{key}.json"  # data/ (gitignored)


def city_of(address: str | None) -> str:
    """'Duhová 1444/2, Michle, 14000 Praha 4' -> 'Praha' (the Maps search city); Brno when unknown."""
    m = re.search(r"\b\d{3} ?\d{2}\s+([^\d,]+)", address or "")
    return m.group(1).strip() if m else "Brno"


def run_actor(ico: str, website_url: str, name: str | None = None, city: str = "Brno", client=None) -> dict:
    """Live: call our actor on Apify (tens of seconds -> prefetch only, result cached a day). The returned item
    carries `_run` = {id, url, status, duration_s, cost_usd, origin} from the run object (ours, not the actor's)."""
    path = _cache_path(ico, website_url)
    try:
        hit = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - hit["_run"]["fetched_at"] <= CACHE_TTL_S:
            return {**hit, "_run": {**hit["_run"], "origin": "cached"}}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    from datetime import timedelta
    from decimal import Decimal

    if client is None:
        from apify_client import ApifyClient
        client = ApifyClient(config.APIFY_TOKEN)
    t0 = time.time()
    run = client.actor(config.APIFY_COMPANY_CHECK_ACTOR).call(
        run_input={"ico": ico, "websiteUrl": website_url, "checkMaps": True, "companyName": name or "",
                   "city": city, "maxPlaces": 1},
        run_timeout=timedelta(seconds=ACTOR_TIMEOUT_S), max_total_charge_usd=Decimal("0.10"), logger=None)
    if run is None or run.status != "SUCCEEDED":
        raise RuntimeError(f"company-check run {getattr(run, 'id', None)} ended {getattr(run, 'status', 'unknown')}")
    items = client.dataset(run.default_dataset_id).list_items().items
    if not items:
        raise RuntimeError(f"company-check run {run.id} returned no item")
    run = client.run(run.id).get() or run  # final usage (the child Google Maps run is billed on its own)
    out = dict(items[0])
    out["_run"] = {"id": run.id, "url": RUN_URL.format(run.id), "status": run.status,
                   "duration_s": round(time.time() - t0, 1), "cost_usd": float(run.usage_total_usd or 0),
                   "fetched_at": time.time(), "origin": "live"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    return out


def _signals(out: dict) -> dict:
    return {s["name"]: s for s in out.get("signals", []) if isinstance(s, dict) and "name" in s}


def reasons(out: dict) -> list[dict]:
    sig = _signals(out)
    site = out.get("websiteUrl")

    def value(name):
        return sig.get(name, {}).get("value")

    def src(name):
        return sig.get(name, {}).get("source_url") or site

    res = []
    if value("website_reachable") is False:
        res.append(codes.reason("website_unreachable", True, "The company website did not respond.", site))
    elif value("website_mentions_ico") is False:
        others = [x for x in (value("website_other_icos") or []) if isinstance(x, str)]
        if others:
            res.append(codes.reason("ico_mismatch_on_website", True,
                                    f"The website shows IČO {', '.join(others)}, not {out.get('ico')} (ARES).",
                                    src("website_other_icos"), website_icos=others, ares_ico=out.get("ico")))
        else:
            res.append(codes.reason("ico_missing_on_website", True,
                                    "The website does not show the company's IČO (Civil Code §435 requires it).",
                                    src("website_mentions_ico")))
    if value("maps_permanently_closed") is True:
        res.append(codes.reason("maps_permanently_closed", True, "Google Maps marks the place permanently closed.",
                                src("maps_permanently_closed")))
    return res


def verdict(out: dict) -> list[dict]:
    """reasons() plus the actor's own risk level, fail-closed: an answer without a known level or without the
    website signal is no answer (review, never "clean"); level "high" puts the seller under review even when no
    single signal maps to a reason (e.g. no site and no Maps place)."""
    res = reasons(out)
    level, sig = out.get("level"), _signals(out)
    if level not in LEVELS or "website_reachable" not in sig:
        return res + [codes.reason("website_check_failed", False, "company-check gave no usable answer "
                                   f"(level {str(level)[:20]!r}).", out.get("websiteUrl"))]
    if level == "high" and not res:
        res.append(codes.reason("website_risk_high", True, f"company-check rates the web footprint high risk "
                                f"(score {out.get('score')}).", out.get("websiteUrl"), score=out.get("score")))
    return res


def summary(web: dict | None) -> dict | None:
    """One line for the deal page and /story from the stored screening["website"] (typed values only)."""
    if not web:
        return None
    sig = {s.get("name"): s for s in web.get("signals") or [] if isinstance(s, dict)}

    def v(name):
        return (sig.get(name) or {}).get("value")
    parts = []
    if v("website_reachable") is False:
        parts.append("website did not respond")
    elif v("website_mentions_ico") is True:
        page = str((sig["website_mentions_ico"].get("source_url") or "")).split("//", 1)[-1]
        parts.append(f"IČO {web.get('ico') or ''} found on {page}".replace("  ", " "))
    elif v("website_mentions_ico") is False:
        others = [x for x in v("website_other_icos") or [] if isinstance(x, str)]
        parts.append(f"website shows IČO {', '.join(others)} instead" if others else "IČO not on the website")
    if v("maps_listing_found") is True:
        rating = v("maps_rating")
        place = "Google Maps place" + (f" ★{rating}" if isinstance(rating, (int, float)) else "")
        parts.append(place + (", permanently closed" if v("maps_permanently_closed") is True else ", open"))
    elif v("maps_listing_found") is False:
        parts.append("no Google Maps place")
    run = web.get("run") or {}
    origin = run.get("origin") or ("recorded" if web.get("demo_fixture") else None)
    return {"text": "; ".join(parts) or "no signals", "level": web.get("level") or "unknown",
            "score": web.get("score"), "run_id": run.get("id"), "run_url": run.get("url"), "origin": origin,
            "checked_at": web.get("checkedAt"), "cost_usd": run.get("cost_usd")}


def latest(ico: str | None) -> dict | None:
    """summary() of the seller's latest stored check (display only, any age) + `hidden`: the check carries a
    non-public reason, so pages the client sees show nothing beyond the neutral decision (§6.7, no tipping-off)."""
    from bank import db
    row = db.one("SELECT result_json FROM screenings WHERE subject=? AND source='check' ORDER BY id DESC LIMIT 1",
                 ((ico or "").strip(),))
    if row is None:
        return None
    res = db.loads(row["result_json"])
    s = summary(res.get("website"))
    return s and {**s, "hidden": any(r.get("public") is not True for r in res.get("reasons") or [])}
