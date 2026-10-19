# Incoming transfers to the bank's client wallets (SPEC §11, L7): Tatum webhook, RPC polling as the fallback.
# The webhook body is untrusted. First the signature: x-payload-hash = base64(HMAC-SHA512(secret, raw body)),
# compared in constant time (fallback: the compact JSON re-serialisation, SPEC §11). Then the chain decides: the
# bank re-reads the receipt and records only Transfer events of its own assets to its client wallets — the payload
# only says which tx to look at. Both paths write one notifications row per tx (tx_id UNIQUE) and one journal
# entry `incoming_transfer` in the same SQLite transaction, so a tx delivered twice, or seen by Tatum and by the
# poller, is counted once. Block → feed latency goes to timings (SPEC §19.4).
#   Live switch-on (needs a public URL): PUBLIC_BASE_URL=https://<tunnel> python -m scripts.tatum_subscribe
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from web3 import Web3
from web3.exceptions import TransactionNotFound

from bank import chain, config, db, journal

POLL_S = 1.0  # default only; the poller reads config.BANK_POLL_S on every tick (SPEC §19.4: block → feed ≤ 5 s)
LOOKBACK = 30  # blocks re-read when the poller starts (about a minute on Base)
RECEIPT_TRIES, RECEIPT_DELAY = 4, 1.0  # Tatum may notify before our RPC has the block; stays far below its 10 s
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

_stop = threading.Event()
_thread: threading.Thread | None = None


# ---- signature ----

def sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha512).digest()).decode()


def signature_ok(raw: bytes, header: str | None, secret: str) -> bool:
    """Raw body bytes first; then the compact JSON form Tatum signs (if a proxy re-formatted the body)."""
    if not secret or not header:
        return False
    candidates = [raw]
    try:
        compact = json.dumps(json.loads(raw), separators=(",", ":"), ensure_ascii=False).encode()
        if compact != raw:
            candidates.append(compact)
    except ValueError:
        pass
    given = header.strip().encode()
    return any(hmac.compare_digest(sign(secret, c).encode(), given) for c in candidates)


# ---- payload (hints only) and the chain (the truth) ----

def parse(body) -> dict:
    """Tatum's enriched template nests the event in `data`; the legacy one is flat (address, counterAddress, asset,
    amount). Units of `value` are not relied on: the amount comes from the chain."""
    d = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    if not isinstance(d, dict):
        return {}
    meta = d.get("tokenMetadata") if isinstance(d.get("tokenMetadata"), dict) else {}
    s = lambda v: v.strip() if isinstance(v, str) else None  # noqa: E731
    return {"tx_id": s(d.get("txId")), "to": s(d.get("to") or d.get("address")),
            "from": s(d.get("from") or d.get("counterAddress")), "contract": s(d.get("contractAddress") or d.get("asset")),
            "value": d.get("value", d.get("amount")), "decimals": meta.get("decimals")}


def client_transfers(logs) -> list[dict]:
    """Transfer events of the bank's assets (EURC, tCZK) whose recipient is a client wallet."""
    assets = {v["address"].lower(): k for k, v in config.ASSETS.items() if v["address"]}
    clients = {r["wallet_address"].lower(): r["id"]
               for r in db.rows("SELECT id, wallet_address FROM companies WHERE wallet_address IS NOT NULL")}
    out = []
    for log in logs:
        topics = log["topics"]
        if len(topics) != 3 or Web3.to_hex(topics[0]) != chain.TRANSFER_TOPIC:
            continue
        asset = assets.get(log["address"].lower())
        to = Web3.to_checksum_address("0x" + Web3.to_hex(topics[2])[-40:])
        if not asset or to.lower() not in clients:
            continue
        out.append({"asset": asset, "token": Web3.to_checksum_address(log["address"]),
                    "from": Web3.to_checksum_address("0x" + Web3.to_hex(topics[1])[-40:]), "to": to,
                    "company_id": clients[to.lower()], "amount": int(Web3.to_hex(log["data"]), 16)})
    return out


def _receipt(tx_hash: str):
    try:
        return chain.w3().eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        return None


def verify_on_chain(tx_hash: str, hint: dict) -> dict:
    """{ok, reason, transfers, block_number}. A transfer the payload names (to / contractAddress) must be on chain."""
    rcpt = chain.read_retry(lambda: _receipt(tx_hash), ok=lambda r: r is not None, tries=RECEIPT_TRIES,
                            delay=RECEIPT_DELAY)
    if rcpt is None:
        return {"ok": False, "reason": "not_on_chain"}
    if rcpt["status"] != 1:
        return {"ok": False, "reason": "tx_reverted"}
    transfers = client_transfers(rcpt["logs"])
    for key, field in (("to", "to"), ("contract", "token")):
        if hint.get(key) and Web3.is_address(hint[key]):
            transfers = [t for t in transfers if t[field].lower() == hint[key].lower()]
    if not transfers:
        return {"ok": False, "reason": "no_matching_transfer"}
    return {"ok": True, "reason": None, "transfers": transfers, "block_number": rcpt["blockNumber"]}


def record(tx_hash: str, transfers: list[dict], block_number: int, source: str, untrusted=None,
           measure: bool = True) -> bool:
    """One notifications row + one `incoming_transfer` journal entry per tx, atomically. False = already counted.
    measure=False for the poller's catch-up on start: an old block is not block → feed latency."""
    tx_id = tx_hash.lower()
    block_ts = chain.w3().eth.get_block(block_number)["timestamp"]  # RPC before the write lock, never inside it
    deal = db.one("SELECT deal_id FROM onchain_txs WHERE lower(tx_hash)=?", (tx_id,))
    first = transfers[0]
    stored = {"source": source, "block_number": block_number, "transfers": transfers}
    if untrusted is not None:
        stored["untrusted_payload"] = untrusted
    with db.immediate() as c:
        cur = c.execute("INSERT INTO notifications (tx_id, payload_json, verified, received_at) VALUES (?,?,1,?)"
                        " ON CONFLICT(tx_id) DO UPDATE SET verified=1, payload_json=excluded.payload_json,"
                        " received_at=excluded.received_at WHERE notifications.verified=0",
                        (tx_id, db.dumps(stored), time.time()))
        if cur.rowcount == 0:
            return False
        journal.append("bank", "incoming_transfer", {
            "source": source, "tx_hash": tx_hash, "link": chain.tx_link(tx_hash), "asset": first["asset"],
            "amount": sum(t["amount"] for t in transfers if t["asset"] == first["asset"] and t["to"] == first["to"]),
            "to": first["to"], "from": first["from"], "company_id": first["company_id"], "block_number": block_number,
            "transfers": transfers}, deal_id=deal and deal["deal_id"])
    if not measure:
        return True
    stage = "block_to_feed" if source == "rpc_poll" else f"block_to_feed:{source}"
    db.run("INSERT INTO timings (ts, deal_id, stage, ms, ok) VALUES (?,?,?,?,1)",
           (time.time(), deal and deal["deal_id"], stage, max(0.0, (time.time() - block_ts) * 1000)))
    return True


def counted(tx_hash: str) -> bool:
    return db.one("SELECT 1 FROM notifications WHERE tx_id=? AND verified=1", (tx_hash.lower(),)) is not None


# ---- Tatum webhook ----

def handle(raw: bytes, header: str | None) -> JSONResponse:
    if not signature_ok(raw, header, config.TATUM_HMAC_SECRET):
        reason = ("no_secret_configured" if not config.TATUM_HMAC_SECRET
                  else "missing_signature" if not header else "bad_signature")
        journal.append("bank", "webhook_rejected", {"source": "tatum", "reason": reason, "bytes": len(raw),
                                                    "body_sha256": hashlib.sha256(raw).hexdigest()})
        return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=401)
    try:
        body = json.loads(raw)
    except ValueError:
        return JSONResponse({"ok": False, "error": "malformed JSON"}, status_code=400)
    hint = parse(body)
    tx = hint.get("tx_id")
    if not tx or not TX_RE.match(tx):
        return JSONResponse({"ok": True, "accepted": False, "reason": "no_tx_id"})
    if counted(tx):
        return JSONResponse({"ok": True, "accepted": True, "duplicate": True, "tx_id": tx})
    try:
        verdict = verify_on_chain(tx, hint)
    except Exception as e:  # noqa: BLE001 — RPC down: 5xx so Tatum retries; the poller catches it meanwhile
        return JSONResponse({"ok": False, "error": f"chain read failed: {type(e).__name__}"}, status_code=503)
    if not verdict["ok"]:
        db.run("INSERT OR IGNORE INTO notifications (tx_id, payload_json, verified, received_at) VALUES (?,?,0,?)",
               (tx.lower(), db.dumps({"source": "tatum", "reason": verdict["reason"], "untrusted_payload": body}),
                time.time()))
        journal.append("bank", "webhook_unverified", {"source": "untrusted:tatum", "tx_hash": tx,
                                                      "reason": verdict["reason"]})
        return JSONResponse({"ok": True, "accepted": False, "reason": verdict["reason"], "tx_id": tx})
    new = record(tx, verdict["transfers"], verdict["block_number"], "tatum", untrusted=body)
    return JSONResponse({"ok": True, "accepted": True, "duplicate": not new, "tx_id": tx})


# ---- RPC polling fallback (no public URL) ----

def seller_wallets() -> list[str]:
    return [r["wallet_address"] for r in db.rows("SELECT c.wallet_address FROM sellers s JOIN companies c"
                                                 " ON c.id = s.company_id WHERE c.wallet_address IS NOT NULL")]


def poll_once(from_block: int | None = None) -> int:
    """Read Transfer events to the seller wallets since from_block (chunked); record new txs. Returns the next block."""
    head = chain.w3().eth.block_number
    start = max(0, head - LOOKBACK) if from_block is None else from_block
    if start > head:
        return start
    wallets = seller_wallets()
    if not wallets:
        return head + 1
    to_topic = ["0x" + w.lower().removeprefix("0x").rjust(64, "0") for w in wallets]
    by_tx: dict[str, list] = {}
    for asset in config.ASSETS.values():
        if asset["address"]:
            for log in chain.get_logs(asset["address"], [chain.TRANSFER_TOPIC, None, to_topic], start, head):
                by_tx.setdefault(Web3.to_hex(log["transactionHash"]), []).append(log)
    for tx, logs in by_tx.items():
        transfers = client_transfers(logs)
        if transfers and not counted(tx):
            record(tx, transfers, logs[0]["blockNumber"], "rpc_poll", measure=from_block is not None)
    return head + 1


def _run():
    nxt = None
    while not _stop.is_set():
        try:
            nxt = poll_once(nxt)
        except Exception as e:  # noqa: BLE001 — a flaky RPC must not kill the poller; the next tick retries the range
            print(f"[webhooks] transfer poll failed: {type(e).__name__}: {e}", flush=True)
        _stop.wait(getattr(config, "BANK_POLL_S", POLL_S))


def start() -> bool:
    """Start the poller unless Tatum can reach us (PUBLIC_BASE_URL set) or BANK_POLLER=0."""
    global _thread
    if config.PUBLIC_BASE_URL or os.getenv("BANK_POLLER", "1") == "0" or os.getenv("DEMO_SIM") == "1":
        return False  # DEMO_SIM: real chain transfers must not appear under the "simulated chain" banner
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_run, name="transfer-poller", daemon=True)
        _thread.start()
    return True


def stop():
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)


@asynccontextmanager
async def lifespan(app):
    start()
    yield
    stop()


router = APIRouter(lifespan=lifespan)


@router.post("/webhooks/tatum")
async def tatum(request: Request):
    raw = await request.body()  # the raw bytes: the signature covers exactly these
    return await asyncio.to_thread(handle, raw, request.headers.get("x-payload-hash"))
