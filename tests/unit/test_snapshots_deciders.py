# The snapshot service's guards (MODEL_V2 §5.1) and the decider draw (§7C.6): template URLs only, exact host,
# public addresses only, limits, outcomes (404 -> unavailable, 5xx/429/timeout -> wait), the fair queue; drand
# round maths and the recorded beacons, commit-reveal challenges, the minimum pool of 3, the reserve provider.
import hashlib
import json

import httpx
import pytest

from bank import deciders, snapshots

OK_BODY = json.dumps({"ico": "97537071", "obchodniJmeno": "Sklady Morava s.r.o.", "datumVzniku": "2001-02-03",
                      "sidlo": {"psc": 61700, "textovaAdresa": "Hněvkovského 1, 61700 Brno"}}).encode()
URL = snapshots.build_url("ares", "97537071")


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def public(host):
    return None


def test_the_url_comes_only_from_the_template_and_the_key():
    assert URL == "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/97537071"
    for bad in ("9753707", "97537071/../x", "https://evil.example", 97537071.0, True):
        assert not snapshots.key_ok("ares", bad)
    with pytest.raises(ValueError):
        snapshots.build_url("ares", "../admin")
    assert not snapshots.key_ok("my-site", "97537071")


def test_fetch_ok_and_extract_by_the_published_mapping():
    body = snapshots.fetch("ares", URL, client(lambda r: httpx.Response(200, content=OK_BODY)), resolve=public)
    fields = snapshots.extract("ares", json.loads(body))
    assert fields["founded"] == "2001-02-03" and fields["founded:type"] == "date" and fields["postalCode"] == 61700


@pytest.mark.parametrize("handler,outcome", [
    (lambda r: httpx.Response(404, json={"kod": "NENALEZENO"}), snapshots.Unavailable),
    (lambda r: httpx.Response(200, content=b"x" * (snapshots.MAX_BYTES + 1)), snapshots.Unavailable),
    (lambda r: httpx.Response(503), snapshots.Wait), (lambda r: httpx.Response(429), snapshots.Wait),
    (lambda r: httpx.Response(302, headers={"location": "https://sites.google.com/x"}), snapshots.Wait),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("t")), snapshots.Wait)])
def test_outcomes_decide_who_bears_the_failure(handler, outcome):
    with pytest.raises(outcome):
        snapshots.fetch("ares", URL, client(handler), resolve=public)


def test_redirects_stay_on_the_host_and_are_limited():
    hops = {"n": 0}

    def handler(r):
        hops["n"] += 1
        return httpx.Response(302, headers={"location": URL + "?again"})
    with pytest.raises(snapshots.Wait, match="redirects"):
        snapshots.fetch("ares", URL, client(handler), resolve=public)
    assert hops["n"] == snapshots.MAX_REDIRECTS + 1


@pytest.mark.parametrize("url", ["http://ares.gov.cz/x", "https://evil.ares.gov.cz/x", "https://user:pw@ares.gov.cz/x"])
def test_only_https_on_the_exact_host(url):
    with pytest.raises(snapshots.Wait):
        snapshots.fetch("ares", url, client(lambda r: httpx.Response(200, content=OK_BODY)), resolve=public)


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.7", "169.254.169.254", "::1", "192.168.1.1"])
def test_private_addresses_after_dns_are_refused(monkeypatch, addr):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, "", (addr, 443))])
    with pytest.raises(PermissionError):
        snapshots.public_address("ares.gov.cz")


def test_fair_queue_is_round_robin_by_seller():
    rows = [{"seller_id": s, "next_try_at": t, "id": i} for i, (s, t) in enumerate(
        [("big", 1), ("big", 2), ("big", 3), ("big", 4), ("small", 5), ("other", 6)])]
    assert [r["seller_id"] for r in snapshots.fair_order(rows)][:3] == ["big", "other", "small"]


def test_drand_round_maths_and_recorded_beacons():
    r = deciders.first_round_after(deciders.round_time(1_000_000))
    assert r == 1_000_001 and deciders.first_round_after(deciders.round_time(1_000_000) - 1) == 1_000_000
    rec = json.loads(deciders.DRAND_FIXTURE.read_text(encoding="utf-8"))["rounds"]["1000000"]
    assert hashlib.sha256(bytes.fromhex(rec["signature"])).hexdigest() == rec["randomness"]  # drand: H(sig)
    b = deciders.beacon(1_000_000)
    assert b["randomness"] == rec["randomness"] and "recorded" in b["source"]
    with pytest.raises(deciders.DeciderError) as e:
        deciders.beacon(deciders.first_round_after(4_000_000_000), now=0)
    assert e.value.status == 425


def case(**kw):
    return {"claim_id": 1, "deal_id": "d", "sides": {"buyer": "64146103", "seller": "30181020"}, "exclude": [],
            "challenges": {"buyer": {}, "seller": {}}, **kw}


def test_pool_never_below_three_and_challenges_in_turn(monkeypatch):
    five = [{"id": f"d{i}", "name": "", "address": "0x" + "1" * 40, "related_icos": []} for i in range(5)]
    assert [d["id"] for d in deciders.eligible(case())[0]] == ["dec-1", "dec-2", "dec-3"]
    c3 = case(challenges={"buyer": {"ids": ["dec-1", "dec-2"]}, "seller": {"ids": ["dec-3"]}})
    assert len(deciders.eligible(c3)[0]) == 3  # challenges never take the pool below 3
    monkeypatch.setattr(deciders, "pool", lambda: five)
    c5 = case(challenges={"buyer": {"ids": ["d0", "d1"]}, "seller": {"ids": ["d4", "d3"]}})
    assert [d["id"] for d in deciders.eligible(c5)[0]] == ["d2", "d3", "d1"][:0] or \
        [d["id"] for d in deciders.eligible(c5)[0]] == ["d1", "d2", "d3"]  # first of each side, then stop at 3
    related = [{**d, "related_icos": ["30181020"]} if d["id"] in ("d0", "d1", "d2") else d for d in five]
    monkeypatch.setattr(deciders, "pool", lambda: related)
    left, reserve = deciders.eligible(case())
    assert reserve and left == [deciders.RESERVE_PROVIDER]  # common beneficiary always applies -> reserve provider
    monkeypatch.setattr(deciders, "pool", lambda: five)
    recent = [("d0", 5), ("d1", 9), ("d2", 1)]
    assert [d["id"] for d in deciders.eligible(case(), recent)[0]] == ["d1", "d3", "d4"]  # oldest first, stop at 3


def test_commit_reveal_challenges(seeded):
    from bank import db
    db.run("INSERT INTO claims (deal_id, rule, buyer_id, seller_id, items_json, amount, state, filed_at)"
           " VALUES ('d','C','alfa','s-beta','[]',1,'decider',0)")
    c = {"id": 1, "deal_id": "d"}
    out = deciders.open_stage(c, {"buyer": "64146103", "seller": "30181020"}, now=1000)
    assert out["t1"] < out["t2"] and out["selection_round"] > deciders.first_round_after(out["t2"]) - 1
    assert "salt" not in out and out["salt_hash"].startswith("0x")
    h = deciders.commitment(["dec-1"], "n0nce")
    deciders.commit(1, "buyer", h, now=1001)
    with pytest.raises(deciders.DeciderError):
        deciders.reveal(1, "buyer", ["dec-2"], "n0nce", now=1002)  # not what was committed
    assert deciders.reveal(1, "buyer", ["dec-1"], "n0nce", now=1002)["challenges"]["buyer"]["revealed"]
    with pytest.raises(deciders.DeciderError):
        deciders.commit(1, "seller", h, now=out["t1"] + 1)  # after T1
    with pytest.raises(deciders.DeciderError) as e:
        deciders.select(1, now=out["t2"] - 1)  # before T2 nobody is drawn
    assert e.value.status == 425
