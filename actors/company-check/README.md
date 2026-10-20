# Czech company web footprint check (company-check)

> **Developer reference** for the Apify Actor. The project it belongs to is described in its [README](../../README.md).

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

## On the Apify platform

Pushed on 2026-10-19 to the team account as **`savvy_sauger/company-check`** (Actor id `LBLUo7ixHlcyIjNSU`,
build 0.1.1, private): <https://console.apify.com/actors/LBLUo7ixHlcyIjNSU>. It runs under **Limited
permissions** (`actorPermissionLevel: LIMITED_PERMISSIONS`, so the Apify MCP server can call it), and
`actor.json` carries `meta.generatedBy`. Default run options: 512 MB, timeout 300 s, build `latest` (now 0.1.2).

Live runs (2026-10-19, 512 MB):

| Input | Run | Result | Cost |
| --- | --- | --- | --- |
| ČEZ, IČO 45274649, `https://www.cez.cz`, Maps in Praha, 3 places | [BZXkhNLHWn1GdqFbE](https://console.apify.com/actors/LBLUo7ixHlcyIjNSU/runs/BZXkhNLHWn1GdqFbE) | `medium` (30): site reachable, IČO not on the home page, `/kontakt` (404) or `/obchodni-podminky`; Maps place found, open, 3.9 | $0.0002 + $0.0122 (child crawler-google-places run Vp1xOhdg7vnhD0NXp, 3 places) |
| Synthetic Gama s.r.o., IČO 59093994, `https://gama-sklad.invalid`, no Maps | [VMWtKXHYycP9leChx](https://console.apify.com/actors/LBLUo7ixHlcyIjNSU/runs/VMWtKXHYycP9leChx) | run SUCCEEDED, `medium` (50): `website_reachable=false` (ConnectError) | $0.0002 |
| O2 Czech Republic a.s., IČO 60193336, `https://www.o2.cz`, Maps in Praha, 1 place (called by the bank, 2026-10-20) | [gdFGBxG9ZmZ5MT9Lt](https://console.apify.com/view/runs/gdFGBxG9ZmZ5MT9Lt) | `low` (0): IČO on the home page, no other IČO; Maps place found, open, ★2.7 (507 reviews, count only); 36 s end to end | $0.0009 + $0.0042 (child run eGQkeSDW39sA51zY0, 1 place) |

cez.cz shows its IČO on a page other than the three the Actor reads, and the first Maps hit for "ČEZ" in Praha
is a ČEZ charging station. Pass `companyName` and `city` for a sharper Maps match.

The Brno warehouse dataset the seller delivers was recorded with the same account
(`fixtures/maps/record_maps.py`, compass/crawler-google-places run w9XlXOBtETIUObLGm, $0.37, 74 places);
see `fixtures/maps/brno_warehouses_recorded.json`. Total Apify spend for the push, the two runs and the
recording: about **$0.38** (cycle to 2026-10-19). The account is on the FREE plan: $5 of platform credit per
monthly cycle, hard usage cap $105; the cycle from 2026-10-20 shows $0.053 after the O2 check and one live
Google Maps run of the demo console (DyUodCZn9bdCVmRDZ, 12 places in Praha, $0.048).

## How the bank uses the answer

`bank/screening/website.py` runs the Actor during negotiation only (never on the money path), with
`companyName` and `city` from ARES and `maxPlaces: 1`, keeps the run id, duration and cost, and caches the
answer a day. Signals map to reasons: another IČO on the site or a permanently closed place blocks the seller;
a missing IČO or an unreachable site sends it to review. The level itself counts too, fail-closed: `high`
without a mapped signal is `website_risk_high` (review), and an answer without a known level or without the
website signal is `website_check_failed` (review, never clean). The deal page and the story show one line:
where the IČO was found, the Maps place, the level and a link to the run.

## Develop, validate, push

```bash
npm install -g apify-cli@1.10.0   # or prefix each command with npx apify-cli@1.10.0
apify login                       # paste the APIFY_TOKEN (stored in ~/.apify/auth.json, never in git)
cd actors/company-check
apify validate-schema             # before every push
apify push                        # builds on the platform
apify call savvy_sauger/company-check -i '{"ico": "45274649", "websiteUrl": "https://www.cez.cz", "checkMaps": false}'
```

The bank calls it with `APIFY_COMPANY_CHECK_ACTOR=savvy_sauger/company-check` (the default in
`bank/config.py`). Tests: `pytest tests/evals/test_company_check.py` from the repository root (14 eval cases,
mocked HTTP) and `tests/unit/test_company_check_bank.py` (the bank side).
