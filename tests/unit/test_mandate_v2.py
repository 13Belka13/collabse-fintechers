# Mandate additions (MODEL_V2 §2, demo scale §14): tier_sum over 24 h with the seller and over the task with the
# seller, the dispute floor (tier lifted, window), require_b_for and require_c_above with the person-confirmed
# fallbacks, the shared unprotected daily cap, and the claim-fee budget reserved at pay.
import time

import pytest

from bank import config, db, mandate
from bank.codes import Code

NOW = time.time()
SOURCE = {"op": "source", "template": "ares", "key_field": "ico"}


@pytest.fixture
def m(seeded):
    mandate.ensure_demo()
    return "m-alfa-1"


def deal(deal_id="d-x", seller="s-beta", category="data_purchase", task_hash="t1", preds=(), confirmed=None,
         state="quoted", amount=None, asset="EURC"):
    crit = {"category": category, "hash": task_hash, "predicates": list(preds)}
    db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, mandate_id, criteria_json, asset, amount, state,"
           " created_at, confirmed_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (deal_id, "alfa", seller, "m-alfa-1", db.dumps(crit), asset, amount, state, NOW, confirmed))
    return {"deal_id": deal_id, "buyer_id": "alfa", "seller_id": seller, "criteria": crit, "confirmed_by": confirmed}


def terms(rule="B", tier="T1", window=20.0, category="data_purchase", count=20):
    return {"rule": rule, "tier": tier, "window_s": window, "category": category, "count": count,
            "fee_bps": config.TARIFF_BPS.get((tier, rule), 0)}


def test_defaults_are_at_the_demo_scale(m):
    p = mandate.current(m)["policy"]
    assert p["dispute_floor"][0]["tier_sum_max"] == "20" and p["dispute_floor"][1]["tier_sum_max"] == "500"
    assert p["dispute_floor"][1]["min_window_s"] == pytest.approx(20) and p["require_c_above_eur"] == "2000"
    assert p["claim_fee_budget_eur_per_month"] == "1000" and p["require_b_for"] == ["data_purchase"]


def test_tier_sum_counts_the_seller_in_24h_and_the_task_with_the_seller(m):
    deal("d-old", amount=4_000_000, state="settled")               # same seller, same task, 4 €
    deal("d-other-task", amount=3_000_000, state="settled", task_hash="t2")
    deal("d-refused", amount=9_000_000, state="refused")           # refused deals do not count
    d = deal()
    ts = mandate.tier_sum(d, 200, NOW)
    assert ts == {"tier_sum": 200 + 400 + 300, "deal": 200, "seller_24h": 900, "task_with_seller": 600}


def test_split_lots_reach_t2_and_need_the_t2_window(m):
    deal("d-old", amount=4_000_000, state="settled")
    t = terms(window=20.0)
    out = mandate.protection(m, deal(), t, 200, NOW)  # 2 € now + 4 € before = 6 € > 5 € (demo T1 max)
    assert out["code"] == Code.TERMS_MISMATCH and out["reasons"][0]["code"] == "window_below_mandate"
    t = terms(window=config.WINDOW_S["T2"][0])
    assert mandate.protection(m, deal("d-y"), t, 200, NOW)["status"] == "ok" and t["tier"] == "T2"
    assert t["fee_bps"] == config.TARIFF_BPS[("T2", "B")]


def test_data_purchase_needs_b_and_is_never_unprotected(m):
    out = mandate.protection(m, deal(), terms(rule="A", tier="T0", window=0), 10, NOW)
    assert out["code"] == Code.TERMS_MISMATCH and out["reasons"][0]["code"] == "rule_below_mandate"
    t = terms(rule="B", tier="T0")
    assert mandate.protection(m, deal("d-b"), t, 10, NOW)["status"] == "ok" and t["tier"] == "T1"  # lifted from T0


def test_c_required_above_the_threshold_falls_back_to_b_only_with_a_person(m):
    big = 2001  # 20,01 € > 20 € (demo require_c_above_eur)
    t = terms(tier="T2", window=config.WINDOW_S["T2"][0])
    out = mandate.protection(m, deal(), t, big, NOW)
    assert out["code"] == Code.CONFIRMATION_REQUIRED and out["reasons"][0]["code"] == "c_unavailable"
    ok = mandate.protection(m, deal("d-conf", confirmed="human:anna"), dict(t), big, NOW)
    assert ok["status"] == "ok" and "B instead of C" in ok["marks"][0]
    with_source = deal("d-src", preds=[SOURCE])  # C is possible here: B is simply below the mandate
    assert mandate.protection(m, with_source, dict(t), big, NOW)["code"] == Code.TERMS_MISMATCH


def test_unprotected_payments_share_one_daily_cap(m):
    """50 cents a day over all sellers (demo): 20 + 20 with two sellers leaves 10 for a third."""
    for i, seller in enumerate(("s-beta", "s-gama")):
        deal(f"d-t0-{i}", seller=seller, category="other", amount=200_000, state="settled")
        db.run("UPDATE deals SET terms_json=? WHERE deal_id=?", (db.dumps({"tier": "T0"}), f"d-t0-{i}"))
        assert mandate.reserve(m, f"d-t0-{i}", 20)["status"] == "ok"
    t = terms(rule="A", tier="T0", window=0, category="other")
    out = mandate.protection(m, deal(seller="s-delta", category="other"), dict(t), 11, NOW)
    assert out["code"] == Code.BUDGET_EXCEEDED and out["reasons"][0]["code"] == "unprotected_daily_cap"
    assert mandate.protection(m, deal("d-ok", seller="s-delta", category="other"), dict(t), 10, NOW)["status"] == "ok"


def test_claim_fee_budget_is_reserved_for_c_and_released_without_a_claim(m):
    t = terms(rule="C", tier="T2", window=config.WINDOW_S["T2"][0], count=10)
    assert mandate.protection(m, deal(preds=[SOURCE]), t, 800, NOW)["status"] == "ok"
    assert t["fee_reserved_eur_cents"] == 80  # max(0,50 € + 10 × 0,02 €, 10 % of 8 €)
    mandate.reserve_fee(m, "d-x", 80)
    assert mandate.fee_budget_used(m, NOW + 1) == 80
    mandate.release_fee("d-x")
    assert mandate.fee_budget_used(m, NOW + 1) == 0
    mandate.reserve_fee(m, "d-x", 80)
    mandate.set_fee_reservation("d-x", 66)  # filed: the actual F, spent
    assert mandate.fee_budget_used(m, NOW + 1) == 66
    mandate.release_fee("d-x")  # spent stays spent
    assert mandate.fee_budget_used(m, NOW + 1) == 66


def test_no_room_in_the_fee_budget_needs_a_person(m):
    mandate.reserve_fee(m, "d-other", 990)
    t = terms(rule="C", tier="T2", window=config.WINDOW_S["T2"][0], count=10)
    out = mandate.protection(m, deal(preds=[SOURCE]), t, 800, NOW)
    assert out["code"] == Code.CONFIRMATION_REQUIRED and out["reasons"][0]["code"] == "claim_fee_budget"
    ok = mandate.protection(m, deal("d-c", preds=[SOURCE], confirmed="human:anna"), dict(t), 800, NOW)
    assert ok["status"] == "ok" and "without a reserved claim fee" in ok["marks"][0]


def test_requirements_tell_the_seller_the_prior_sum(m):
    deal("d-old", amount=4_000_000, state="settled")
    req = mandate.requirements(m, deal())
    assert req["prior_eur_cents"] == 400 and req["require_b"] and req["require_c_above_eur_cents"] == 2000
