# Source snapshots (MODEL_V2 §5.1, §2.1). The bank publishes source templates; for rule C the task terms carry
# {"op": "source", "template": t, "key_field": f} and the bank builds the snapshot URL itself from the template and
# the item's key — a source_url sent by the seller is never fetched. Each template publishes its field mapping
# "item field -> path in the snapshot" with a comparison type; a C claim is possible only on mapped fields.
import re2

TEMPLATES = {
    "ares": {
        "title": "ARES — economic subject by IČO (Ministry of Finance CZ)",
        "url": "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{key}",
        "host": "ares.gov.cz", "key": r"\d{8}", "method": "api",
        "fields": {"name": {"path": "obchodniJmeno", "type": "string"},
                   "founded": {"path": "datumVzniku", "type": "date"},
                   "address": {"path": "sidlo.textovaAdresa", "type": "string"},
                   "postalCode": {"path": "sidlo.psc", "type": "code"}},
    },
}


def key_ok(template: str, key) -> bool:
    t = TEMPLATES.get(template)
    return bool(t) and isinstance(key, (str, int)) and not isinstance(key, bool) \
        and re2.fullmatch(t["key"], str(key).strip()) is not None


def build_url(template: str, key) -> str:
    """The only way a snapshot URL is made: published template + the item's key (never the seller's URL)."""
    if not key_ok(template, key):
        raise ValueError(f"{key!r} is not a key of {template}")
    return TEMPLATES[template]["url"].format(key=str(key).strip())


def extract(template: str, body) -> dict:
    """Field values the bank itself reads from a snapshot by the published mapping: {field: value, field:type: t}."""
    out = {}
    for field, m in TEMPLATES[template]["fields"].items():
        cur = body
        for part in m["path"].split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is not None:
            out[field] = cur
            out[field + ":type"] = m["type"]
    return out
