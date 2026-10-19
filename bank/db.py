# One SQLite for the whole bank (SPEC §12). WAL mode; one connection per thread.
# Timestamps are unix seconds (REAL). Money is integer atomic units of the deal asset;
# mandate/reservation amounts are integer euro cents.
import json
import sqlite3
import threading
from pathlib import Path

from bank import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, ico TEXT UNIQUE NOT NULL, dic TEXT,
  account_number TEXT, iban TEXT, wallet_address TEXT, wallet_key_ref TEXT, role TEXT NOT NULL,
  synthetic INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS mandates (
  mandate_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, current_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mandate_versions (
  mandate_id TEXT NOT NULL, version INTEGER NOT NULL, policy_json TEXT NOT NULL,
  policy_hash TEXT NOT NULL, approved_at REAL NOT NULL, approved_by TEXT,
  PRIMARY KEY (mandate_id, version));
CREATE TABLE IF NOT EXISTS reservations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mandate_id TEXT NOT NULL, mandate_version INTEGER,
  deal_id TEXT NOT NULL, amount INTEGER NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL,
  updated_at REAL, committed INTEGER NOT NULL DEFAULT 0, UNIQUE (mandate_id, deal_id));
CREATE INDEX IF NOT EXISTS reservations_mandate ON reservations(mandate_id, state);
CREATE TABLE IF NOT EXISTS sellers (
  seller_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, category TEXT, profile_url TEXT,
  base_url TEXT, website_url TEXT);
CREATE TABLE IF NOT EXISTS deals (
  deal_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, seller_id TEXT NOT NULL, mandate_id TEXT,
  task_json TEXT, offer_json TEXT, quote_json TEXT, criteria_json TEXT, asset TEXT,
  amount INTEGER, payment_info_json TEXT, payment_hash TEXT, state TEXT NOT NULL,
  idem_key TEXT UNIQUE, created_at REAL NOT NULL, updated_at REAL);
CREATE TABLE IF NOT EXISTS screenings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, source TEXT NOT NULL,
  result_json TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS screenings_subject ON screenings(subject, source, fetched_at);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, deal_id TEXT, kind TEXT NOT NULL, outcome TEXT NOT NULL,
  codes_json TEXT, evidence_json TEXT, explanation_client TEXT, explanation_compliance TEXT,
  ms REAL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (
  deal_id TEXT PRIMARY KEY, items_json TEXT, rule_results_json TEXT, judge_flags_json TEXT,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS disputes (
  deal_id TEXT PRIMARY KEY, items_json TEXT, rejection_criteria_json TEXT, seller_statement TEXT,
  step TEXT, step_deadline_at REAL, decision TEXT, decided_by TEXT, default_rule TEXT,
  decided_at REAL, terms_json TEXT, captured INTEGER NOT NULL DEFAULT 0, capture_n INTEGER NOT NULL DEFAULT 0,
  holdback INTEGER NOT NULL DEFAULT 0, holdback_until REAL, holdback_released INTEGER NOT NULL DEFAULT 0,
  voided INTEGER NOT NULL DEFAULT 0, pending_json TEXT, opened_at REAL, closed_at REAL);
CREATE TABLE IF NOT EXISTS onchain_txs (
  tx_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, deal_id TEXT, status TEXT NOT NULL,
  idem_key TEXT UNIQUE, sender TEXT, nonce INTEGER, raw_tx TEXT, block_number INTEGER, gas_used INTEGER,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notifications (
  tx_id TEXT UNIQUE NOT NULL, payload_json TEXT NOT NULL, verified INTEGER NOT NULL,
  received_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL,
  deal_id TEXT, payload_json TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS timings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, deal_id TEXT, stage TEXT NOT NULL,
  ms REAL NOT NULL, ok INTEGER NOT NULL, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, cost_usd REAL);
CREATE TABLE IF NOT EXISTS frozen_wallets (
  address TEXT PRIMARY KEY, case_ref TEXT, frozen_at REAL NOT NULL);
"""

_local = threading.local()


def conn() -> sqlite3.Connection:
    """Thread-local connection to config.DB_PATH (schema created on first use)."""
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != config.DB_PATH:
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        c.executescript(SCHEMA)
        _local.conn, _local.path = c, config.DB_PATH
    return c


def close():
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn, _local.depth = None, 0


class immediate:
    """`with db.immediate() as c:` — BEGIN IMMEDIATE ... COMMIT/ROLLBACK (atomic check-then-write).
    Re-entrant: a nested block (e.g. journal.append inside a state change) joins the outer transaction.
    Never hold it across a chain tx or an `await` — other writers wait at most 10 s."""

    def __enter__(self):
        self.c = conn()
        depth = getattr(_local, "depth", 0)
        if depth == 0:
            self.c.execute("BEGIN IMMEDIATE")
        _local.depth = depth + 1
        return self.c

    def __exit__(self, exc_type, exc, tb):
        _local.depth -= 1
        if _local.depth == 0:
            self.c.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def one(sql, params=()):
    row = conn().execute(sql, params).fetchone()
    return dict(row) if row else None


def rows(sql, params=()):
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def run(sql, params=()):
    return conn().execute(sql, params)


def dumps(obj) -> str:
    """Canonical JSON used for storage and hashing (sorted keys, no spaces, UTF-8)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def loads(s):
    return json.loads(s) if s else None
