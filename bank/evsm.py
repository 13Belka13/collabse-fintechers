# Beneficial owners (EvSM, evidence skutečných majitelů, zákon 37/2021 Sb.; MODEL_V2 §2, §7C.6, §7C.10). Two
# companies are related when they share a beneficial owner: then their deals of one task add up in tier_sum, a
# buyer related to the seller is not "independent" in the seller's C-review limit, and a decider related to a side
# is challenged. The register itself needs authorised access, so the bank reads a SYNTHETIC extract
# (fixtures/registries/evsm.json: pseudonymous owner ids per IČO, no names). TODO(data): the live register.
import functools
import json

from bank import config

FIXTURE = config.ROOT / "fixtures" / "registries" / "evsm.json"


@functools.cache
def _extract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def owners(ico: str | None) -> set[str]:
    return set(_extract()["owners"].get(str(ico or ""), []))


def related(a: str | None, b: str | None) -> bool:
    """Same company, or a common beneficial owner."""
    return bool(a) and (a == b or bool(owners(a) & owners(b)))


def related_icos(ico: str | None) -> set[str]:
    """Every IČO in the extract that shares an owner with this one, itself included."""
    mine = owners(ico)
    return {str(ico)} | {i for i, o in _extract()["owners"].items() if mine & set(o)} if ico else set()


def decider_icos(decider_id: str) -> set[str]:
    """IČOs a decider is related to (its own firm and firms of its owners); empty for the demo deciders."""
    return set(_extract().get("deciders", {}).get(decider_id, []))
