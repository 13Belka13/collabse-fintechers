"""company-check Actor: the web footprint of a Czech company for a bank's counterparty check (SPEC §10.1).

Input: ico (8 digits, checksum), websiteUrl, checkMaps. Output (dataset item and OUTPUT record):
{ico, websiteUrl, score, level: low/medium/high, signals: [{name, value, source_url, explanation}], checkedAt}.
Registries (ARES, VAT, insolvency, sanctions) are checked by the bank itself; this Actor adds what a
registry cannot know. Website content is untrusted data; no personal data is collected.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
from apify import Actor

from . import checks

USER_AGENT = 'company-check (Apify Actor; counterparty web footprint)'


async def search_maps(inp: dict, name: str) -> list[dict]:
    """One capped compass/crawler-google-places run; only non-personal fields are read."""
    run = await Actor.call(
        'compass/crawler-google-places',
        checks.maps_input(inp, name),
        timeout=timedelta(minutes=3),
        max_total_charge_usd=Decimal('0.50'),
    )
    if run is None or not run.default_dataset_id:
        raise RuntimeError('Google Maps run did not start')
    page = await Actor.apify_client.dataset(run.default_dataset_id).list_items(fields=checks.MAPS_FIELDS)
    return [{k: it.get(k) for k in checks.MAPS_FIELDS} for it in page.items]


async def main() -> None:
    async with Actor:
        try:
            inp = checks.validate_input(await Actor.get_input())
        except checks.InputError as e:
            await Actor.fail(status_message=f'Invalid input: {e}')
            return

        Actor.log.info(f'Checking website of IČO {inp["ico"]}: {inp["websiteUrl"]}')
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                     headers={'User-Agent': USER_AGENT}) as client:
            pages = await checks.fetch_pages(inp['websiteUrl'], client)

        places = None
        if inp['checkMaps']:
            name = inp['companyName'] or inp['websiteUrl'].split('//', 1)[1].split('/')[0]
            try:
                places = await search_maps(inp, name)
            except Exception as e:  # noqa: BLE001 - the website signals are still worth returning
                Actor.log.warning(f'Google Maps check failed: {type(e).__name__}')

        item = checks.build_output(inp, pages, places)
        Actor.log.info(f'IČO {inp["ico"]}: level {item["level"]} (score {item["score"]})')
        await Actor.push_data(item)
        await Actor.set_value('OUTPUT', item)
