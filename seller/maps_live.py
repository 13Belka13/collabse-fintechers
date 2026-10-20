# Live goods for the demo console (data source "apify"): the seller answers a task like "10 warehouses in Praha
# with rating above 4.5 and a website" with real Google Maps places from one capped run of
# compass/crawler-google-places (SPEC §10.2). Caps: maxCrawledPlacesPerSearch = min(3·N, 60), no reviews, no
# contacts, no images, scrapeReviewsPersonalData false, 120 s (60 places ≈ 0,25 $). Only KEEP_FIELDS are requested
# from the dataset API, so phone numbers never reach this machine. One run per normalized query (city + cap),
# cached a day in data/maps_live/; any error or timeout falls back to the recorded Brno dataset.
# The run starts in the background when the seller first hears the task (negotiation), delivery waits for it.
import hashlib
import json
import re
import threading
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from bank import config

ACTOR = "compass/crawler-google-places"
KEEP_FIELDS = ("title", "categoryName", "totalScore", "reviewsCount", "address", "city", "postalCode", "website",
               "placeId", "url", "permanentlyClosed")
MAX_PLACES = 60
TIMEOUT_S = 120
MAX_CHARGE_USD = Decimal("0.50")  # the platform minimum for a pay-per-event actor; the place cap bounds it lower
CACHE_TTL_S = 24 * 3600
SEARCH = "sklad"  # the category the demo sells: warehouses (Czech search term)
RECORDED = config.ROOT / "fixtures" / "maps" / "brno_warehouses_recorded.json"
FIRST_NAMES = {"jan", "jiří", "petr", "josef", "pavel", "martin", "tomáš", "jaroslav", "miroslav", "zdeněk", "václav",
               "michal", "františek", "karel", "milan", "lukáš", "david", "jakub", "ondřej", "vladimír", "roman",
               "marie", "jana", "eva", "hana", "anna", "lenka", "kateřina", "lucie", "věra", "alena", "petra"}

POSTCODE_CITY = {"6": "Brno", "1": "Praha"}  # bank/deals.py POSTCODE_PREFIX the other way round
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="maps-live")
_inflight: dict[str, Future] = {}
_lock = threading.Lock()
STATS = {"live": 0, "cached": 0, "recorded": 0}  # where each fetch was answered from (cache hit rate in the report)


def query_of(count: int, predicates: list[dict] | None) -> dict:
    """The buyer's confirmed predicates -> {city, min_rating, count, cap}: city from `eq city` or from the postcode
    regex (6xx xx Brno, 1xx xx Praha), rating from `range totalScore gt|gte`."""
    city, rating = "Brno", None
    for p in predicates or []:
        if p.get("op") == "eq" and p.get("field") == "city" and isinstance(p.get("value"), str):
            city = p["value"]
        elif p.get("op") == "regex" and p.get("field") == "postalCode":
            city = POSTCODE_CITY.get(str(p.get("pattern"))[1:2], city)
        elif p.get("op") == "range" and p.get("field") == "totalScore":
            rating = p.get("gt", p.get("gte"))
    count = max(1, min(int(count or 1), 100))
    return {"city": " ".join(str(city).split())[:80] or "Brno", "min_rating": rating, "count": count,
            "cap": min(3 * count, MAX_PLACES)}


def key_of(q: dict) -> str:
    """Normalized query: lower-case city without diacritics + the place cap (rating is filtered locally)."""
    city = unicodedata.normalize("NFKD", q["city"]).encode("ascii", "ignore").decode().lower().strip()
    return f"{re.sub(r'[^a-z0-9]+', '-', city)}-{q['cap']}"


def run_input(q: dict) -> dict:
    return {"searchStringsArray": [SEARCH], "locationQuery": f"{q['city']}, Czechia", "language": "cs",
            "countryCode": "cz", "maxCrawledPlacesPerSearch": q["cap"], "skipClosedPlaces": False,
            "scrapeContacts": False, "scrapePlaceDetailPage": False, "maxReviews": 0, "maxImages": 0,
            "scrapeReviewsPersonalData": False, "maximumLeadsEnrichmentRecords": 0}


def clean(raw: list[dict]) -> list[dict]:
    """KEEP_FIELDS only (whatever the dataset sent), first copy of each placeId, no place named after a person."""
    out, seen = [], set()
    for it in raw:
        if not isinstance(it, dict) or not it.get("placeId") or it["placeId"] in seen:
            continue
        words = str(it.get("title") or "").split()
        if len(words) == 2 and words[0].lower() in FIRST_NAMES:  # "Petr Novák": a sole trader, personal data
            continue
        seen.add(it["placeId"])
        out.append({k: it.get(k) for k in KEEP_FIELDS})
    return out


def _cache_path(key: str):
    """Next to the bank's SQLite (data/ by default, gitignored; a temp dir in tests)."""
    from pathlib import Path
    return Path(config.DB_PATH).parent / "maps_live" / f"{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"


def _recorded(error: str) -> dict:
    data = json.loads(RECORDED.read_text(encoding="utf-8"))
    return {"items": data["items"], "source": "recorded", "error": error,
            "run": {"id": data["run_id"], "url": f"https://console.apify.com/view/runs/{data['run_id']}"}}


def _run(q: dict, client=None) -> dict:
    """One capped live run -> {items, source: live, run}; raises on anything unusable."""
    if client is None:
        from apify_client import ApifyClient
        client = ApifyClient(config.APIFY_TOKEN)
    t0 = time.time()
    run = client.actor(ACTOR).call(run_input=run_input(q), run_timeout=timedelta(seconds=TIMEOUT_S),
                                   wait_duration=timedelta(seconds=TIMEOUT_S + 15),
                                   max_total_charge_usd=MAX_CHARGE_USD, logger=None)  # no actor log in ours
    if run is None or run.status not in ("SUCCEEDED", "TIMED-OUT"):  # a timed-out run keeps what it found
        raise RuntimeError(f"run {getattr(run, 'id', None)} ended {getattr(run, 'status', 'unknown')}")
    items = clean(client.dataset(run.default_dataset_id).list_items(fields=list(KEEP_FIELDS)).items)
    if not items:
        raise RuntimeError(f"run {run.id} returned no places")
    run = client.run(run.id).get() or run  # the per-event charges are final only after the run ended
    return {"items": items, "source": "live", "error": None,
            "run": {"id": run.id, "url": f"https://console.apify.com/view/runs/{run.id}", "status": run.status,
                    "duration_s": round(time.time() - t0, 1), "cost_usd": float(run.usage_total_usd or 0),
                    "places": len(items), "query": run_input(q), "fetched_at": time.time()}}


def fetch(q: dict, client=None) -> dict:
    """Cache (a day) -> live run -> recorded dataset. Never raises."""
    path = _cache_path(key_of(q))
    try:
        hit = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - hit["run"]["fetched_at"] <= CACHE_TTL_S:
            STATS["cached"] += 1
            return {**hit, "source": "cached"}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not config.APIFY_TOKEN and client is None:
        STATS["recorded"] += 1
        return _recorded("no APIFY_TOKEN (TODO(credential))")
    try:
        out = _run(q, client)
    except Exception as e:  # noqa: BLE001 — the seller still delivers: the recorded places, checked the same way
        STATS["recorded"] += 1
        return _recorded(f"{type(e).__name__}: {str(e)[:200]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    STATS["live"] += 1
    return out


def prefetch(q: dict, client=None) -> Future:
    """Start (or join) the run for this query in the background; one run per normalized query at a time."""
    key = key_of(q)
    with _lock:
        f = _inflight.get(key)
        if f is None or f.done():
            f = _inflight[key] = _pool.submit(fetch, q, client)
        return f


def places(count: int, predicates: list[dict] | None, client=None) -> tuple[list[dict], dict]:
    """Delivery: up to `count` places, those that pass the buyer's own predicates first (an honest seller sorts by
    the public terms; the bank still checks every item). Waits for the background run at most TIMEOUT_S + 30 s."""
    from bank import deals, verifier
    q = query_of(count, predicates)
    try:
        got = prefetch(q, client).result(timeout=TIMEOUT_S + 30)
    except Exception as e:  # noqa: BLE001
        got = _recorded(f"{type(e).__name__}")
    items = got["items"]
    c = deals.buyer_criteria({"criteria": {"count": len(items) or 1, "city": q["city"], "registry_check": False,
                                           "min_rating_exclusive": q["min_rating"] or 0}})
    res = verifier.evaluate(items, verifier.criteria_predicates(c), ordered=len(items)) if items else []
    good = [it for it, r in zip(items, res) if r["status"] == "accepted"]
    rest = [it for it, r in zip(items, res) if r["status"] != "accepted"]
    meta = {k: got.get(k) for k in ("source", "error", "run")}
    meta.update(returned=len(items), passing=len(good))
    return [dict(it) for it in (good + rest)[:count]], meta
