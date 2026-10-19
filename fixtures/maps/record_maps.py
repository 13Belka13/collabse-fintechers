# Record the Google Maps warehouse dataset once (SPEC §10.2) and reuse it in every demo run.
# Usage (needs APIFY_TOKEN in .env; ~$0.20 for 40 places):  python fixtures/maps/record_maps.py
# Writes fixtures/maps/brno_warehouses.json keeping only the fields the verifier uses; never phone fields.
# The "gama" variant (2 fakes) is rebuilt from the first 20 places the same way as the synthetic file.
import copy
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apify_client import ApifyClient  # noqa: E402

from bank import config  # noqa: E402
from bank.verifier import KEEP_FIELDS  # noqa: E402

RUN_INPUT = {"searchStringsArray": ["sklad", "skladové prostory"], "locationQuery": "Brno, Czechia",
             "language": "cs", "countryCode": "cz", "maxCrawledPlacesPerSearch": 40, "skipClosedPlaces": True,
             "scrapeContacts": False, "scrapePlaceDetailPage": False, "maxReviews": 0, "maxImages": 0,
             "scrapeReviewsPersonalData": False, "maximumLeadsEnrichmentRecords": 0}


def main():
    if not config.APIFY_TOKEN:
        sys.exit("APIFY_TOKEN is empty (TODO(credential))")
    client = ApifyClient(config.APIFY_TOKEN)
    run = client.actor("compass/crawler-google-places").call(
        run_input=RUN_INPUT, run_timeout=timedelta(minutes=5), max_total_charge_usd=Decimal("0.50"))
    raw = client.dataset(run.default_dataset_id).list_items(fields=list(KEEP_FIELDS)).items
    items = [{k: it.get(k) for k in KEEP_FIELDS} for it in raw if it.get("website")][:20]
    gama = copy.deepcopy(items)
    gama[18] = {**copy.deepcopy(items[2]), "title": f"{items[2]['title']} II"}  # duplicate placeId
    gama[19] = {**copy.deepcopy(items[19]), "city": "Praha", "postalCode": "158 00",
                "address": "Bucharova 1281/2, 158 00 Praha 5"}                     # outside Brno
    out = {"_note": f"Recorded compass/crawler-google-places run {run.id}; fields limited to KEEP_FIELDS, "
                    "no contact numbers, no reviews. 'gama' = same 20 with 2 fakes (#19 duplicate placeId of #3, "
                    "#20 moved to Praha).",
           "demo_fixture": True, "recorded": True, "query": RUN_INPUT, "items": items, "gama": gama}
    path = ROOT / "fixtures" / "maps" / "brno_warehouses.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(items)} places -> {path}")


if __name__ == "__main__":
    main()
