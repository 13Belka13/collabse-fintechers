# Hash-chained journal (SPEC §12): hash = sha256(prev_hash || canonical_json(entry)).
# actor: "bank" | "timer" | "human:<who>" | "agent:buyer" | ...; instructions that came from a
# counterparty carry payload["source"] = "untrusted:seller".
import hashlib
import time

from bank import db

GENESIS = "0" * 64


def _entry_hash(prev_hash: str, ts: float, actor: str, event: str, deal_id, payload: dict) -> str:
    body = db.dumps({"ts": ts, "actor": actor, "event": event, "deal_id": deal_id, "payload": payload})
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


def append(actor: str, event: str, payload: dict | None = None, deal_id: str | None = None) -> dict:
    payload = payload or {}
    with db.immediate() as c:
        last = c.execute("SELECT hash FROM journal ORDER BY id DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        ts = round(time.time(), 6)
        h = _entry_hash(prev, ts, actor, event, deal_id, payload)
        cur = c.execute("INSERT INTO journal (ts, actor, event, deal_id, payload_json, prev_hash, hash)"
                        " VALUES (?,?,?,?,?,?,?)", (ts, actor, event, deal_id, db.dumps(payload), prev, h))
    return {"id": cur.lastrowid, "ts": ts, "actor": actor, "event": event, "deal_id": deal_id,
            "payload": payload, "prev_hash": prev, "hash": h}


def verify() -> tuple[bool, int | None]:
    """Recompute the chain. Returns (True, None) or (False, id of the first bad row)."""
    prev = GENESIS
    for r in db.rows("SELECT * FROM journal ORDER BY id"):
        expected = _entry_hash(prev, r["ts"], r["actor"], r["event"], r["deal_id"], db.loads(r["payload_json"]))
        if r["prev_hash"] != prev or r["hash"] != expected:
            return False, r["id"]
        prev = r["hash"]
    return True, None


def entries(deal_id: str | None = None, limit: int = 200) -> list[dict]:
    if deal_id:
        rows = db.rows("SELECT * FROM journal WHERE deal_id=? ORDER BY id DESC LIMIT ?", (deal_id, limit))
    else:
        rows = db.rows("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["payload"] = db.loads(r.pop("payload_json"))
    return rows
