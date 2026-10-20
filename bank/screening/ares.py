# ARES (SPEC §6.4 p.2): GET by IČO, no key. Unknown IČO -> 404 NENALEZENO, bad format -> 400.
# Live checks only for legal entities: pravniForma 112 (s.r.o.) and 121 (a.s.); a sole trader's entry is
# a person's name (personal data), so for any other form we keep nothing but the form code.
from bank import codes

URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"
LEGAL_ENTITY_FORMS = {"112": "s.r.o.", "121": "a.s."}


class RegistryError(Exception):
    """The registry answered, but not with a usable result (5xx, maintenance window, SOAP fault)."""


def url(ico: str) -> str:
    return URL.format(ico=ico)


def fetch(ico: str, client) -> dict:
    r = client.get(url(ico), headers={"Accept": "application/json"})
    if r.status_code >= 500:
        raise RegistryError(f"ARES HTTP {r.status_code}")
    return {"status": r.status_code, "body": r.json()}


def parse(raw: dict) -> dict:
    status, body = raw["status"], raw["body"] or {}
    if status == 404:
        return {"found": False}
    if status != 200:
        raise RegistryError(f"ARES HTTP {status}: {body.get('kod')}")
    form = body.get("pravniForma")
    if form not in LEGAL_ENTITY_FORMS:
        return {"found": True, "legal_form": form, "legal_entity": False}
    regs = body.get("seznamRegistraci") or {}
    seat = body.get("sidlo") or {}
    return {"found": True, "legal_form": form, "legal_entity": True, "ico": body.get("ico"),
            "name": body.get("obchodniJmeno"), "address": seat.get("textovaAdresa"),
            "street": seat.get("nazevUlice"), "psc": str(seat["psc"]) if seat.get("psc") else None,
            "founded": body.get("datumVzniku"), "dissolved": body.get("datumZaniku"), "dic": body.get("dic"),
            "vat_registered": regs.get("stavZdrojeDph") == "AKTIVNI",
            "insolvency_flag": regs.get("stavZdrojeIr") == "AKTIVNI",
            "registrations": {k: regs.get(k) for k in ("stavZdrojeVr", "stavZdrojeDph", "stavZdrojeIr",
                                                       "stavZdrojeCeu")}}


def reasons(p: dict, ico: str) -> list[dict]:
    src = url(ico)
    if not p["found"]:
        return [codes.reason("ares_not_found", True, f"IČO {ico} is not in ARES.", src)]
    if not p["legal_entity"]:
        return [codes.reason("legal_entities_only", True,
                             "Check only for legal entities (s.r.o., a.s.).", src, legal_form=p["legal_form"])]
    out = []
    if p["dissolved"]:
        out.append(codes.reason("ares_dissolved", True, f"Company dissolved on {p['dissolved']} (ARES).", src,
                                dissolved=p["dissolved"]))
    if p["insolvency_flag"]:
        out.append(codes.reason("insolvency_record", True, "ARES shows an active insolvency register entry.",
                                src, stavZdrojeIr="AKTIVNI"))
    return out
