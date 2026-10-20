# Related companies by a common beneficial owner (EvSM, synthetic extract): tier_sum adds deals of one task with
# related sellers (MODEL_V2 §2), so splitting a purchase across sellers of one owner does not lower the tier.
from bank import evsm, mandate
from tests.unit.test_mandate_v2 import NOW, deal, m  # noqa: F401 — the fixture

BETA, GAMA, DELTA, ALFA = "30181020", "59093994", "54763363", "64146103"


def test_extract_relates_gama_and_delta_only():
    assert evsm.related(GAMA, DELTA) and evsm.related(DELTA, GAMA) and evsm.related(BETA, BETA)
    assert not evsm.related(BETA, GAMA) and not evsm.related(ALFA, DELTA) and not evsm.related(None, None)
    assert evsm.related_icos(DELTA) == {GAMA, DELTA} and evsm.related_icos("99999999") == {"99999999"}


def test_related_sellers_come_from_the_extract(m):  # noqa: F811
    assert mandate.related_sellers("s-gama") == ["s-delta", "s-gama"]
    assert mandate.related_sellers("s-beta") == ["s-beta"]
    assert mandate.related_sellers("s-unknown") == ["s-unknown"]


def test_tier_sum_adds_the_task_with_related_sellers(m):  # noqa: F811
    deal("d-gama", seller="s-gama", amount=4_000_000, state="settled")   # same task, related seller, 4 €
    deal("d-beta", seller="s-beta", amount=3_000_000, state="settled")   # same task, unrelated seller
    ts = mandate.tier_sum(deal(seller="s-delta"), 200, NOW)
    assert ts["task_with_seller"] == 200 + 400 and ts["seller_24h"] == 200 and ts["tier_sum"] == 600


def test_decider_related_to_a_side_by_owner_is_challenged(monkeypatch):
    from bank import deciders
    monkeypatch.setitem(evsm._extract()["deciders"], "dec-2", [GAMA])  # dec-2 works for Gama's owner
    case = {"sides": {"buyer": ALFA, "seller": DELTA}, "exclude": [], "challenges": {"buyer": {}, "seller": {}}}
    left, reserve = deciders.eligible(case)
    assert reserve is True and left == [deciders.RESERVE_PROVIDER]  # 2 left < minimum pool of 3 -> reserve
    monkeypatch.setitem(evsm._extract()["deciders"], "dec-2", [])
    assert [d["id"] for d in deciders.eligible(case)[0]] == ["dec-1", "dec-2", "dec-3"]
