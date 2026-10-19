# Czech company web footprint check (company-check)

**company-check** answers one question a bank or a buyer asks before paying a new Czech supplier:
*does the company's public web footprint match the company in the business register?* Give it an IČO
(Czech company ID) and the company's website. It reads three pages of the site, looks for the IČO the law
requires there, and checks the company's Google Maps place. It returns a risk level (`low`, `medium`,
`high`) and a list of signals, each with its source URL and a one-line explanation.

It was built for the bank in our FinTechers Hackathon 2026 project (two AI agents close a B2B deal and the
bank checks the seller before money moves). The bank checks the official registries itself (ARES, the VAT
payer register, the insolvency register ISIR, EU sanctions). This Actor adds what a registry cannot know.

## What it checks

| Signal | How |
|---|---|
| `website_reachable` | The home page answers with HTTP status below 400 |
| `website_mentions_ico` | The IČO appears next to an `IČ` / `IČO` label on the home page, `/kontakt` or `/obchodni-podminky` (spaces inside the number are allowed). Czech Civil Code §435 requires a business to show it |
| `website_other_icos` | Other checksum-valid IČO numbers on those pages: a site that shows somebody else's IČO is a strong warning |
| `maps_listing_found`, `maps_rating`, `maps_reviews`, `maps_permanently_closed` | One capped run of [compass/crawler-google-places](https://apify.com/compass/crawler-google-places) for the company name and city |

Scoring: unreachable site +50, different IČO +60, missing IČO +30, place permanently closed +60, no place
+20. Level `high` from 60 points, `medium` from 20, otherwise `low`. A wrong IČO checksum is an input error.

## How to use

1. Open the Actor in Apify Console, enter the IČO and the website URL, and click **Start**.
2. Or call it from code or an AI agent (Apify MCP server, `tools=<user>/company-check`).
3. Read the dataset item, or the `OUTPUT` record of the default key-value store.

### Input example

```json
{"ico": "45274649", "websiteUrl": "https://www.cez.cz", "checkMaps": true, "companyName": "ČEZ", "city": "Praha"}
```

Optional fields all have defaults (`checkMaps` true, `companyName` = website domain, `city` Brno, `maxPlaces` 3),
so API and MCP calls work with only `ico` and `websiteUrl`.

### Output example

```json
{"ico": "30181020", "websiteUrl": "https://beta-data.example", "score": 0, "level": "low",
 "signals": [
  {"name": "website_reachable", "value": true, "source_url": "https://beta-data.example", "explanation": "Home page answered HTTP 200."},
  {"name": "website_mentions_ico", "value": true, "source_url": "https://beta-data.example/kontakt", "explanation": "IČO 30181020 found on https://beta-data.example/kontakt (Civil Code §435 requires it on the website)."},
  {"name": "website_other_icos", "value": [], "source_url": "https://beta-data.example", "explanation": "No other IČO on the site."},
  {"name": "maps_listing_found", "value": true, "source_url": "https://www.google.com/maps/place/?q=place_id:…", "explanation": "A Google Maps place exists."}
 ],
 "checkedAt": "2026-10-19T12:00:00Z"}
```

The dataset has two views: **Overview** (one row per company) and **Signals** (`unwind` of `signals`, one row
per signal with its source link).

## Pricing

The website part costs only Actor compute (a few seconds of the smallest memory). The Google Maps part runs
compass/crawler-google-places for at most `maxPlaces` places (about $0.004 per place, capped by
`max_total_charge_usd` 0.50). Turn it off with `checkMaps: false`.

## Safety and data

- **Website content is untrusted data.** Pages are only matched with regular expressions; nothing on a
  page is interpreted as an instruction, and no page text is copied into the output (only IČO numbers that
  pass the checksum). Our evals include a site with a prompt-injection text; the result does not change.
- **No personal data:** no phone numbers, no reviews (`scrapeReviewsPersonalData: false`, `maxReviews: 0`),
  only business fields of the place. Logs go through `Actor.log` only.
- Use it for legal entities; a sole trader's register entry is a person's name.

## FAQ

**Why a medium level for a big company?** Some large sites render the footer with JavaScript or keep the
IČO on another page (for example `cez.cz` has no `/kontakt`). Medium means "look at it", not "fraud".

**Does it check the registries?** No. The calling bank checks ARES, VAT, ISIR and sanctions itself.

**Can I run it locally?** Yes: `pip install -r requirements.txt`, put `INPUT.json` into
`storage/key_value_stores/default/`, then `python -m my_actor`.

## Develop, validate, push

No `APIFY_TOKEN` was available during the build, so the Actor has not been pushed yet. With a token:

```bash
npm install -g apify-cli          # apify-cli 1.10+
apify login                       # paste the APIFY_TOKEN
cd actors/company-check
apify validate-schema             # before every push
apify push                        # builds on the platform
apify call company-check -i '{"ico": "45274649", "websiteUrl": "https://www.cez.cz", "checkMaps": false}'
```

Then set **Limited permissions** in the Actor settings (with full permissions the Actor is not visible to the
MCP server) and put `APIFY_COMPANY_CHECK_ACTOR=<username>/company-check` into the bank's `.env`.
Tests: `pytest tests/evals/test_company_check.py` from the repository root (8 eval cases, mocked HTTP).
