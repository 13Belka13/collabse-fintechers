"""Pure checks of the company-check Actor (no Apify imports, so evals run them with mocked HTTP).

Signals (SPEC §10.1): website_reachable, website_mentions_ico, website_other_icos (home + /kontakt +
/obchodni-podminky, regex for IČ/IČO with spaces inside the number), maps_* from a Google Maps place.
Website content is untrusted data: it is only matched with regular expressions, never interpreted,
and no page text is copied into the output (only IČO numbers that pass the checksum).
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any

PAGES = ('', '/kontakt', '/obchodni-podminky')
MAX_PAGE_BYTES = 2_000_000
MAX_PLACES = 10
# "IČ: 123 45 678", "IČO 12345678", "ICO:12 345 678", "I.Č. 12345678"; not "DIČ" (\b needs a non-letter before I).
ICO_RE = re.compile(r'\bI\.?\s?[ČC]\.?\s?(?:O\.?)?\s*[:：]?\s*(\d(?:[  ]?\d){7})(?!\d)', re.IGNORECASE)
WEIGHTS = {  # risk points per finding; level from the sum
    'website_unreachable': 50,
    'ico_mismatch': 60,
    'ico_missing': 30,
    'maps_permanently_closed': 60,
    'maps_not_found': 20,
}


class InputError(ValueError):
    """Invalid Actor input (reported as a failed run, never as a risk level)."""


def ico_checksum_ok(ico: str) -> bool:
    if not (isinstance(ico, str) and len(ico) == 8 and ico.isdigit()):
        return False
    s = sum(int(d) * w for d, w in zip(ico[:7], range(8, 1, -1), strict=True))
    return (11 - s % 11) % 10 == int(ico[7])


def validate_input(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    ico = str(raw.get('ico') or '').strip()
    if not re.fullmatch(r'\d{8}', ico):
        raise InputError('ico must be 8 digits')
    if not ico_checksum_ok(ico):
        raise InputError(f'ico {ico} fails the mod-11 checksum')
    url = str(raw.get('websiteUrl') or '').strip().rstrip('/')
    if not re.match(r'^https?://[^\s/]+', url):
        raise InputError('websiteUrl must start with http:// or https://')
    return {
        'ico': ico,
        'websiteUrl': url,
        'checkMaps': bool(raw.get('checkMaps', True)),
        'companyName': str(raw.get('companyName') or '').strip(),
        'city': str(raw.get('city') or 'Brno').strip(),
        'maxPlaces': max(1, min(int(raw.get('maxPlaces') or 3), MAX_PLACES)),
    }


def page_text(body: str) -> str:
    body = re.sub(r'(?is)<(script|style)\b.*?</\1>', ' ', body)
    return html.unescape(re.sub(r'(?s)<[^>]+>', ' ', body))


def find_icos(text: str) -> list[str]:
    """Checksum-valid IČO numbers written next to an IČ/IČO label, in order of appearance."""
    found: list[str] = []
    for m in ICO_RE.finditer(text):
        num = re.sub(r'\D', '', m.group(1))
        if ico_checksum_ok(num) and num not in found:
            found.append(num)
    return found


async def fetch_pages(base_url: str, client: Any) -> list[dict[str, Any]]:
    """GET home, /kontakt, /obchodni-podminky -> [{url, ok, status, icos}] (errors become ok=False)."""
    out = []
    for path in PAGES:
        url = base_url + path
        try:
            r = await client.get(url)
            ok = r.status_code < 400
            text = page_text(r.text[:MAX_PAGE_BYTES]) if ok else ''
            out.append({'url': url, 'ok': ok, 'status': r.status_code, 'icos': find_icos(text)})
        except Exception as e:  # noqa: BLE001 - any network failure is a signal, not a crash
            out.append({'url': url, 'ok': False, 'status': None, 'icos': [], 'error': type(e).__name__})
    return out


def site_signals(ico: str, base_url: str, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    home = pages[0]
    signals = [{
        'name': 'website_reachable',
        'value': home['ok'],
        'source_url': home['url'],
        'explanation': f"Home page answered HTTP {home['status']}." if home['status'] else
                       f"Home page did not answer ({home.get('error', 'error')}).",
    }]
    if not home['ok']:
        return signals
    ours = next((p for p in pages if ico in p['icos']), None)
    others = [(p['url'], n) for p in pages for n in p['icos'] if n != ico]
    other_numbers = list(dict.fromkeys(n for _, n in others))
    signals.append({
        'name': 'website_mentions_ico',
        'value': ours is not None,
        'source_url': ours['url'] if ours else base_url,
        'explanation': f'IČO {ico} found on {ours["url"]} (Civil Code §435 requires it on the website).' if ours
                       else f'IČO {ico} not found on the home page, /kontakt or /obchodni-podminky.',
    })
    signals.append({
        'name': 'website_other_icos',
        'value': other_numbers,
        'source_url': others[0][0] if others else base_url,
        'explanation': f'The site shows IČO {", ".join(other_numbers)}, which differs from {ico}.' if others
                       else 'No other IČO on the site.',
    })
    return signals


def maps_signals(places: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """places: Google Maps items (title, totalScore, reviewsCount, permanentlyClosed, url); None = not checked."""
    if places is None:
        return []
    if not places:
        return [{'name': 'maps_listing_found', 'value': False, 'source_url': None,
                 'explanation': 'No Google Maps place found for the company name and city.'}]
    p = places[0]
    url = p.get('url')
    return [
        {'name': 'maps_listing_found', 'value': True, 'source_url': url, 'explanation': 'A Google Maps place exists.'},
        {'name': 'maps_rating', 'value': p.get('totalScore'), 'source_url': url, 'explanation': 'Average rating.'},
        {'name': 'maps_reviews', 'value': p.get('reviewsCount'), 'source_url': url,
         'explanation': 'Number of reviews (count only; review texts are not collected).'},
        {'name': 'maps_permanently_closed', 'value': bool(p.get('permanentlyClosed')), 'source_url': url,
         'explanation': 'Google Maps marks the place permanently closed.' if p.get('permanentlyClosed')
                        else 'The place is not marked closed.'},
    ]


def score(signals: list[dict[str, Any]]) -> tuple[int, str]:
    v = {s['name']: s['value'] for s in signals}
    points = 0
    if v.get('website_reachable') is False:
        points += WEIGHTS['website_unreachable']
    elif v.get('website_mentions_ico') is False:
        points += WEIGHTS['ico_mismatch'] if v.get('website_other_icos') else WEIGHTS['ico_missing']
    if v.get('maps_permanently_closed') is True:
        points += WEIGHTS['maps_permanently_closed']
    if v.get('maps_listing_found') is False:
        points += WEIGHTS['maps_not_found']
    points = min(points, 100)
    return points, 'high' if points >= 60 else 'medium' if points >= 20 else 'low'


def build_output(inp: dict[str, Any], pages: list[dict[str, Any]], places: list[dict[str, Any]] | None,
                 now: datetime | None = None) -> dict[str, Any]:
    signals = site_signals(inp['ico'], inp['websiteUrl'], pages) + maps_signals(places)
    points, level = score(signals)
    return {
        'ico': inp['ico'],
        'websiteUrl': inp['websiteUrl'],
        'score': points,
        'level': level,
        'signals': signals,
        'checkedAt': (now or datetime.now(UTC)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }


def maps_input(inp: dict[str, Any], name: str) -> dict[str, Any]:
    """Input for compass/crawler-google-places: capped, no personal data (SPEC §10.2)."""
    return {
        'searchStringsArray': [name],
        'locationQuery': f"{inp['city']}, Czechia",
        'language': 'cs',
        'countryCode': 'cz',
        'maxCrawledPlacesPerSearch': inp['maxPlaces'],
        'scrapeContacts': False,
        'scrapePlaceDetailPage': False,
        'maxReviews': 0,
        'maxImages': 0,
        'scrapeReviewsPersonalData': False,
        'maximumLeadsEnrichmentRecords': 0,
    }


MAPS_FIELDS = ['title', 'totalScore', 'reviewsCount', 'permanentlyClosed', 'url', 'placeId', 'city']  # never phone
