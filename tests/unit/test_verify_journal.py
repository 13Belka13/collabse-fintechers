# Journal tamper test (SPEC §19.2): scripts/verify_journal.py catches a changed, deleted or re-hashed row.
import hashlib

from bank import db, journal
from scripts import verify_journal


def _five():
    for i in range(5):
        journal.append("bank", "escrow_captured", {"amount": 225_000 * (i + 1), "n": i}, deal_id="d-tamper")


def test_intact_chain_passes(capsys):
    _five()
    assert verify_journal.main([]) == 0
    assert "5 entries, hash chain intact" in capsys.readouterr().out


def test_changed_payload_is_caught_at_that_row(capsys):
    _five()
    db.run("UPDATE journal SET payload_json=? WHERE id=3", (db.dumps({"amount": 1, "n": 2}),))
    assert verify_journal.main([]) == 1
    assert "BROKEN at row id 3" in capsys.readouterr().out


def test_rehashed_row_is_caught_at_the_next_row():
    """An attacker who also recomputes the changed row's own hash still breaks the link to the next row."""
    _five()
    row = db.one("SELECT * FROM journal WHERE id=2")
    payload = {"amount": 1, "n": 1}
    body = db.dumps({"ts": row["ts"], "actor": row["actor"], "event": row["event"], "deal_id": row["deal_id"],
                     "payload": payload})
    forged = hashlib.sha256((row["prev_hash"] + body).encode()).hexdigest()
    db.run("UPDATE journal SET payload_json=?, hash=? WHERE id=2", (db.dumps(payload), forged))
    assert journal.verify() == (False, 3)


def test_deleted_row_is_caught():
    _five()
    db.run("DELETE FROM journal WHERE id=4")
    assert journal.verify() == (False, 5)


def test_changed_actor_is_caught():
    _five()
    db.run("UPDATE journal SET actor='human:mallory' WHERE id=1")
    assert journal.verify() == (False, 1)


def test_missing_database_is_exit_2(tmp_path):
    assert verify_journal.main(["--db", str(tmp_path / "nope.db")]) == 2
