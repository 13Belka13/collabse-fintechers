# Bank UI (SPEC §13): server-rendered Jinja2 + HTMX, no JS build. The bank's decision and where the money is
# are the hero. Reads tables + the journal; writes only through module functions (bank.disputes for human
# dispute steps). Client texts always pass codes.client_view; AI-generated text carries the AI Act Art. 50 label.
# The router's lifespan starts the dispute timer (bank/deadlines.py) inside the bank process.
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from bank import codes, db, deadlines, deals, disputes, escrow, explain, journal, money, timing

DEMO_MANDATE = "m-alfa-1"
BANK_OFFICER = "human:bank-officer"
TRANSFER_EVENTS = {"escrow_authorized": "Locked in escrow", "escrow_captured": "Paid to the seller",
                   "escrow_voided": "Returned to the buyer", "x402_settled": "x402 payment settled"}
INFO_EVENTS = {"dispute.opened", "dispute.default_applied", "dispute.buyer_review", "dispute.seller_objection",
               "dispute.decision", "dispute.holdback_released", "dispute.closed", "dispute.capture_impossible",
               "dispute.tx_failed", "escrow_expiry_alarm", "timer_error", "x402_refused", "demo.scenario"}
BAD_EVENTS = {"escrow_expiry_alarm", "timer_error", "dispute.tx_failed", "dispute.capture_impossible", "x402_refused"}
STEP_TEXT = {"review": "Buyer reviews the flagged item", "objection": "Seller may object to the rejection",
             "decision": "Bank / arbiter decides", "done": "Decided"}


@asynccontextmanager
async def lifespan(app):
    if os.getenv("DEMO_SIM") == "1":
        from scripts import demo_sim
        demo_sim.install()
    if os.getenv("BANK_TIMER", "1") != "0":
        deadlines.start()
    yield
    deadlines.stop()


router = APIRouter(lifespan=lifespan, include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _sim() -> bool:
    try:
        from scripts import demo_sim
        return demo_sim.ACTIVE
    except ImportError:
        return False


def _fmt_ts(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"


def _fmt_money(atomic, asset="EURC") -> str:
    return money.fmt(asset or "EURC", int(atomic or 0))


def _tx(h: str | None) -> dict | None:
    if not h:
        return None
    from bank import chain
    sim = _sim()
    return {"hash": h, "short": f"{h[:10]}…{h[-6:]}", "url": None if sim else chain.tx_link(h), "sim": sim}


templates.env.filters.update(ts=_fmt_ts, money=_fmt_money, tx=_tx,
                             eur=lambda c: f"{(c or 0) / 100:.2f} €".replace(".", ","),
                             left=lambda t: max(0, int((t or 0) - time.time())))
templates.env.globals.update(AI_LABEL=explain.AI_LABEL, sim=_sim, STEP_TEXT=STEP_TEXT)


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"active": name.split(".")[0], **ctx})


# ---- view helpers ----

def decision_of(row: dict) -> dict:
    """A stored decision back in the Decision shape (codes_json[0] is the code, evidence_json the reasons)."""
    code_list = db.loads(row.get("codes_json")) or [None]
    return {"status": row["outcome"], "code": code_list[0], "reasons": db.loads(row.get("evidence_json")) or [],
            "explanation": row.get("explanation_client") or "", "deal_id": row.get("deal_id")}


def explain_row(row: dict, view: str) -> dict:
    """One explanation level of a stored decision. Client: only what codes.client_view lets through."""
    d = decision_of(row)
    client = codes.client_view(d)
    if view == "client":
        return {"status": client["status"], "code": client["code"], "reasons": client["reasons"],
                "text": client["explanation"] or explain.render(d, "client")["text"]}
    return {"status": d["status"], "code": d["code"], "reasons": d["reasons"],
            "text": row.get("explanation_compliance") or explain.render(d, "compliance")["text"],
            "client_text": client["explanation"],
            "hidden_from_client": client["code"] == codes.Code.UNDER_REVIEW and d["code"] != codes.Code.UNDER_REVIEW}


def ledger(deal: dict) -> dict:
    """Where a deal's money is, from the journal's escrow_* events: valid with or without a dispute."""
    total = int(deal.get("amount") or 0)
    ev = journal.entries(deal["deal_id"], limit=1000)
    locked = any(e["event"] == "escrow_authorized" for e in ev)
    captured = sum(int(e["payload"].get("amount", 0)) for e in ev if e["event"] == "escrow_captured")
    returned = total - captured if any(e["event"] == "escrow_voided" for e in ev) else 0
    d = disputes.get(deal["deal_id"])
    running = d is not None and not d["holdback_released"]
    return {"total": total, "locked": locked, "captured": captured, "returned": returned,
            "in_escrow": (total - captured - returned) if locked else 0,
            "holdback": d["holdback"] if running else 0, "holdback_until": d["holdback_until"] if running else None}


def feed(view: str, limit: int = 60) -> list[dict]:
    """/bank: decisions (green / red with reasons / amber for a human), transfers, dispute and timer steps."""
    rows = []
    for r in db.rows("SELECT * FROM decisions WHERE kind='pay' ORDER BY id DESC LIMIT ?", (limit,)):
        x = explain_row(r, view)
        rows.append({"ts": r["created_at"], "kind": "decision", "deal_id": r["deal_id"], "ms": r["ms"],
                     "tone": {"ok": "ok", "refused": "bad"}.get(x["status"], "warn"), **x})
    entries = journal.entries(limit=600)
    asset_of = {r["deal_id"]: r["asset"] for r in db.rows("SELECT deal_id, asset FROM deals")}
    by_tx = {e["payload"].get("tx_hash"): (e["actor"], e["payload"].get("rule"))
             for e in entries if e["event"] in ("dispute.capture", "dispute.void")}
    names = {r["id"]: r["name"] for r in db.rows("SELECT id, name FROM companies")}
    for e in entries:
        p = e["payload"]
        if e["event"] == "incoming_transfer":  # verified on chain by bank.webhooks (Tatum webhook or RPC poll)
            source = {"tatum": "Tatum webhook", "rpc_poll": "bank's RPC poll"}.get(p.get("source"), p.get("source"))
            rows.append({"ts": e["ts"], "kind": "transfer", "tone": "ok", "deal_id": e["deal_id"],
                         "title": f"Received by {names.get(p.get('company_id'), p.get('to'))}", "amount": p.get("amount"),
                         "asset": p.get("asset"), "tx": p.get("tx_hash"), "actor": None, "rule": None,
                         "source": source})
        elif e["event"] in TRANSFER_EVENTS:
            deal = deals.get(e["deal_id"]) if e["deal_id"] else None
            amount = p.get("amount")
            if e["event"] == "escrow_voided" and deal:
                amount = ledger(deal)["returned"] or None
            actor, rule = by_tx.get(p.get("tx_hash"), (e["actor"], None))
            rows.append({"ts": e["ts"], "kind": "transfer", "tone": "ok", "deal_id": e["deal_id"],
                         "title": TRANSFER_EVENTS[e["event"]], "amount": amount, "asset": (deal or {}).get("asset"),
                         "tx": p.get("tx_hash") or p.get("transaction"), "actor": actor, "rule": rule})
        elif e["event"] in INFO_EVENTS:
            if view == "client" and e["event"] == "x402_refused":
                p = {"code": codes.client_view({"code": p.get("code"), "reasons": p.get("reasons", [])})["code"]}
            rows.append({"ts": e["ts"], "kind": "event", "deal_id": e["deal_id"], "event": e["event"],
                         "asset": asset_of.get(e["deal_id"]) or "EURC",
                         "tone": "bad" if e["event"] in BAD_EVENTS else "info", "actor": e["actor"], "payload": p})
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:limit]


def panel() -> dict:
    last = db.one("SELECT ms FROM timings WHERE stage='decision' ORDER BY id DESC LIMIT 1")
    return {"last_ms": last and last["ms"], "decision": timing.stage_stats("decision"), "llm": timing.llm_totals(),
            "timer": deadlines.running()}


def alarms(now: float | None = None) -> list[dict]:
    """Open deals whose escrow can no longer be paid out within the hour (§9.6)."""
    now = time.time() if now is None else now
    out = []
    for d in db.rows("SELECT deal_id, state, payment_info_json FROM deals WHERE payment_info_json IS NOT NULL"
                     " AND state IN ('authorized','delivered','verified','disputed','settled')"):
        exp = int((db.loads(d["payment_info_json"]) or {}).get("authorizationExpiry") or 0)
        if exp and exp - now < deadlines.ALARM_S:
            out.append({"deal_id": d["deal_id"], "state": d["state"], "left": max(0, int(exp - now)), "expiry": exp})
    return out


def _deal_or_404(deal_id: str) -> dict:
    deal = deals.get(deal_id)
    if deal is None:
        raise HTTPException(404, "unknown deal")
    return deal


def _seller(seller_id: str) -> dict:
    return db.one("SELECT c.id, c.name, c.ico FROM sellers s JOIN companies c ON c.id = s.company_id"
                  " WHERE s.seller_id=?", (seller_id,)) or {"id": seller_id, "name": seller_id, "ico": None}


def _names() -> dict:
    return {r["seller_id"]: r["name"] for r in db.rows(
        "SELECT s.seller_id, c.name FROM sellers s JOIN companies c ON c.id = s.company_id")}


def _items(deal_id: str) -> tuple[list[dict], dict]:
    delivery = db.one("SELECT * FROM deliveries WHERE deal_id=?", (deal_id,)) or {}
    raw = {i.get("placeId"): i for i in (db.loads(delivery.get("items_json")) or [])}
    return db.loads(delivery.get("rule_results_json")) or [], raw


# ---- the story of one deal, for people watching the demo (/story) ----

PLAIN = {  # decision codes in words for the audience; the code itself stays visible next to it
    "MANDATE_NOT_ACTIVE": "the mandate is not active", "MANDATE_EXPIRED": "the mandate has expired",
    "CATEGORY_NOT_IN_SCOPE": "outside what the mandate allows", "COUNTERPARTY_NOT_IN_SCOPE": "money was asked for a wrong account",
    "COUNTERPARTY_NOT_TRUSTED": "the seller failed the bank's checks", "AMOUNT_ABOVE_LIMIT": "the amount is above the limit",
    "BUDGET_EXCEEDED": "the monthly budget would be exceeded", "QUOTE_MISMATCH": "the payment request differs from the deal",
    "VELOCITY_EXCEEDED": "too many payments too fast", "CONFIRMATION_REQUIRED": "a person must confirm this amount",
    "VAT_LIABILITY_RISK": "Czech VAT liability risk (§109)", "UNDER_REVIEW": "the payment is under review at the bank",
}


def _name(n: str) -> str:
    return (n or "").rstrip(".")  # "Beta s.r.o." at the end of a sentence must not end in ".."


def _step(key, title, status, text="", ts=None, **extra) -> dict:
    """status: done | now | blocked | wait (a human must act) | skipped (never reached: no money moved)."""
    return {"key": key, "title": title, "status": status, "text": text, "ts": ts, **extra}


def story(deal_id: str | None = None) -> dict | None:
    """One deal as a sequence of plain steps + where its money is now. Decision texts go through the same
    client view as the feed: what the audience sees is what the client may see (no tipping-off)."""
    from bank import mandate as mandate_mod
    row = (db.one("SELECT deal_id FROM deals WHERE deal_id=?", (deal_id,)) if deal_id
           else db.one("SELECT deal_id FROM deals ORDER BY created_at DESC LIMIT 1"))
    if row is None:
        return None
    deal = deals.get(row["deal_id"])
    asset, did = deal["asset"] or "EURC", deal["deal_id"]
    ev = list(reversed(journal.entries(did, limit=1000)))  # oldest first
    seller = _seller(deal["seller_id"])
    buyer = db.one("SELECT name FROM companies WHERE id=?", (deal["buyer_id"],)) or {"name": deal["buyer_id"]}
    first = lambda name: next((e for e in ev if e["event"] == name), None)  # noqa: E731
    every = lambda name: [e for e in ev if e["event"] == name]  # noqa: E731
    steps = []

    # 1. the human's mandate
    m = mandate_mod.current(deal["mandate_id"]) if deal.get("mandate_id") else None
    budget = ((deal.get("task") or {}).get("budget") or {}).get("value")
    if m:
        p = m["policy"]
        steps.append(_step("mandate", "The human set the agent's limits", "done",
                           f"Up to {int(p['per_deal_max']['value']) / 100:.2f} € per deal, "
                           f"{int(p['budget']['value']) / 100:.2f} € a month, a person confirms above "
                           f"{int(p['confirm_above']['value']) / 100:.2f} €"
                           + (f"; this task may spend at most {int(budget) / 100:.2f} €." if budget else "."),
                           ts=(first("deal_created") or {}).get("ts")))

    # 2. the agents talk (the seller's words are untrusted data)
    talk = []
    for e in ev:
        if e["event"] == "buyer_message" and e["actor"] == "agent:buyer":
            talk.append({"who": "buyer", "text": e["payload"].get("message", ""), "ts": e["ts"]})
        elif e["event"] == "seller_reply" and e["actor"] == "agent:seller":
            offer = e["payload"].get("offer") or {}
            price = (offer.get("prices") or {}).get(asset)
            full = e["payload"].get("reply_text") or ""
            hidden = full[full.index("SYSTEM:"):].strip(" ->") if "SYSTEM:" in full else None
            talk.append({"who": "seller", "text": full.split(" — ")[0], "ts": e["ts"], "untrusted": True,
                         "price": _fmt_money(price, asset) if price else None, "injected": hidden})
    if talk:
        offers = sum(t["who"] == "seller" for t in talk)
        steps.append(_step("talk", f"The agents negotiated ({offers} offer{'s' if offers != 1 else ''})",
                           "done", talk=talk, ts=talk[0]["ts"]))

    # 3. the bank's decision(s) before any money moves
    decided = db.rows("SELECT * FROM decisions WHERE deal_id=? AND kind='pay' ORDER BY id", (did,))
    stopped = None
    for r in decided:
        x = explain_row(r, "client")
        human = next((e for e in ev if e["event"] in ("vat_risk_acknowledged", "payment_confirmed")
                      and e["ts"] >= r["created_at"]), None)
        plain = PLAIN.get(x["code"], x["code"])
        if x["status"] == "ok":
            status, title = "done", "The bank approved the payment"
            marks = [part.split("]")[0] for part in (x["text"] or "").split("[")[1:]]  # "[VAT risk acknowledged …]"
            reasons = ["Within the mandate and the task budget; the seller passed the bank's checks."] + \
                [f"Note: {m}." for m in marks]
        elif x["status"] == "requires_user_action":
            status = "done" if human else "wait"
            title = f"The bank asked a person first: {plain}"
            reasons = [rr["text"] for rr in x["reasons"]][:2] or [x["text"]]
        else:
            status, title = "blocked", f"The bank refused: {plain}"
            stopped = x
            reasons = [rr["text"] for rr in x["reasons"]][:2] or [x["text"]]
        if human:
            reasons.append(f"{human['actor'].removeprefix('human:')} "
                           + ("acknowledged the VAT risk once." if human["event"] == "vat_risk_acknowledged"
                              else "confirmed the payment."))
        steps.append(_step("decision", title, status, " ".join(t for t in reasons if t), ts=r["created_at"],
                           ms=r["ms"], code=x["code"]))
    if not decided and talk:
        steps.append(_step("decision", "The bank decides before any money moves", "wait",
                           "Waiting for the agent to pay."))

    # 4. money into the safe
    lock = first("escrow_authorized")
    if lock:
        steps.append(_step("escrow", "The money went into the escrow safe", "done",
                           f"{_fmt_money(lock['payload'].get('amount'), asset)} locked on chain; neither side can take "
                           "it alone. The seller checked the safe before delivering.", ts=lock["ts"],
                           tx=lock["payload"].get("tx_hash")))
    # 5. the delivery checked by code rules (the AI judge only flags)
    check = first("verifier.settlement")
    if check:
        c = check["payload"]
        parts = [f"{c.get('accepted', 0)} of {c.get('ordered', 0)} items accepted by the code rules"]
        if c.get("rejected"):
            parts.append(f"{c['rejected']} rejected")
        if c.get("held"):
            parts.append(f"{c['held']} flagged by the AI judge for a person")
        steps.append(_step("verify", f"{c.get('delivered', 0)} items delivered and checked", "done",
                           ", ".join(parts) + ".", ts=check["ts"]))
    # 6. payouts, the dispute, the holdback
    lg = ledger(deal) if lock else None
    caps = every("escrow_captured")
    d = disputes.get(did)
    if caps:
        open_items = [it for it in (d or {}).get("items", []) if not it.get("outcome")]
        text = f"{_fmt_money(lg['captured'], asset)} paid to {_name(seller['name'])}."
        if open_items:
            text += f" {len(open_items)} item(s) in dispute wait in the safe, with deadlines."
        if lg["holdback"]:
            text += f" A {_fmt_money(lg['holdback'], asset)} holdback (5 %) stays in the safe for the warranty period."
        steps.append(_step("pay", "The seller was paid for what it delivered", "now" if open_items else "done",
                           text, ts=caps[-1]["ts"], tx=caps[-1]["payload"].get("tx_hash"),
                           deadline=lg["holdback_until"], dispute=bool(open_items)))
    void = first("escrow_voided")
    if void:
        steps.append(_step("return", "The rest went back to the buyer", "done",
                           f"{_fmt_money(lg['returned'], asset)} returned to {_name(buyer['name'])}.", ts=void["ts"],
                           tx=void["payload"].get("tx_hash")))
    if stopped and not lock:
        steps.append(_step("money", "No money moved", "skipped",
                           "The bank stopped this before anything was signed: balances are unchanged on chain."))

    money_now = ({"buyer_out": lg["total"], "in_escrow": lg["in_escrow"], "to_seller": lg["captured"],
                  "returned": lg["returned"], "holdback": lg["holdback"]} if lg else None)
    if stopped and not lock:
        headline, tone = f"Refused — {PLAIN.get(stopped['code'], stopped['code'])}. No money moved.", "bad"
    elif money_now and money_now["in_escrow"] == 0:
        headline, tone = f"Settled: {_fmt_money(money_now['to_seller'], asset)} to {seller['name']}", "ok"
    elif money_now:
        headline, tone = f"In progress: {_fmt_money(money_now['in_escrow'], asset)} still in the safe", "warn"
    elif deal.get("state") == "awaiting_human":
        headline, tone = "Waiting for a person to decide", "warn"
    elif any(s["status"] == "wait" for s in steps):  # an offer is on the table, the agent has not asked to pay yet
        headline, tone = "Price agreed — the bank decides before any money moves", "info"
    else:
        headline, tone = "Negotiating", "info"
    return {"deal": deal, "asset": asset, "seller": seller, "buyer": buyer, "steps": steps, "money": money_now,
            "headline": headline, "tone": tone, "item_count": (deal.get("offer") or {}).get("count"),
            "price": _fmt_money(deal["amount"], asset) if deal.get("amount") else None}


# ---- pages ----

@router.get("/")
def home():
    return RedirectResponse("/story", status_code=303)


@router.get("/story", response_class=HTMLResponse)
def story_page(request: Request, deal_id: str | None = None):
    from scripts.demo_scenarios import SCENARIOS
    recent = db.rows("SELECT d.deal_id, d.asset, d.state, c.name FROM deals d LEFT JOIN sellers s"
                     " ON s.seller_id=d.seller_id LEFT JOIN companies c ON c.id=s.company_id"
                     " ORDER BY d.created_at DESC LIMIT 8")
    return page(request, "story.html", s=story(deal_id), deal_id=deal_id, scenarios=SCENARIOS, recent=recent)


@router.get("/story/panel", response_class=HTMLResponse)
def story_panel(request: Request, deal_id: str | None = None):
    return templates.TemplateResponse(request, "_story.html", {"s": story(deal_id), "deal_id": deal_id})


@router.get("/bank", response_class=HTMLResponse)
def bank_page(request: Request, view: str = "client"):
    from scripts.demo_scenarios import SCENARIOS
    view = "compliance" if view == "compliance" else "client"
    return page(request, "bank.html", view=view, rows=feed(view), panel=panel(), alarms=alarms(),
                scenarios=SCENARIOS, names=_names())


@router.get("/bank/feed", response_class=HTMLResponse)
def bank_feed(request: Request, view: str = "client"):
    view = "compliance" if view == "compliance" else "client"
    return templates.TemplateResponse(request, "_feed.html", {"view": view, "rows": feed(view), "panel": panel(),
                                                              "alarms": alarms(), "names": _names()})


@router.get("/mandate", response_class=HTMLResponse)
def mandate_page(request: Request, mandate_id: str = DEMO_MANDATE, error: str = "", ok: str = ""):
    from bank import mandate
    current = mandate.current(mandate_id)
    pending = []
    for d in db.rows("SELECT * FROM deals WHERE mandate_id=? AND state='awaiting_human' ORDER BY created_at DESC",
                     (mandate_id,)):
        dec = db.one("SELECT * FROM decisions WHERE deal_id=? AND kind='pay' ORDER BY id DESC LIMIT 1",
                     (d["deal_id"],))
        if dec:
            pending.append({"deal": d, "decision": explain_row(dec, "compliance"), "seller": _seller(d["seller_id"])})
    return page(request, "mandate.html", mandate_id=mandate_id, current=current,
                versions=list(reversed(mandate.versions(mandate_id))) if current else [],
                summary=mandate.summary(mandate_id) if current else None, pending=pending, error=error, ok=ok,
                has_confirm=hasattr(deals, "confirm"))


@router.post("/mandate/deals/{deal_id}/{action}")
def mandate_action(deal_id: str, action: str, who: str = Form(...)):
    """The human in the bank UI confirms a payment above the threshold or acknowledges the §109 VAT risk
    (once per seller and asset). Same functions as POST /api/deals/{id}/confirm|ack-vat, called in-process:
    the browser never holds the API's human token."""
    deal = _deal_or_404(deal_id)
    if action not in ("confirm", "ack-vat") or not who.startswith("human:"):
        raise HTTPException(400, "unknown action")
    try:
        if action == "confirm":
            if not hasattr(deals, "confirm"):
                raise deals.DealError(501, "deals.confirm is not in this build yet")
            result = deals.confirm(deal_id, who)
        elif hasattr(deals, "ack_vat"):
            result = deals.ack_vat(deal_id, who)
        else:  # before the integrator's deals.ack_vat: acknowledge in the mandate, then pay again
            from bank import mandate
            mandate.acknowledge_vat(deal["mandate_id"], _seller(deal["seller_id"])["ico"], deal["asset"], who)
            result = deals.pay(deal_id)
    except deals.DealError as e:
        return RedirectResponse(f"/mandate?error={e}", status_code=303)
    view = codes.client_view(result)
    return RedirectResponse(f"/mandate?ok={view['status']}: {view['explanation'][:160]}", status_code=303)


@router.get("/deals", response_class=HTMLResponse)
def deals_page(request: Request):
    rows = db.rows("SELECT * FROM deals ORDER BY created_at DESC LIMIT 100")
    for r in rows:
        r["ledger"], r["dispute"] = ledger(r), disputes.get(r["deal_id"])
    return page(request, "deals.html", rows=rows, names=_names())


@router.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(request: Request, deal_id: str):
    from bank import verifier
    deal = _deal_or_404(deal_id)
    items, raw = _items(deal_id)
    decisions = [{"client": explain_row(r, "client"), "compliance": explain_row(r, "compliance"), "row": r}
                 for r in db.rows("SELECT * FROM decisions WHERE deal_id=? ORDER BY id", (deal_id,))]
    return page(request, "deal.html", deal=deal, seller=_seller(deal["seller_id"]), ledger=ledger(deal),
                items=items, raw=raw, criteria=verifier.criteria_of(deal["criteria"]) if deal["criteria"] else None,
                txs=db.rows("SELECT * FROM onchain_txs WHERE deal_id=? ORDER BY created_at", (deal_id,)),
                decisions=decisions, dispute=disputes.get(deal_id), events=journal.entries(deal_id, limit=80))


@router.post("/deals/{deal_id}/retry-delivery")
def retry_delivery(deal_id: str):
    """Funds are locked but no delivery came: deals.pay on an 'authorized' deal resends the escrow notice to the
    seller (no second lock, same idempotency keys)."""
    deal = _deal_or_404(deal_id)
    if deal["state"] != "authorized":
        raise HTTPException(409, f"deal is {deal['state']}, not waiting for a delivery")
    try:
        deals.pay(deal_id)
    except deals.DealError as e:
        journal.append("human:bank-officer", "delivery_retry_failed", {"error": str(e)[:200]}, deal_id=deal_id)
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


@router.get("/deals/{deal_id}/escrow-state", response_class=HTMLResponse)
def escrow_state(request: Request, deal_id: str):
    deal = _deal_or_404(deal_id)
    st, err = None, None
    try:
        st = escrow.read_state(deal["payment_hash"]) if deal["payment_hash"] else None
    except Exception as e:  # noqa: BLE001 — the chain may be unreachable: say so instead of failing the page
        err = f"{type(e).__name__}: {str(e)[:120]}"
    return templates.TemplateResponse(request, "_escrow_state.html",
                                      {"deal": deal, "st": st, "err": err, "ledger": ledger(deal)})


@router.get("/deals/{deal_id}/dispute", response_class=HTMLResponse)
def dispute_page(request: Request, deal_id: str, error: str = "", ok: str = ""):
    deal = _deal_or_404(deal_id)
    _, raw = _items(deal_id)
    seller = _seller(deal["seller_id"])
    return page(request, "dispute.html", deal=deal, d=disputes.get(deal_id), raw=raw, ledger=ledger(deal),
                error=error, ok=ok, criteria=disputes.allowed_criteria(deal), silence=disputes.silence_text,
                crit_text=criteria_text(deal),
                seller=seller, buyer_actor=f"human:{deal['buyer_id']}", seller_actor=f"human:{seller['id']}",
                officer=BANK_OFFICER,
                events=[e for e in journal.entries(deal_id, limit=200) if e["event"].startswith("dispute.")])


def criteria_text(deal: dict) -> dict:
    """Rule code -> what was agreed, so the card says which criterion an item violates."""
    from bank import verifier
    c = verifier.criteria_of(deal.get("criteria"))
    out = {"city": f"agreed: {c['city']} (postcode {c['postcode_prefix']}xx xx)",
           "min_rating": f"agreed: rating > {c['min_rating']}", "category": "agreed: " + ", ".join(c["category_keywords"]),
           "permanently_closed": "agreed: the place is open", "duplicate": "agreed: each place once",
           "over_quantity": f"agreed: {c['count']} items"}
    out.update({f"has_{f}": f"agreed: the item has a {f}" for f in c["required_fields"]})
    return out


def _dispute_action(deal_id: str, fn, *args):
    try:
        fn(deal_id, *args)
    except disputes.DisputeError as e:
        return RedirectResponse(f"/deals/{deal_id}/dispute?error={e}", status_code=303)
    return RedirectResponse(f"/deals/{deal_id}/dispute?ok=1", status_code=303)


@router.post("/deals/{deal_id}/dispute/{place_id}/review")
def dispute_review(deal_id: str, place_id: str, action: str = Form(...), who: str = Form(...),
                   criterion: str = Form(""), evidence: str = Form("")):
    return _dispute_action(deal_id, disputes.buyer_review, place_id, action, who, criterion or None, evidence)


@router.post("/deals/{deal_id}/dispute/{place_id}/objection")
def dispute_objection(deal_id: str, place_id: str, who: str = Form(...), statement: str = Form("")):
    return _dispute_action(deal_id, disputes.seller_objection, place_id, statement, who)


@router.post("/deals/{deal_id}/dispute/{place_id}/decide")
def dispute_decide(deal_id: str, place_id: str, verdict: str = Form(...), who: str = Form(BANK_OFFICER),
                   note: str = Form("")):
    return _dispute_action(deal_id, disputes.decide, place_id, verdict, who, note)


@router.get("/statement", response_class=HTMLResponse)
def statement_page(request: Request, mandate_id: str = DEMO_MANDATE):
    from bank import mandate
    rows = db.rows("SELECT * FROM deals WHERE mandate_id=? ORDER BY created_at", (mandate_id,))
    totals: dict[str, dict] = {}
    for r in rows:
        r["ledger"] = ledger(r)
        t = totals.setdefault(r["asset"] or "EURC", {"total": 0, "captured": 0, "returned": 0, "in_escrow": 0})
        t["total"] += r["ledger"]["total"] if r["ledger"]["locked"] else 0
        for k in ("captured", "returned", "in_escrow"):
            t[k] += r["ledger"][k]
    return page(request, "statement.html", mandate_id=mandate_id, rows=rows, totals=totals, names=_names(),
                summary=mandate.summary(mandate_id))


@router.get("/statement/reconcile", response_class=HTMLResponse)
def reconcile(request: Request, mandate_id: str = DEMO_MANDATE):
    """Our ledger vs the escrow's paymentState on chain: capturable must equal what we say is still locked."""
    out = []
    for r in db.rows("SELECT * FROM deals WHERE mandate_id=? AND payment_hash IS NOT NULL ORDER BY created_at",
                     (mandate_id,)):
        lg = ledger(r)
        if not lg["locked"]:
            continue
        try:
            st = escrow.read_state(r["payment_hash"])
            out.append({"deal": r, "ours": lg["in_escrow"], "chain": st["capturable"],
                        "ok": st["capturable"] == lg["in_escrow"], "err": None})
        except Exception as e:  # noqa: BLE001
            out.append({"deal": r, "ours": lg["in_escrow"], "chain": None, "ok": None, "err": type(e).__name__})
    return templates.TemplateResponse(request, "_reconcile.html", {"rows": out})


@router.post("/demo/{scenario}")
def demo(scenario: str, next: str = "/bank"):
    from scripts import demo_scenarios
    if scenario not in demo_scenarios.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    threading.Thread(target=demo_scenarios.run_safe, args=(scenario,), daemon=True).start()
    return RedirectResponse(next if next in ("/bank", "/story") else "/bank", status_code=303)  # no open redirect


@router.post("/timer/tick")
def timer_tick():
    """One timer pass now (the demo need not wait up to 5 s)."""
    deadlines.tick()
    return RedirectResponse("/bank", status_code=303)
