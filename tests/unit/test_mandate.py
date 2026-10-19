# Mandate rules (SPEC §6.1-6.5): every code of the closed list, VAT rule, versions + hash, atomic reservations.
import threading
import time
from datetime import UTC, datetime

import pytest

from bank import codes, core, db, journal, mandate, money, timing
from bank.codes import Code

EVIL = "0xBadBadBadBadBadBadBadBadBadBadBadBadBad0"
BETA_ACCOUNT = "5638829718/9999"


@pytest.fixture
def m(seeded):
    mandate.ensure_demo()
    return mandate.DEMO_MANDATE_ID


def screening(level="ok", payer=False, unreliable=None, published=(), reasons=()):
    return {"level": level, "reasons": list(reasons), "sources": [],
            "vat": {"payer": payer, "unreliable": unreliable, "published_accounts": list(published)}}


def case(price="5.00", asset="EURC", seller="s-beta", deal_id="d1", task_budget="500", pay_to=None,
         quote_amount=None, offer=True, **deal_extra):
    amount = money.to_atomic(asset, price)
    deal = {"deal_id": deal_id, "seller_id": seller, **deal_extra,
            "task": {"text": "20 warehouses in Brno", "budget": {"value": task_budget, "currency": "EUR"}}
            if task_budget else {},
            "offer": {"offer_id": f"{deal_id}-o1", "price": price, "asset": asset, "amount": str(amount)} if offer else None}
    wallet = (core.seller(seller) or {}).get("wallet_address")
    quote = {"asset": asset, "amount": str(quote_amount or amount), "pay_to": pay_to or wallet}
    return deal, quote


def decide(m, scr=None, **kw):
    deal, quote = case(**kw)
    return mandate.decide(m, deal, quote, screening() if scr is None else scr)


# ---- one case per code of the closed list -------------------------------------------------------

def c_not_active(m):
    mandate.amend(m, {"active": False}, "human:test")
    return decide(m)


def c_expired(m):
    later = datetime(2027, 1, 1, tzinfo=UTC).timestamp()
    deal, quote = case()
    return mandate.decide(m, deal, quote, screening(), now=later)


def c_category(m):
    mandate.amend(m, {"category": "logistics"}, "human:test")
    return decide(m)


def c_not_in_scope(m):
    return decide(m, pay_to=EVIL)


def c_not_trusted(m):
    r = codes.reason("ico_mismatch_on_website", True, "IČO on the website differs from ARES",
                     "https://ares.gov.cz/ekonomicke-subjekty?ico=54763363")
    return decide(m, scr=screening("blocked", reasons=[r]), seller="s-delta")


def c_amount(m):
    return decide(m, price="6.00")  # task budget 5 €


def c_budget(m):
    assert mandate.reserve(m, "old1", 1000)["status"] == "ok"
    assert mandate.reserve(m, "old2", 1000)["status"] == "ok"
    assert mandate.reserve(m, "old3", 600)["status"] == "ok"
    return decide(m)  # 26 € used + 5 € > 30 €


def c_quote(m):
    return decide(m, quote_amount=money.to_atomic("EURC", "5.01"))


def c_velocity(m):
    mandate.amend(m, {"velocity": {"max_deals": 2, "window_s": 60}}, "human:test")
    mandate.reserve(m, "v1", 100)
    mandate.reserve(m, "v2", 100)
    return decide(m)


def c_confirm(m):
    return decide(m, price="7.00", task_budget=None)


def c_vat(m):
    return decide(m, scr=screening(payer=True, unreliable="NE", published=[BETA_ACCOUNT]))


def c_review(m):
    timeout = codes.reason("registry_timeout", False, "ARES did not answer in 2 s")
    return decide(m, scr=screening("review", reasons=[timeout]))


CASES = {
    Code.MANDATE_NOT_ACTIVE: c_not_active, Code.MANDATE_EXPIRED: c_expired, Code.CATEGORY_NOT_IN_SCOPE: c_category,
    Code.COUNTERPARTY_NOT_IN_SCOPE: c_not_in_scope, Code.COUNTERPARTY_NOT_TRUSTED: c_not_trusted,
    Code.AMOUNT_ABOVE_LIMIT: c_amount, Code.BUDGET_EXCEEDED: c_budget, Code.QUOTE_MISMATCH: c_quote,
    Code.VELOCITY_EXCEEDED: c_velocity, Code.CONFIRMATION_REQUIRED: c_confirm, Code.VAT_LIABILITY_RISK: c_vat,
    Code.UNDER_REVIEW: c_review,
}


def test_every_code_of_the_closed_list_has_a_case():
    assert set(CASES) == set(Code)


@pytest.mark.parametrize("code", list(Code), ids=str)
def test_each_refusal_code_is_reproduced(m, code):
    d = CASES[code](m)
    assert d["code"] == code
    assert d["status"] == codes.SPEC[code][0] and d["next_tool"] == codes.SPEC[code][1]
    assert d["reasons"] and all(isinstance(r["public"], bool) for r in d["reasons"])
    assert d["explanation"]
    view = codes.client_view(d)
    if code == Code.UNDER_REVIEW:
        assert view["explanation"] == codes.NEUTRAL_CLIENT_TEXT and view["reasons"] == []
    else:  # all mandate/registry-fact reasons are public: the client sees the real code and sources
        assert view["code"] == code and view["reasons"]


def test_ok_decision_shape_timing_and_speed(m):
    t0 = time.perf_counter()
    d = decide(m, deal_id="d-ok")
    ms = (time.perf_counter() - t0) * 1000
    assert d["status"] == "ok" and d["code"] is None and d["eur_cents"] == 500 and d["asset"] == "EURC"
    assert d["mandate_version"] == 1 and d["policy_hash"] == mandate.current(m)["policy_hash"]
    assert ms < 50
    assert timing.stage_stats("decision")["n"] == 1
    assert db.one("SELECT deal_id, ok FROM timings WHERE stage='decision'") == {"deal_id": "d-ok", "ok": 1}


def test_decide_moves_nothing(m):
    decide(m)
    assert db.one("SELECT COUNT(*) n FROM reservations")["n"] == 0


# ---- refusal details ------------------------------------------------------------------------------

def test_unknown_mandate_and_unknown_seller(m):
    deal, quote = case()
    assert mandate.decide("m-nope", deal, quote, screening())["code"] == Code.MANDATE_NOT_ACTIVE
    deal, quote = case(seller="s-nobody", pay_to=EVIL)
    d = mandate.decide(m, deal, quote, screening())
    assert d["code"] == Code.COUNTERPARTY_NOT_IN_SCOPE and d["reasons"][0]["code"] == "seller_not_registered"


def test_payto_swap_is_not_in_scope_even_for_a_tiny_amount(m):
    d = decide(m, price="0.10", pay_to=EVIL.lower())
    assert d["code"] == Code.COUNTERPARTY_NOT_IN_SCOPE
    assert d["reasons"][0]["evidence"]["pay_to"] == EVIL.lower()
    # the registered wallet in another letter case is fine (addresses compare case-insensitively)
    wallet = core.seller("s-beta")["wallet_address"]
    assert decide(m, pay_to=wallet.lower())["status"] == "ok"


def test_counterparty_list_in_mandate(m):
    mandate.amend(m, {"counterparties": ["59093994"]}, "human:test")  # Gama only
    d = decide(m)
    assert d["code"] == Code.COUNTERPARTY_NOT_IN_SCOPE and d["reasons"][0]["code"] == "counterparty_not_listed"


def test_asset_not_in_mandate(m):
    mandate.amend(m, {"assets_allowed": ["tCZK"]}, "human:test")
    d = decide(m)
    assert d["code"] == Code.CATEGORY_NOT_IN_SCOPE and d["reasons"][0]["code"] == "asset_not_allowed"
    deal, quote = case()
    quote["asset"] = "0x0000000000000000000000000000000000000001"
    assert mandate.decide(m, deal, quote, screening())["reasons"][0]["code"] == "asset_not_allowed"


def test_quote_given_as_raw_x402_option(m):
    from bank import config
    deal, quote = case()
    raw = {"scheme": "exact", "network": "eip155:84532", "asset": config.EURC, "amount": quote["amount"],
           "payTo": quote["pay_to"]}
    assert mandate.decide(m, deal, raw, screening())["status"] == "ok"


def test_no_offer_and_db_row_shaped_deal(m):
    d = decide(m, offer=False)
    assert d["code"] == Code.QUOTE_MISMATCH and d["reasons"][0]["code"] == "no_agreed_offer"
    deal, quote = case()
    row = {"deal_id": "d1", "seller_id": "s-beta", "task_json": db.dumps(deal["task"]),
           "offer_json": db.dumps(deal["offer"])}
    assert mandate.decide(m, row, quote, screening())["status"] == "ok"


def test_screening_missing_or_unknown_level_fails_closed(m):
    deal, quote = case()
    for scr in (None, {"level": "weird"}, {"level": "ok"}):  # last: no VAT status in the cache
        d = mandate.decide(m, deal, quote, scr)
        assert d["code"] == Code.UNDER_REVIEW
        assert codes.client_view(d)["explanation"] == codes.NEUTRAL_CLIENT_TEXT


def test_blocked_with_hidden_reason_is_neutral_for_the_client(m):
    hidden = codes.reason("sanctions_near_match", False, "Name 92% similar to a listed entity")
    d = decide(m, scr=screening("blocked", reasons=[hidden]))
    assert d["code"] == Code.COUNTERPARTY_NOT_TRUSTED
    view = codes.client_view(d)
    assert view["code"] == Code.UNDER_REVIEW and "92%" not in str(view)


def test_per_deal_limit_without_task_budget(m):
    d = decide(m, price="12.00", task_budget=None)
    assert d["code"] == Code.AMOUNT_ABOVE_LIMIT and d["reasons"][0]["code"] == "per_deal_max"


def test_task_budget_is_checked_before_per_deal(m):
    d = decide(m, price="12.00")
    assert d["reasons"][0]["code"] == "task_budget" and d["reasons"][0]["evidence"]["limit"] == 500


def test_confirmation_passes_once_confirmed(m):
    d = decide(m, price="7.00", task_budget=None, confirmed_by="human:jana")
    assert d["status"] == "ok" and "confirmed by human:jana" in d["marks"]


def test_budget_counts_spend_under_old_versions(m):
    mandate.reserve(m, "a", 1000)
    mandate.reserve(m, "b", 1000)
    mandate.amend(m, {"per_deal_max": {"value": "900", "currency": "EUR"}}, "human:test")
    assert mandate.current(m)["version"] == 2
    d = decide(m, price="9.00", task_budget=None, confirmed_by="human:t")
    assert d["status"] == "ok"
    mandate.reserve(m, "c", 900)
    d = decide(m, price="1.01", task_budget=None, deal_id="d2")
    assert d["code"] == Code.BUDGET_EXCEEDED and d["reasons"][0]["evidence"]["spent"] == 2900


def test_budget_ignores_released_and_last_month(m):
    mandate.reserve(m, "a", 1000)
    mandate.release("a")
    db.run("INSERT INTO reservations (mandate_id, deal_id, amount, state, created_at) VALUES (?,?,?,?,?)",
           (m, "last-month", 3000, "committed", time.time() - 40 * 86400))
    assert decide(m)["status"] == "ok"


def test_retry_of_the_same_deal_is_not_double_counted(m):
    mandate.reserve(m, "x1", 1000)
    mandate.reserve(m, "x2", 1000)
    mandate.reserve(m, "d1", 500)
    assert decide(m)["status"] == "ok"  # d1's own reservation does not count against d1


# ---- VAT rule §6.5 ------------------------------------------------------------------------------------

def test_vat_eurc_to_payer_then_acknowledged_once(m):
    scr = screening(payer=True, unreliable="NE", published=[BETA_ACCOUNT])
    d = decide(m, scr=scr)
    assert d["code"] == Code.VAT_LIABILITY_RISK and d["reasons"][0]["code"] == "paid_in_virtual_asset"
    v1 = mandate.current(m)
    v = mandate.acknowledge_vat(m, "30181020", "EURC", "human:jana")
    assert v == 2 and mandate.current(m)["policy_hash"] != v1["policy_hash"]
    assert mandate.acknowledge_vat(m, "30181020", "EURC", "human:jana") == 2  # once per pair
    d = decide(m, scr=scr)
    assert d["status"] == "ok" and d["marks"][0].startswith("VAT risk acknowledged ")
    assert "VAT risk acknowledged" in d["explanation"]
    assert "VAT risk acknowledged" in codes.client_view(d)["explanation"]


def test_vat_ack_is_per_seller_and_asset(m):
    mandate.acknowledge_vat(m, "30181020", "tCZK", "human:jana")
    mandate.acknowledge_vat(m, "59093994", "EURC", "human:jana")
    assert decide(m, scr=screening(payer=True))["code"] == Code.VAT_LIABILITY_RISK


def test_vat_tczk_to_published_account_passes(m):
    for published in ([BETA_ACCOUNT], ["000000-5638829718/9999"], ["CZ1399990000005638829718"]):
        d = decide(m, price="125.00", asset="tCZK", scr=screening(payer=True, unreliable="NE", published=published))
        assert d["status"] == "ok" and d["marks"] == [] and d["eur_cents"] == 500


def test_vat_tczk_to_unpublished_account_is_a_risk(m):
    d = decide(m, price="125.00", asset="tCZK", scr=screening(payer=True, published=["1111111111/0100"]))
    assert d["code"] == Code.VAT_LIABILITY_RISK and d["reasons"][0]["code"] == "account_not_published"


def test_vat_unreliable_payer_is_not_trusted_and_public(m):
    d = decide(m, price="125.00", asset="tCZK", scr=screening(payer=True, unreliable="ANO", published=[BETA_ACCOUNT]))
    assert d["code"] == Code.COUNTERPARTY_NOT_TRUSTED and d["reasons"][0]["code"] == "vat_unreliable_payer"
    assert codes.client_view(d)["code"] == Code.COUNTERPARTY_NOT_TRUSTED


def test_non_payer_in_eurc_passes_without_mark(m):
    d = decide(m, scr=screening(payer=False, unreliable="NENALEZEN"))
    assert d["status"] == "ok" and d["marks"] == []


def test_tczk_rounds_up_to_the_next_cent(m):
    d = decide(m, price="125.01", asset="tCZK", scr=screening())
    assert d["code"] == Code.AMOUNT_ABOVE_LIMIT and d["reasons"][0]["evidence"]["eur_cents"] == 501


# ---- versions and hash ------------------------------------------------------------------------------

def test_versions_are_immutable_and_hashes_recompute(m):
    mandate.amend(m, {"confirm_above": {"value": "500", "currency": "EUR"}}, "human:test")
    mandate.acknowledge_vat(m, "30181020", "EURC", "human:test")
    vs = mandate.versions(m)
    assert [v["version"] for v in vs] == [1, 2, 3]
    assert len({v["policy_hash"] for v in vs}) == 3
    for v in vs:
        assert v["policy_hash"] == mandate.policy_hash(v["policy"]) and v["policy"]["version"] == v["version"]
    assert vs[0]["policy"]["confirm_above"]["value"] == "600"  # old version untouched
    p = vs[0]["policy"]
    assert p["per_deal_max"]["value"] == "1000" and p["budget"]["value"] == "3000" and p["company_ico"] == "64146103"
    events = [e["event"] for e in journal.entries()]
    assert events.count("mandate_version") == 3 and journal.verify() == (True, None)


def test_invalid_policies_are_rejected(m):
    with pytest.raises(ValueError, match="below per_deal_max"):
        mandate.amend(m, {"confirm_above": {"value": "1000", "currency": "EUR"}}, "h")
    with pytest.raises(ValueError, match="digit string"):
        mandate.amend(m, {"per_deal_max": {"value": "10.00", "currency": "EUR"}}, "h")
    with pytest.raises(ValueError, match="cannot change"):
        mandate.amend(m, {"version": 7}, "h")
    with pytest.raises(ValueError, match="unknown asset"):
        mandate.amend(m, {"assets_allowed": ["USDT"]}, "h")
    with pytest.raises(ValueError, match="exceed the budget"):
        mandate.amend(m, {"budget": {"value": "900", "currency": "EUR", "period": "month"}}, "h")
    with pytest.raises(ValueError):
        mandate.create("m2", "nobody", {}, "h")
    with pytest.raises(ValueError):
        mandate.amend("m-nope", {}, "h")
    assert mandate.current(m)["version"] == 1


# ---- reservations ------------------------------------------------------------------------------------

def test_race_ten_parallel_reservations_budget_for_three(m):
    mandate.amend(m, {"budget": {"value": "1500", "currency": "EUR", "period": "month"}}, "human:test")
    db.conn()
    start = threading.Barrier(10)
    results = [None] * 10

    def pay(i):
        start.wait()
        try:
            results[i] = mandate.reserve(m, f"race-{i}", 500)
        finally:
            db.close()

    threads = [threading.Thread(target=pay, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    outcome = sorted(r["code"] or "ok" for r in results)
    assert outcome == ["BUDGET_EXCEEDED"] * 7 + ["ok"] * 3
    assert db.one("SELECT COUNT(*) n, SUM(amount) s FROM reservations WHERE state='reserved'") == {"n": 3, "s": 1500}
    assert journal.verify() == (True, None)


def test_reserve_is_idempotent_per_deal(m):
    a = mandate.reserve(m, "d1", 500)
    b = mandate.reserve(m, "d1", 500)
    assert a["status"] == b["status"] == "ok" and b["idempotent"] and a["reservation_id"] == b["reservation_id"]
    assert db.one("SELECT COUNT(*) n FROM reservations")["n"] == 1
    with pytest.raises(ValueError):
        mandate.reserve(m, "d0", 0)
    assert mandate.reserve("m-nope", "d9", 100)["code"] == Code.MANDATE_NOT_ACTIVE


def test_partial_capture_then_release_returns_the_rest(m):
    mandate.reserve(m, "d1", 500)
    mandate.commit("d1", 428)   # 4,275 € rounded up by the caller
    s = mandate.summary(m)
    assert (s["committed_eur_cents"], s["reserved_eur_cents"], s["remaining_eur_cents"]) == (428, 72, 2500)
    mandate.commit("d1", 25)
    r = mandate.release("d1")
    assert r == {"deal_id": "d1", "state": "committed", "committed": 453}
    s = mandate.summary(m)
    assert (s["committed_eur_cents"], s["reserved_eur_cents"], s["remaining_eur_cents"]) == (453, 0, 2547)
    assert mandate.release("d1")["state"] == "committed"  # idempotent
    with pytest.raises(ValueError):
        mandate.commit("d1", 1)  # nothing left to capture


def test_full_void_releases_and_the_deal_can_reserve_again(m):
    mandate.reserve(m, "d1", 500)
    assert mandate.release("d1")["state"] == "released"
    assert mandate.summary(m)["remaining_eur_cents"] == 3000
    with pytest.raises(ValueError):
        mandate.commit("d1", 100)
    again = mandate.reserve(m, "d1", 400)
    assert again["status"] == "ok" and mandate.summary(m)["remaining_eur_cents"] == 2600


def test_commit_and_release_guard_against_bugs(m):
    with pytest.raises(ValueError):
        mandate.commit("nope", 1)
    with pytest.raises(ValueError):
        mandate.release("nope")
    mandate.reserve(m, "d1", 500)
    with pytest.raises(ValueError):
        mandate.commit("d1", 501)
    with pytest.raises(ValueError):
        mandate.commit("d1", -1)


def test_budget_refusal_from_reserve_is_journaled(m):
    for i in range(3):
        mandate.reserve(m, f"f{i}", 1000)
    d = mandate.reserve(m, "f3", 1)
    assert d["code"] == Code.BUDGET_EXCEEDED and d["explanation"]
    assert journal.entries()[0]["event"] == "budget_refused"
    assert mandate.summary("m-nope") is None
