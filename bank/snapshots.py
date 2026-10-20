# Source snapshots (MODEL_V2 §5.1, §2.1). The bank publishes source templates; for rule C the task terms carry
# {"op": "source", "template": t, "key_field": f} and the bank builds the snapshot URL itself from the template and
# the item's key — a source_url sent by the seller is never fetched. Each template publishes its field mapping
# "item field -> path in the snapshot" with a comparison type; a C claim is possible only on mapped fields, and
# the values are extracted by the bank (never quoted by the buyer).
# The fetcher is meant to run as an isolated process without access to the bank's network; here it is one module
# with the same guards: https only, exact host of a published template (no subdomains), every resolved address
# public (no private, loopback, link-local or reserved ranges: SSRF / DNS rebinding), no cookies or auth, 2 MB and
# 10 s per snapshot, at most 3 redirects on the same host. Outcomes: a clear "no such key" (404) or an oversized
# answer -> SOURCE_UNAVAILABLE; a timeout, 5xx, 429, a redirect elsewhere or our own failure -> the item waits and
# the bank retries with a pause (not the seller's fault). Only items that passed every other predicate get here.
# MOCK / demo: ARES answers for the synthetic companies come from fixtures/registries/items.json.
import hashlib
import ipaddress
import socket
import time
from urllib.parse import urlsplit

import httpx
import re2

from bank import config, db, journal

TEMPLATES = {
    "ares": {
        "title": "ARES — economic subject by IČO (Ministry of Finance CZ)",
        "url": "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{key}",
        "host": "ares.gov.cz", "key": r"\d{8}", "method": "api",
        "fields": {"name": {"path": "obchodniJmeno", "type": "string"},
                   "founded": {"path": "datumVzniku", "type": "date"},
                   "address": {"path": "sidlo.textovaAdresa", "type": "string"},
                   "postalCode": {"path": "sidlo.psc", "type": "code"}},
    },
}
MAX_BYTES, TIMEOUT_S, MAX_REDIRECTS = 2 * 1024 * 1024, 10.0, 3
RETRY_S = (5, 30, 120, 600)  # pauses between tries while a source is down (then every 10 min)
GIVE_UP_BUSINESS_DAYS = 5   # then a person of the buyer decides: accept without C, or reject (§5.1)


class Unavailable(Exception):
    """The source clearly has no such key, or the answer is over the limit: SOURCE_UNAVAILABLE."""


class Wait(Exception):
    """Not the seller's fault (timeout, 5xx, 429, redirect elsewhere, our own failure): retry later."""


def key_ok(template: str, key) -> bool:
    t = TEMPLATES.get(template)
    return bool(t) and isinstance(key, (str, int)) and not isinstance(key, bool) \
        and re2.fullmatch(t["key"], str(key).strip()) is not None


def build_url(template: str, key) -> str:
    """The only way a snapshot URL is made: published template + the item's key (never the seller's URL)."""
    if not key_ok(template, key):
        raise ValueError(f"{key!r} is not a key of {template}")
    return TEMPLATES[template]["url"].format(key=str(key).strip())


def extract(template: str, body) -> dict:
    """Field values the bank itself reads from a snapshot by the published mapping: {field: value, field:type: t}."""
    out = {}
    for field, m in TEMPLATES[template]["fields"].items():
        cur = body
        for part in m["path"].split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is not None:
            out[field] = cur
            out[field + ":type"] = m["type"]
    return out


# ---- the guarded fetch ----

def public_address(host: str) -> None:
    """Every address the host resolves to must be public (raises Wait on DNS failure, Unavailable never)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise Wait(f"DNS: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local \
                or ip.is_private:
            raise PermissionError(f"{host} resolves to a non-public address {ip}")


def fetch(template: str, url: str, client: httpx.Client | None = None, resolve=public_address) -> bytes:
    """GET with the §5.1 guards. Returns the body; raises Unavailable / Wait / PermissionError."""
    t = TEMPLATES[template]
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False, trust_env=False)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.hostname != t["host"] or parts.username or parts.password:
                raise Wait(f"not a published host of {template}: {parts.scheme}://{parts.hostname}")
            resolve(parts.hostname)  # checked right before every request (redirects included)
            t0 = time.monotonic()
            try:
                with client.stream("GET", url, headers={"Accept": "application/json", "Cookie": ""}) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        url = str(httpx.URL(url).join(r.headers.get("location", "")))
                        continue
                    if r.status_code == 404:
                        raise Unavailable("the source has no such key (404)")
                    if r.status_code == 429 or r.status_code >= 500:
                        raise Wait(f"source answered {r.status_code}")
                    if r.status_code != 200:
                        raise Wait(f"source answered {r.status_code}")
                    body = b""
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise Unavailable("the answer is over the 2 MB limit")
                        if time.monotonic() - t0 > TIMEOUT_S:
                            raise Wait("over the 10 s limit")
                    return body
            except httpx.HTTPError as e:
                raise Wait(f"{type(e).__name__}") from e
        raise Wait("more than 3 redirects")
    finally:
        if own:
            client.close()


def _mock_body(template: str, key: str) -> bytes:
    """Demo source: the synthetic companies' ARES records (fixtures/registries/items.json) and 404 otherwise."""
    from bank import screening
    fx = screening.fixture(key) if template == "ares" else None
    if fx is None:
        raise Unavailable("the source has no such key (404)")
    return db.dumps(fx["ares"]["body"]).encode()


# ---- snapshots per item ----

def get(deal_id: str, version: int, idx: int, template: str) -> dict | None:
    row = db.one("SELECT * FROM snapshots WHERE deal_id=? AND version=? AND idx=? AND template=?",
                 (deal_id, version, idx, template))
    if row:
        row["fields"] = db.loads(row.pop("fields_json")) or {}
    return row


def for_item(deal: dict, version: int, idx: int, item: dict, pred: dict, now: float | None = None):
    """The engine's snapshot callable: {"status": "ok", "fields": {...}} | "pending" | "unavailable". Takes the
    snapshot on first call (the URL from the template and the item's key), retries with pauses while it waits."""
    from bank import verifier
    now = time.time() if now is None else now
    template = pred["template"]
    key = str(verifier.get_field(item, pred["key_field"])[1]).strip()
    row = get(deal["deal_id"], version, idx, template)
    if row is None:
        url = build_url(template, key)
        db.run("INSERT OR IGNORE INTO snapshots (deal_id, version, idx, template, key, url, seller_id, status,"
               " attempts, next_try_at, created_at) VALUES (?,?,?,?,?,?,?,'pending',0,?,?)",
               (deal["deal_id"], version, idx, template, key, url, deal["seller_id"], now, now))
        row = get(deal["deal_id"], version, idx, template)
    if row["status"] == "pending" and row["attempts"] == 0:  # the first try at delivery; retries go through drain()
        row = take(row, now)
    if row["status"] == "ok":
        return {"status": "ok", "fields": row["fields"], "hash": row["body_hash"], "taken_at": row["taken_at"]}
    return "unavailable" if row["status"] == "unavailable" else "pending"


def take(row: dict, now: float, fetcher=None) -> dict:
    """One try for one snapshot; the result, its hash and the time by the bank's journal are stored."""
    fetcher = fetcher or (lambda t, u: _mock_body(t, row["key"]) if config.MOCK else fetch(t, u))
    status, body, err = "pending", None, None
    try:
        body = fetcher(row["template"], row["url"])
        status = "ok"
    except Unavailable as e:
        status, err = "unavailable", str(e)
    except (Wait, PermissionError, OSError) as e:
        err = f"{type(e).__name__}: {e}"[:200]
    attempts = row["attempts"] + 1
    pause = RETRY_S[min(attempts - 1, len(RETRY_S) - 1)]
    fields, h = {}, None
    if body is not None:
        h = "0x" + hashlib.sha256(body).hexdigest()
        try:
            import json
            fields = extract(row["template"], json.loads(body))
        except ValueError:
            status, err = "unavailable", "the answer is not JSON"
    with db.immediate():
        db.run("UPDATE snapshots SET status=?, body=?, body_hash=?, fields_json=?, taken_at=?, attempts=?,"
               " next_try_at=?, error=? WHERE id=?",
               (status, body, h, db.dumps(fields), now if status == "ok" else None, attempts, now + pause, err,
                row["id"]))
        journal.append("bank", f"snapshot.{status}", {"url": row["url"], "hash": h, "error": err,
                                                     "version": row["version"], "index": row["idx"]},
                       deal_id=row["deal_id"])
    return get(row["deal_id"], row["version"], row["idx"], row["template"])


def fair_order(rows: list[dict]) -> list[dict]:
    """§5.1 fair queue: a source's request budget is shared equally between sellers — round robin by seller, so one
    seller's deliveries (or its puppet buyers) cannot slow the snapshots of others."""
    by_seller: dict[str, list] = {}
    for r in sorted(rows, key=lambda r: r["next_try_at"]):
        by_seller.setdefault(r["seller_id"], []).append(r)
    out = []
    while any(by_seller.values()):
        for seller in sorted(by_seller):
            if by_seller[seller]:
                out.append(by_seller[seller].pop(0))
    return out


def drain(now: float | None = None, per_source: int = 10) -> int:
    """The timer's pass over waiting snapshots, at most `per_source` requests per template, fairly by seller."""
    now = time.time() if now is None else now
    done = 0
    for template in TEMPLATES:
        rows = db.rows("SELECT * FROM snapshots WHERE status='pending' AND template=? AND next_try_at<=?",
                       (template, now))
        for row in fair_order(rows)[:per_source]:
            row["fields"] = db.loads(row.pop("fields_json")) or {}
            if overdue(row, now):  # 5 business days without a snapshot: silence rejects (a person may accept it)
                db.run("UPDATE snapshots SET status='unavailable', error=? WHERE id=?",
                       ("no snapshot in 5 business days", row["id"]))
                journal.append("timer", "snapshot.gave_up", {"url": row["url"], "version": row["version"],
                                                             "index": row["idx"]}, deal_id=row["deal_id"])
            else:
                take(row, now)
            done += 1
    return done


def overdue(row: dict, now: float) -> bool:
    """No snapshot for 5 business days: a person of the buyer decides (accept without C or reject; silence rejects)."""
    return now - row["created_at"] > GIVE_UP_BUSINESS_DAYS * config.BUSINESS_DAY_S
