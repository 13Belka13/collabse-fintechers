# Verify the journal's hash chain (SPEC §12, §19.2): every row's hash = sha256(prev_hash || canonical entry) and
# links to the row before it. A changed, deleted or re-hashed row breaks the chain at that row or the next one.
#   python -m scripts.verify_journal [--db data/bank.db]
# Exit code 0 = intact, 1 = broken (prints the first bad row id), 2 = no such database.
import argparse
import os
import sys
from pathlib import Path

from bank import config, db, journal


def check(db_path: str | None = None) -> dict:
    if db_path:
        os.environ["DB_PATH"] = db_path
        config.reload()
        db.close()
    n = db.one("SELECT COUNT(*) n FROM journal")["n"]
    ok, bad = journal.verify()
    return {"db": config.DB_PATH, "entries": n, "ok": ok, "first_bad_id": bad}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify the bank journal's hash chain")
    ap.add_argument("--db", default=None, help="SQLite path (default: DB_PATH from .env)")
    a = ap.parse_args(argv)
    if a.db and not Path(a.db).exists():
        print(f"no database at {a.db}")
        return 2
    r = check(a.db)
    if r["ok"]:
        print(f"journal OK: {r['entries']} entries, hash chain intact ({r['db']})")
        return 0
    print(f"journal BROKEN at row id {r['first_bad_id']} of {r['entries']} ({r['db']})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
