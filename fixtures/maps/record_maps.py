# Record the Google Maps warehouse dataset once (SPEC §10.2) and reuse it in every demo run.
# Usage (needs APIFY_TOKEN in .env; ~$0.004 per place, capped at $0.50):  python fixtures/maps/record_maps.py
# Writes fixtures/maps/brno_warehouses_recorded.json; the synthetic brno_warehouses.json stays as the offline seed.
# Only KEEP_FIELDS are requested from the dataset API, so contact numbers and reviews never reach this machine.
# "passing" = placeIds that meet the default demo criteria (Brno or 6xx xx, rating > 4.0, website, open,
# category keyword); a place titled with a person's name (a sole trader, "Petr Novák") is dropped as personal data;
# "gama" = the first 20 passing places with 2 fakes (#19 duplicate placeId of #3, #20 in Praha).
import copy
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apify_client import ApifyClient  # noqa: E402

from bank import config  # noqa: E402
from bank import deals, verifier  # noqa: E402

KEEP_FIELDS = ("title", "categoryName", "totalScore", "reviewsCount", "address", "city", "postalCode", "website",
               "placeId", "url", "permanentlyClosed")

ACTOR = "compass/crawler-google-places"
OUT = ROOT / "fixtures" / "maps" / "brno_warehouses_recorded.json"
RUN_INPUT = {"searchStringsArray": ["sklad", "skladové prostory"], "locationQuery": "Brno, Czechia",
             "language": "cs", "countryCode": "cz", "maxCrawledPlacesPerSearch": 40, "skipClosedPlaces": True,
             "scrapeContacts": False, "scrapePlaceDetailPage": False, "maxReviews": 0, "maxImages": 0,
             "scrapeReviewsPersonalData": False, "maximumLeadsEnrichmentRecords": 0}
FIRST_NAMES = {"jan", "jiří", "petr", "josef", "pavel", "martin", "tomáš", "jaroslav", "miroslav", "zdeněk", "václav",
               "michal", "františek", "karel", "milan", "lukáš", "david", "jakub", "ondřej", "vladimír", "roman",
               "marie", "jana", "eva", "hana", "anna", "lenka", "kateřina", "lucie", "věra", "alena", "petra"}


def person_named(title: str | None) -> bool:
    """'Petr Zimin' (first name + surname, nothing else) is a natural person, not a business listing."""
    words = str(title or "").split()
    return len(words) == 2 and words[0].lower() in FIRST_NAMES


def clean(raw: list[dict]) -> list[dict]:
    """KEEP_FIELDS only, first copy of each placeId (the two search strings overlap), no person-named places."""
    items, seen = [], set()
    for it in raw:
        if it.get("placeId") and it["placeId"] not in seen and not person_named(it.get("title")):
            seen.add(it["placeId"])
            items.append({k: it.get(k) for k in KEEP_FIELDS})
    return items


def passing(items: list[dict]) -> list[dict]:
    """Items that pass every code rule and carry a category keyword (no over_quantity cap here)."""
    c = deals.buyer_criteria({"criteria": {"count": len(items), "registry_check": False}})
    res = verifier.evaluate(items, verifier.criteria_predicates(c), ordered=len(items))
    return [it for it, r in zip(items, res) if r["status"] == "accepted"]


def main():
    if not config.APIFY_TOKEN:
        sys.exit("APIFY_TOKEN is empty (TODO(credential))")
    client = ApifyClient(config.APIFY_TOKEN)
    run = client.actor(ACTOR).call(
        run_input=RUN_INPUT, run_timeout=timedelta(minutes=5), max_total_charge_usd=Decimal("0.50"))
    run = client.run(run.id).get()
    raw = client.dataset(run.default_dataset_id).list_items(fields=list(KEEP_FIELDS)).items
    items = clean(raw)
    good = passing(items)
    gama = None
    if len(good) >= 20:
        gama = copy.deepcopy(good[:20])
        gama[18] = {**copy.deepcopy(good[2]), "title": f"{good[2]['title']} II"}  # duplicate placeId
        gama[19] = {**copy.deepcopy(good[19]), "city": "Praha", "postalCode": "158 00",
                    "address": "Bucharova 1281/2, 158 00 Praha 5"}               # outside Brno
    out = {"_note": f"Recorded {ACTOR} run {run.id}; fields limited to KEEP_FIELDS (SPEC §10.2), no contact "
                    "numbers, no reviews, no personal data. Public business listings only.",
           "demo_fixture": True, "recorded": True, "actor": ACTOR, "run_id": run.id,
           "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "cost_usd": round(float(run.usage_total_usd or 0), 4), "query": RUN_INPUT,
           "raw_count": len(raw), "count": len(items), "passing_count": len(good),
           "passing": [it["placeId"] for it in good], "items": items, "gama": gama}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"run {run.id}: {len(raw)} rows, {len(items)} unique places, {len(good)} pass the demo criteria, "
          f"${out['cost_usd']} -> {OUT}")


if __name__ == "__main__":
    main()
