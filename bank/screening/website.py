# Web footprint from the Apify actor company-check (SPEC §10.1, actors/company-check/): IČO on the website
# and the Google Maps place. Registries are checked by the bank itself; the actor only adds what a registry
# cannot know. Its output is data about an untrusted site, so only typed signal values are read here.
from bank import codes, config

ACTOR_TIMEOUT_S = 120


def run_actor(ico: str, website_url: str, name: str | None = None) -> dict:
    """Live: call our actor on Apify (tens of seconds -> prefetch only, result cached)."""
    from datetime import timedelta

    from apify_client import ApifyClient
    client = ApifyClient(config.APIFY_TOKEN)
    run = client.actor(config.APIFY_COMPANY_CHECK_ACTOR).call(
        run_input={"ico": ico, "websiteUrl": website_url, "checkMaps": True, "companyName": name or ""},
        run_timeout=timedelta(seconds=ACTOR_TIMEOUT_S))
    items = client.dataset(run.default_dataset_id).list_items().items
    if not items:
        raise RuntimeError("company-check returned no item")
    return items[0]


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
