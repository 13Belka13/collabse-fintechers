# Dispute timer (SPEC §9.6): a background thread in the bank process, every 5 s. It applies the silence
# defaults from dispute_terms (bank.disputes.apply_defaults), releases the holdback when its window is over,
# retries a pending capture/void under the same idem key, and raises an alarm in the /bank feed when an open
# deal is less than 1 h from authorizationExpiry. Every step is journalled with actor "timer" and the rule.
# tick(now) takes the clock as a parameter, so tests drive it with a fake clock.
import threading
import time

from bank import db, disputes, journal

INTERVAL_S = 5
ALARM_S = 3600
LOCKED_STATES = ("authorized", "delivered", "verified", "disputed", "settled")
_thread: threading.Thread | None = None
_stop = threading.Event()
_tick_lock = threading.Lock()


def tick(now: float | None = None) -> list[dict]:
    """One pass. Returns what it did: [{deal_id, ...applied}]. Two ticks at the same moment do nothing twice."""
    now = time.time() if now is None else now
    done = []
    with _tick_lock:
        # a deal deals.settle left 'verified' (items held for a human) gets its dispute card and deadlines
        for r in db.rows("SELECT deal_id FROM deals WHERE state='verified'"
                         " AND deal_id NOT IN (SELECT deal_id FROM disputes)"):
            disputes.open_(r["deal_id"], now=now)
            done.append({"deal_id": r["deal_id"], "opened": True})
        for deal_id in disputes.open_ids():
            for a in disputes.apply_defaults(deal_id, now=now):
                done.append({"deal_id": deal_id, **a})
        done += alarms(now)
    return done


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
