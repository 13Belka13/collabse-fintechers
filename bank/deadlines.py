# Deal timer (MODEL_V2 §8.2): a background thread in the bank process, every 5 s. For every open settlement it
# rechecks items waiting for a registry or a snapshot and lets the money follow the rule's schedule
# (bank.disputes.tick: capture at acceptance / deadline / window end, the final void), retries a pending
# capture/void under the same idem key, and raises an alarm in the /bank feed when an open deal is less than 1 h
# from authorizationExpiry. Every step is journalled with actor "timer" and the rule.
# The delivery deadline moves by the time items waited for a registry or a source snapshot (§5.1, §5.2 p. 9): the
# seller cannot replace an item before the bank has decided it. Capped at 5 business days — after that a person of
# the buyer decides the item (§5.1), and the escrow's authorizationExpiry (deadline + window + 30 days) stays ahead.
# One deal that raises never stops the others: its failure is journalled (timer.deal_failed, bank-internal detail)
# and after STUCK_AFTER failures in a row the deal raises one settlement.stuck alarm on /bank for a person.
# tick(now) takes the clock as a parameter, so tests drive it with a fake clock.
import threading
import time

from bank import config, db, disputes, journal

INTERVAL_S = 5
ALARM_S = 3600
MAX_WAIT_BD = 5
LOCKED_STATES = ("authorized", "delivered", "dispute_c")
STUCK_AFTER = 3
_fails: dict[str, int] = {}  # deal_id -> consecutive failed ticks (this process)
_thread: threading.Thread | None = None
_stop = threading.Event()
_tick_lock = threading.Lock()


def tick(now: float | None = None) -> list[dict]:
    """One pass. Returns what it did: [{deal_id, ...applied}]. Two ticks at the same moment do nothing twice."""
    now = time.time() if now is None else now
    done = []
    with _tick_lock:
        try:
            from bank import snapshots
            snapshots.drain(now)  # waiting source snapshots, fairly by seller (§5.1)
        except Exception as e:  # noqa: BLE001 — a source outage only keeps items waiting
            journal.append("timer", "snapshot.drain_failed", {"error": repr(e)[:200]})
        for deal_id in disputes.open_ids():
            before = journal.entries(deal_id, limit=1)
            try:
                disputes.tick(deal_id, now=now)
            except Exception as e:  # noqa: BLE001 — the next deal still settles
                done += _failed(deal_id, e)
                continue
            _fails.pop(deal_id, None)
            after = journal.entries(deal_id, limit=1)
            if after and (not before or after[0]["id"] != before[0]["id"]):
                done.append({"deal_id": deal_id, "event": after[0]["event"]})
        try:
            from bank import claims_c
            claims_c.retry_transfers()  # fee shares and decider pay owed by decided C claims (§7C.4)
        except Exception as e:  # noqa: BLE001 — journalled; the next pass retries
            journal.append("timer", "clearing.retry_failed", {"error": repr(e)[:200]})
        done += alarms(now)
    return done


def _failed(deal_id: str, e: Exception) -> list[dict]:
    n = _fails[deal_id] = _fails.get(deal_id, 0) + 1
    journal.append("timer", "timer.deal_failed", {"attempt": n, "error": f"{type(e).__name__}: {str(e)[:200]}"},
                   deal_id=deal_id)
    if n < STUCK_AFTER or db.one("SELECT 1 FROM journal WHERE deal_id=? AND event='settlement.stuck'", (deal_id,)):
        return []
    journal.append("timer", "settlement.stuck", {"attempts": n, "rule": f"{STUCK_AFTER} failed timer runs in a row: "
                                                 "the money waits for a person"}, deal_id=deal_id)
    return [{"deal_id": deal_id, "alarm": "settlement.stuck"}]


def waited(deal_id: str, now: float) -> float:
    """Seconds the deal's items spent waiting for a registry or a snapshot: the union of [received_at, decided_at or
    now] over items that waited (decided later than received, or still pending). Items accepted anyway by a person
    are left out: their decided_at is the waiver, not the end of a wait."""
    total, end = 0.0, None
    for r in db.rows("SELECT received_at a, COALESCE(decided_at, ?) b FROM items WHERE deal_id=? AND waived_by IS NULL"
                     " AND (status='pending' OR decided_at>received_at) ORDER BY received_at", (now, deal_id)):
        a, b = r["a"], max(r["a"], min(r["b"], now))
        if end is None or a > end:
            total, end = total + b - a, b
        elif b > end:
            total, end = total + b - end, b
    return min(total, MAX_WAIT_BD * config.BUSINESS_DAY_S)


def delivery_deadline(s: dict, now: float) -> float:
    """The settlement's delivery deadline with the waiting time added."""
    return s["deadline_at"] + waited(s["deal_id"], now)


def alarms(now: float) -> list[dict]:
    """Once per deal: money still locked and authorizationExpiry within ALARM_S (§9.6)."""
    out = []
    marks = ",".join("?" * len(LOCKED_STATES))
    for deal in db.rows(f"SELECT deal_id, payment_info_json FROM deals WHERE state IN ({marks})"
                        " AND payment_info_json IS NOT NULL", LOCKED_STATES):
        expiry = int((db.loads(deal["payment_info_json"]) or {}).get("authorizationExpiry") or 0)
        if not expiry or expiry - now >= ALARM_S:
            continue
        if db.one("SELECT 1 FROM journal WHERE deal_id=? AND event='escrow_expiry_alarm'", (deal["deal_id"],)):
            continue
        left = max(0, int(expiry - now))
        journal.append("timer", "escrow_expiry_alarm", {"authorization_expiry": expiry, "seconds_left": left,
                                                        "rule": "authorizationExpiry - now < 1 h, deal open"},
                       deal_id=deal["deal_id"])
        out.append({"deal_id": deal["deal_id"], "alarm": left})
    return out


def _loop(interval: float):
    while not _stop.wait(interval):
        try:
            tick()
        except Exception as e:  # noqa: BLE001 — the timer must survive one bad deal; the error is journalled
            try:
                journal.append("timer", "timer_error", {"error": repr(e)[:300]})
            except Exception:  # noqa: BLE001
                print("deadlines: tick failed:", repr(e), flush=True)


def start(interval: float = INTERVAL_S) -> bool:
    """Start the timer thread once per process. Returns False if it was already running."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(interval,), name="dispute-timer", daemon=True)
    _thread.start()
    return True


def stop():
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=INTERVAL_S + 1)


def running() -> bool:
    return _thread is not None and _thread.is_alive()
