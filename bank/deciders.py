# External deciders for rule C (MODEL_V2 §7C.6–8): appointment that nobody can steer.
# - Pool: every accredited decider minus objective challenges, never below 3: "common beneficiary with a side" is
#   always applied (if fewer than 3 remain, the case goes to the reserve provider; C is not cancelled), then "cases
#   with the same side within 90 days" oldest first while the pool stays >= 3 (off in the demo), then the parties'
#   own challenges in turn (first of each, then second) while the pool stays >= 3.
# - Schedule fixed when the stage opens and journalled with the hash of the bank's secret salt s: T1 = +24 h
#   (challenge hashes), T2 = T1 + 24 h (reveals), selection round = first drand round after T2, sample round = first
#   round after T2 + 48 h. Round numbers follow from time, so nobody picks the round.
# - Challenges are commit-reveal: a hash before T1, the list and nonce before T2; no reveal or a mismatch -> that
#   side's challenges are not applied.
# - Selection: H(drand[selection round] ‖ s) mod |pool|; sample: items ordered by H(drand[sample round] ‖ s ‖ i).
#   s is revealed after the decision, so anyone can recheck the draw; the decider's identity is hidden from the
#   parties until then, and the decider sees pseudonyms only.
# - The decider decides within 48 h of the sample round, per sampled item; the result extends to the share of D.
# - Re-review paid by the loser (§7C.6): two NEW deciders from the pool without the first (fewer than two left ->
#   the reserve provider alone), a new salt s' committed when the step opens, its own schedule and selection round;
#   they decide the same sampled items, the result is the majority of three per item. s' is revealed at the end.
# - Audit: H(drand[sample round] ‖ s ‖ "audit") mod 100 < 5 sends the decision to another decider for a check; s is
#   secret until the decision, so the decider cannot know. A divergence is journalled (a penalty under the decider's
#   contract with the bank); the parties' outcome does not change.
# drand: quicknet (3 s rounds). Live rounds from api.drand.sh; recorded rounds in fixtures/drand/quicknet.json;
# in MOCK a round that is not recorded gets a labelled deterministic stand-in (never presented as the real beacon).
import hashlib
import json
import secrets
import time

import httpx

from bank import config, db, journal, receipts

DRAND_FIXTURE = config.ROOT / "fixtures" / "drand" / "quicknet.json"
DRAND_HASH = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
DRAND_GENESIS, DRAND_PERIOD = 1692803367, 3
HOUR = 3600
MIN_POOL, SAMPLE_K = 3, 5
REREVIEW_N, AUDIT_PCT = 2, 5
RESERVE_PROVIDER = {"id": "reserve", "name": "Reserve dispute-resolution provider (contract with the bank)",
                    "address": "0x000000000000000000000000000000000000dEC0", "related_icos": []}


class DeciderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def pool() -> list[dict]:
    """Accredited deciders. Demo: 3 demo deciders whose keys a person on stage controls (§14) — said aloud."""
    from bank import evsm
    return [{"id": f"dec-{i}", "name": f"Demo decider {i}", "address": f"0x{'dec0':0>36}{i:04x}",
             "related_icos": sorted(evsm.decider_icos(f"dec-{i}"))} for i in (1, 2, 3)]


# ---- drand ----

def round_time(r: int) -> int:
    return DRAND_GENESIS + (r - 1) * DRAND_PERIOD


def first_round_after(t: float) -> int:
    """The first round whose time is strictly after t."""
    r = int((t - DRAND_GENESIS) // DRAND_PERIOD) + 1
    while round_time(r) <= t:
        r += 1
    return r


def _recorded() -> dict:
    return json.loads(DRAND_FIXTURE.read_text(encoding="utf-8"))["rounds"]


def beacon(r: int, now: float | None = None) -> dict:
    """{"round", "randomness", "source"}. A future round raises DeciderError(425)."""
    now = time.time() if now is None else now
    rec = _recorded().get(str(r))
    if rec:
        return {"round": r, "randomness": rec["randomness"], "source": "drand quicknet (recorded)"}
    if round_time(r) > now:
        raise DeciderError(425, f"drand round {r} is not out yet")
    if config.MOCK:
        fake = hashlib.sha256(f"MOCK drand quicknet round {r}".encode()).hexdigest()
        return {"round": r, "randomness": fake, "source": "MOCK stand-in (not the real beacon)"}
    out = httpx.get(f"https://api.drand.sh/{DRAND_HASH}/public/{r}", timeout=5).json()
    if hashlib.sha256(bytes.fromhex(out["signature"])).hexdigest() != out["randomness"]:
        raise DeciderError(502, "drand randomness is not sha256(signature)")
    return {"round": r, "randomness": out["randomness"], "source": "drand quicknet (api.drand.sh)"}


def draw(randomness: str, salt: str, extra: bytes = b"") -> int:
    """H(drand ‖ s [‖ extra]) as an integer."""
    return int.from_bytes(hashlib.sha256(bytes.fromhex(randomness) + bytes.fromhex(salt) + extra).digest(), "big")


# ---- the case ----

def get(claim_id: int) -> dict | None:
    row = db.one("SELECT data_json FROM decider_cases WHERE claim_id=?", (claim_id,))
    return db.loads(row["data_json"]) if row else None


def _save(case: dict):
    db.run("INSERT INTO decider_cases (claim_id, data_json) VALUES (?,?) ON CONFLICT(claim_id) DO UPDATE SET"
           " data_json=excluded.data_json", (case["claim_id"], db.dumps(case)))


def schedule(now: float) -> dict:
    t1 = now + 24 * HOUR / config.TIME_DIVISOR
    t2 = t1 + 24 * HOUR / config.TIME_DIVISOR
    return {"opened_at": now, "t1": t1, "t2": t2, "selection_round": first_round_after(t2),
            "sample_round": first_round_after(t2 + 48 * HOUR / config.TIME_DIVISOR)}


def open_stage(claim: dict, sides: dict, now: float | None = None, exclude: tuple = (), step: str = "first") -> dict:
    """Both fees are in: fix the schedule and commit to a fresh secret salt (its hash goes to the journal; on a
    chain-backed deployment, also to the network). sides: {"buyer": ico, "seller": ico}."""
    now = time.time() if now is None else now
    salt = secrets.token_hex(32)
    case = {"claim_id": claim["id"], "deal_id": claim["deal_id"], "step": step, "sides": sides,
            "salt": salt, "salt_hash": "0x" + hashlib.sha256(bytes.fromhex(salt)).hexdigest(),
            **schedule(now), "challenges": {"buyer": {}, "seller": {}}, "exclude": list(exclude),
            "decider": None, "sample": None, "verdicts": None, "decided_at": None, "salt_revealed": False}
    _save(case)
    journal.append("bank", "decider.stage_opened", {k: case[k] for k in ("claim_id", "step", "salt_hash", "t1", "t2",
                                                                       "selection_round", "sample_round")},
                   deal_id=claim["deal_id"])
    return public(case)


def public(case: dict) -> dict:
    """What a party sees: never the salt before the decision, never the decider's identity before the decision."""
    out = {k: v for k, v in case.items() if k not in ("salt", "decider")}
    out["decider_assigned"] = case["decider"] is not None
    if case["salt_revealed"]:
        out["salt"], out["decider"] = case["salt"], case["decider"]
    out["challenges"] = {p: {"committed": bool(c.get("commit")), "revealed": bool(c.get("ids") is not None)}
                         for p, c in case["challenges"].items()}
    if case.get("rereview"):
        rr = case["rereview"]
        out["rereview"] = {k: v for k, v in rr.items() if rr.get("salt_revealed") or k not in ("salt", "deciders")}
        out["rereview"]["verdicts"] = len(rr["verdicts"])
    if case.get("audit"):
        out["audit"] = {"done": case["audit"]["verdicts"] is not None}
    return out


def commitment(ids: list[str], nonce: str) -> str:
    return receipts.hex32(receipts.sha256(receipts.jcs({"deciders": list(ids), "nonce": str(nonce)})))


def commit(claim_id: int, party: str, h: str, now: float | None = None) -> dict:
    now, case = time.time() if now is None else now, _case(claim_id)
    if party not in ("buyer", "seller") or now > case["t1"]:
        raise DeciderError(409, "challenge hashes are taken until T1")
    case["challenges"][party] = {"commit": h}
    _save(case)
    journal.append(party, "decider.challenge_committed", {"claim_id": claim_id, "hash": h}, deal_id=case["deal_id"])
    return public(case)


def reveal(claim_id: int, party: str, ids: list[str], nonce: str, now: float | None = None) -> dict:
    now, case = time.time() if now is None else now, _case(claim_id)
    c = case["challenges"].get(party) or {}
    if now > case["t2"] or not c.get("commit"):
        raise DeciderError(409, "reveals are taken until T2, after a committed hash")
    if commitment(ids, nonce) != c["commit"] or len(ids) > 2:
        raise DeciderError(400, "the reveal does not match the committed hash (or names more than 2)")
    c["ids"] = list(ids)
    _save(case)
    journal.append(party, "decider.challenge_revealed", {"claim_id": claim_id, "count": len(ids)},
                   deal_id=case["deal_id"])
    return public(case)


def _case(claim_id: int) -> dict:
    case = get(claim_id)
    if case is None:
        raise DeciderError(404, "no decider stage for this claim")
    return case


def eligible(case: dict, recent: list[tuple[str, float]] | None = None) -> tuple[list[dict], bool]:
    """(the pool after challenges, whether the reserve provider takes the case). recent: [(decider_id, case time)]
    of cases with the same side in 90 days — off in the demo (a second run would challenge the first decider)."""
    from bank import evsm
    sides = set().union(*(evsm.related_icos(i) for i in case["sides"].values()))  # a side and its owners' firms
    left = [d for d in pool() if d["id"] not in case["exclude"] and not sides & set(d["related_icos"])]
    if len(left) < MIN_POOL:
        return [RESERVE_PROVIDER], True
    for did, _ in sorted(recent or [], key=lambda x: x[1]):
        if len(left) > MIN_POOL and any(d["id"] == did for d in left):
            left = [d for d in left if d["id"] != did]
    revealed = {p: list(c.get("ids") or []) for p, c in case["challenges"].items()}
    for turn in range(2):  # first challenge of each side, then the second
        for party in ("buyer", "seller"):
            ids = revealed[party]
            if turn < len(ids) and len(left) > MIN_POOL and any(d["id"] == ids[turn] for d in left):
                left = [d for d in left if d["id"] != ids[turn]]
    return left, False


def select(claim_id: int, now: float | None = None) -> dict:
    """After the selection round is out: the decider by H(drand ‖ s). Idempotent."""
    now, case = time.time() if now is None else now, _case(claim_id)
    if case["decider"]:
        return public(case)
    if now <= case["t2"]:
        raise DeciderError(425, "the challenge reveals run until T2")
    b = beacon(case["selection_round"], now)
    left, reserve = eligible(case)
    chosen = left[draw(b["randomness"], case["salt"]) % len(left)]
    case.update(decider=chosen["id"], selection_beacon=b, pool_size=len(left), reserve=reserve)
    _save(case)
    journal.append("bank", "decider.selected", {"claim_id": claim_id, "round": b["round"], "beacon": b["source"],
                                                "pool_size": len(left), "reserve": reserve},
                   deal_id=case["deal_id"])  # the identity stays out of the journal until the decision
    return public(case)


def sample(claim_id: int, n_items: int, now: float | None = None, k: int = SAMPLE_K) -> list[int]:
    """After the sample round: indexes of the disputed items the decider checks (all if there are at most k)."""
    now, case = time.time() if now is None else now, _case(claim_id)
    if case["sample"] is not None:
        return case["sample"]
    b = beacon(case["sample_round"], now)
    order = sorted(range(n_items), key=lambda i: draw(b["randomness"], case["salt"], i.to_bytes(4, "big")))
    case.update(sample=sorted(order[:k]), sample_beacon=b)
    _save(case)
    journal.append("bank", "decider.sampled", {"claim_id": claim_id, "round": b["round"], "items": case["sample"]},
                   deal_id=case["deal_id"])
    return case["sample"]


def deadline(case: dict) -> float:
    return round_time(case["sample_round"]) + 48 * HOUR / config.TIME_DIVISOR


def decide(claim_id: int, decider_id: str, verdicts: dict, now: float | None = None) -> dict:
    """The decider's conclusion per sampled item: {index: "buyer" | "seller"}. Returns the buyer's share of D."""
    now, case = time.time() if now is None else now, _case(claim_id)
    if case["decided_at"]:
        raise DeciderError(409, "already decided")
    if decider_id != case["decider"]:
        raise DeciderError(403, "only the assigned decider decides this case")
    if case["sample"] is None:
        raise DeciderError(425, "the sample is not drawn yet")
    if now > deadline(case):
        raise DeciderError(409, "late: the case goes to another decider")
    got = {int(k): v for k, v in verdicts.items()}
    if set(got) != set(case["sample"]) or not set(got.values()) <= {"buyer", "seller"}:
        raise DeciderError(400, "a conclusion for every sampled item: buyer or seller")
    upheld = sum(v == "buyer" for v in got.values())
    case.update(verdicts=got, decided_at=now, upheld=upheld, of=len(got), salt_revealed=True)
    if audit_due(case):
        left = [d for d in eligible({**case, "exclude": case["exclude"] + [decider_id]})[0]]
        auditor = left[draw(case["sample_beacon"]["randomness"], case["salt"], b"auditor") % len(left)]
        case["audit"] = {"decider": auditor["id"], "verdicts": None}
    _save(case)
    journal.append(f"decider:{decider_id}", "decider.decided",
                   {"claim_id": claim_id, "upheld": upheld, "of": len(got), "salt": case["salt"],
                    "decider": decider_id, "audit": bool(case.get("audit"))}, deal_id=case["deal_id"])
    return public(case)


def _verdicts(case: dict, verdicts: dict) -> dict:
    got = {int(k): v for k, v in verdicts.items()}
    if set(got) != set(case["sample"]) or not set(got.values()) <= {"buyer", "seller"}:
        raise DeciderError(400, "a conclusion for every sampled item: buyer or seller")
    return got


# ---- the random 5 % audit ----

def audit_due(case: dict) -> bool:
    return draw(case["sample_beacon"]["randomness"], case["salt"], b"audit") % 100 < AUDIT_PCT


def audit(claim_id: int, decider_id: str, verdicts: dict, now: float | None = None) -> dict:
    """The auditing decider's check of a decision: the items where it differs are journalled; money is untouched."""
    now, case = time.time() if now is None else now, _case(claim_id)
    a = case.get("audit")
    if not a or a["verdicts"] is not None:
        raise DeciderError(409, "no audit is due on this case")
    if decider_id != a["decider"]:
        raise DeciderError(403, "only the auditing decider checks this case")
    got = _verdicts(case, verdicts)
    first = {int(i): v for i, v in case["verdicts"].items()}  # JSON keys are strings
    diverged = sorted(i for i in got if got[i] != first[i])
    a.update(verdicts=got, decided_at=now, diverged=diverged)
    _save(case)
    journal.append(f"decider:{decider_id}", "decider.audited",
                   {"claim_id": claim_id, "diverged": len(diverged), "of": len(got),
                    "consequence": "penalty under the decider's contract" if diverged else "none"},
                   deal_id=case["deal_id"])
    return public(case)


# ---- re-review paid by the loser ----

def open_rereview(claim_id: int, now: float | None = None) -> dict:
    """Commit to a new salt s' and fix the step's schedule; the first decider is out of the pool."""
    now, case = time.time() if now is None else now, _case(claim_id)
    if not case["decided_at"] or case.get("rereview"):
        raise DeciderError(409, "a re-review follows one first decision")
    salt = secrets.token_hex(32)
    case["rereview"] = {"salt": salt, "salt_hash": "0x" + hashlib.sha256(bytes.fromhex(salt)).hexdigest(),
                        **schedule(now), "deciders": None, "verdicts": {}, "decided_at": None}
    _save(case)
    rr = case["rereview"]
    journal.append("bank", "decider.rereview_opened", {"claim_id": claim_id, "salt_hash": rr["salt_hash"],
                                                       "t2": rr["t2"], "selection_round": rr["selection_round"]},
                   deal_id=case["deal_id"])
    return public(case)


def select_rereview(claim_id: int, now: float | None = None) -> list[str]:
    """After the step's selection round: two new deciders by H(drand ‖ s' ‖ id) order. Idempotent."""
    now, case = time.time() if now is None else now, _case(claim_id)
    rr = case["rereview"]
    if rr["deciders"]:
        return rr["deciders"]
    if now <= rr["t2"]:
        raise DeciderError(425, "the re-review draw waits for its schedule")
    b = beacon(rr["selection_round"], now)
    sides = {"buyer": {}, "seller": {}}
    left, reserve = eligible({**case, "exclude": case["exclude"] + [case["decider"]], "challenges": sides})
    if reserve or len(left) < REREVIEW_N:
        chosen = [RESERVE_PROVIDER["id"]]
    else:
        chosen = [d["id"] for d in sorted(left, key=lambda d: draw(b["randomness"], rr["salt"], d["id"].encode()))]
        chosen = chosen[:REREVIEW_N]
    rr.update(deciders=chosen, selection_beacon=b)
    _save(case)
    journal.append("bank", "decider.rereview_selected", {"claim_id": claim_id, "round": b["round"],
                                                         "count": len(chosen)}, deal_id=case["deal_id"])
    return chosen


def decide_rereview(claim_id: int, decider_id: str, verdicts: dict, now: float | None = None) -> dict | None:
    """One new decider's conclusion on the same sampled items. When all are in: the majority of three per item
    (the reserve provider alone decides when the pool was too small). Returns the final result or None."""
    now, case = time.time() if now is None else now, _case(claim_id)
    rr = case.get("rereview") or {}
    if not rr.get("deciders") or rr["decided_at"]:
        raise DeciderError(409, "no re-review decision is due")
    if decider_id not in rr["deciders"] or decider_id in rr["verdicts"]:
        raise DeciderError(403, "only a re-review decider decides, once")
    if now > deadline({"sample_round": rr["selection_round"]}):
        raise DeciderError(409, "late: the re-review decider missed the 48 h")
    rr["verdicts"][decider_id] = {str(i): v for i, v in _verdicts(case, verdicts).items()}
    final = None
    if set(rr["verdicts"]) == set(rr["deciders"]):
        votes = [{int(i): v for i, v in x.items()} for x in [case["verdicts"], *rr["verdicts"].values()]]
        if rr["deciders"] == [RESERVE_PROVIDER["id"]]:
            votes = votes[1:]
        out = {i: max(("buyer", "seller"), key=lambda side: sum(v[i] == side for v in votes)) for i in case["sample"]}
        upheld = sum(v == "buyer" for v in out.values())
        rr.update(decided_at=now, final=out, upheld=upheld, of=len(out), salt_revealed=True)
        final = {"upheld": upheld, "of": len(out), "changed": upheld != case["upheld"]}
    _save(case)
    journal.append(f"decider:{decider_id}", "decider.rereview_decided",
                   {"claim_id": claim_id, **(final or {}), **({"salt": rr["salt"]} if final else {})},
                   deal_id=case["deal_id"])
    return final


def replace_late(claim_id: int, now: float | None = None) -> dict | None:
    """A decider who missed the 48 h is replaced through a new draw with a new salt (§7C.8); money stays put."""
    now, case = time.time() if now is None else now, _case(claim_id)
    if case["decided_at"] or case["decider"] is None or case["sample"] is None or now <= deadline(case):
        return None
    late = case["decider"]
    journal.append("bank", "decider.late", {"claim_id": claim_id}, deal_id=case["deal_id"])
    return open_stage({"id": claim_id, "deal_id": case["deal_id"]}, case["sides"], now,
                      exclude=tuple(case["exclude"]) + (late,), step="replacement")
