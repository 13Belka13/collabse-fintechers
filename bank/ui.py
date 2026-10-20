# Bank UI (SPEC §13): server-rendered Jinja2 + HTMX, no JS build. The bank's decision and where the money is
# are the hero. Reads tables + the journal; writes only through module functions (bank.disputes, bank.receipts for
# the buyer's human actions). Client texts always pass codes.client_view; AI-generated text carries the AI Act
# Art. 50 label. The router's lifespan starts the deal timer (bank/deadlines.py) inside the bank process.
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from bank import codes, config, db, deadlines, deals, disputes, escrow, explain, journal, money, ramp, receipts, timing
from bank.screening import website
from bank.ui_auth import same_origin

DEMO_MANDATE = "m-alfa-1"
BANK_OFFICER = "human:bank-officer"
TRANSFER_EVENTS = {"escrow_authorized": "Locked in escrow", "escrow_captured": "Paid to the seller",
                   "escrow_voided": "Returned to the buyer", "x402_settled": "x402 payment settled",
                   "payout_to_account": "Paid out to the seller's bank account"}
INFO_EVENTS = {"settlement.opened", "delivery.autocodes", "delivery.refused", "delivery.accepted_anyway",
               "claim_b.filed", "settlement.accepted", "settlement.closed", "settlement.tx_failed",
               "escrow_expiry_alarm", "timer_error", "x402_refused", "demo.scenario"}
BAD_EVENTS = {"escrow_expiry_alarm", "timer_error", "settlement.tx_failed", "x402_refused", "delivery.refused"}
INFO_EVENTS |= {"agent_unavailable", "seller_unavailable"}  # a slow / failing model or seller shows, never silent
BAD_EVENTS |= {"agent_unavailable", "seller_unavailable"}
INFO_EVENTS.add("settlement.stuck")  # the timer failed a deal STUCK_AFTER times in a row (bank.deadlines)
BAD_EVENTS.add("settlement.stuck")
STEP_TEXT = {"delivery": "Waiting for the delivery", "window": "Claim window open", "settling": "Settling",
             "dispute_c": "C dispute: only the disputed sum stays in escrow", "closed": "Settled"}


@asynccontextmanager
async def lifespan(app):
    if os.getenv("DEMO_SIM") == "1":
        from scripts import demo_sim
        demo_sim.install()
    if os.getenv("BANK_TIMER", "1") != "0":
        deadlines.start()
    yield
    deadlines.stop()


# every POST here is a person's action: same-origin or the human token (bank/ui_auth.py); pages stay open
router = APIRouter(lifespan=lifespan, include_in_schema=False, dependencies=[Depends(same_origin)])
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
from bank import economics  # noqa: E402 — the "what this deal cost" panel (_economics.html)
templates.env.globals["economics"] = economics


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
            # the client got the neutral text instead of this code or of a non-public reason (§6.7, no tipping-off)
            "hidden_from_client": client["code"] == codes.Code.UNDER_REVIEW and (
                d["code"] != codes.Code.UNDER_REVIEW or any(r.get("public") is not True for r in d["reasons"]))}


def ledger(deal: dict) -> dict:
    """Where a deal's money is: the settlement record when there is one, else the journal's escrow_* events."""
    total = int(deal.get("amount") or 0)
    ev = journal.entries(deal["deal_id"], limit=1000)
    locked = any(e["event"] == "escrow_authorized" for e in ev)
    s = disputes.get(deal["deal_id"])
    if s is not None:
        mv = disputes.money_view(deal, s)
        return {"total": total, "locked": locked, "captured": mv["captured"], "returned": mv["returned"],
                "in_escrow": mv["in_escrow"] if locked else 0}
    captured = sum(int(e["payload"].get("amount", 0)) for e in ev if e["event"] == "escrow_captured")
    returned = total - captured if any(e["event"] == "escrow_voided" for e in ev) else 0
    return {"total": total, "locked": locked, "captured": captured, "returned": returned,
            "in_escrow": (total - captured - returned) if locked else 0}


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
             for e in entries if e["event"] in ("settlement.capture", "settlement.void")}
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
            "llm_today": timing.llm_spent_today(), "llm_daily": config.LLM_DAILY_USD, "timer": deadlines.running()}


def alarms(now: float | None = None) -> list[dict]:
    """Open deals whose escrow can no longer be paid out within the hour (§9.6), and open deals the timer could
    not move (settlement.stuck)."""
    now = time.time() if now is None else now
    out = []
    for d in db.rows("SELECT deal_id, state, payment_info_json FROM deals WHERE payment_info_json IS NOT NULL"
                     " AND state IN ('authorized','delivered','dispute_c')"):
        exp = int((db.loads(d["payment_info_json"]) or {}).get("authorizationExpiry") or 0)
        if exp and exp - now < deadlines.ALARM_S:
            out.append({"deal_id": d["deal_id"], "state": d["state"], "left": max(0, int(exp - now)), "expiry": exp})
    for d in db.rows("SELECT DISTINCT j.deal_id, d.state FROM journal j JOIN deals d ON d.deal_id = j.deal_id"
                     " JOIN settlements s ON s.deal_id = j.deal_id WHERE j.event='settlement.stuck' AND s.closed_at IS NULL"):
        out.append({"deal_id": d["deal_id"], "state": d["state"], "stuck": True})
    return out


def _deal_or_404(deal_id: str) -> dict:
    deal = deals.get(deal_id)
    if deal is None:
        raise HTTPException(404, "unknown deal")
    return deal


def human_of(company_id: str) -> str:
    """The actor of a person's action in the UI: the logged-in demo person stands in for the company's authorised
    person. Set by the server from the deal or mandate, never taken from the form."""
    return f"human:{company_id}"


def _owner(mandate_id: str) -> str | None:
    row = db.one("SELECT company_id FROM mandates WHERE mandate_id=?", (mandate_id,))
    return row["company_id"] if row else None


def _seller(seller_id: str) -> dict:
    return db.one("SELECT c.id, c.name, c.ico FROM sellers s JOIN companies c ON c.id = s.company_id"
                  " WHERE s.seller_id=?", (seller_id,)) or {"id": seller_id, "name": seller_id, "ico": None}


def _names() -> dict:
    return {r["seller_id"]: r["name"] for r in db.rows(
        "SELECT s.seller_id, c.name FROM sellers s JOIN companies c ON c.id = s.company_id")}


def mask(value) -> str:
    """A rejected item's field as the buyer's person sees it (§5.2 p. 7): enough to recognise, not the data."""
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= 4 else f"{text[:3]}…({len(text)} chars)"


def _items(deal: dict) -> list[dict]:
    """Every delivered item with its autocode, the violated predicate in words and the masked field value."""
    from bank import verifier
    out = []
    for r in receipts.items_of(deal["deal_id"]):
        p = r["predicate"]
        field = p and (p.get("field") or p.get("ico_field") or p.get("key_field"))
        item = r["item"] if isinstance(r["item"], dict) else {}
        out.append({**r, "label": item.get("name") or item.get("title") or item.get("placeId") or f"#{r['idx']}",
                    "key": item.get("ico") or item.get("placeId"), "rule_text": verifier.describe(p) if p else "",
                    "masked": mask(verifier.get_field(item, field)[1]) if field else ""})
    return out


# ---- the story of one deal, for people watching the demo (/story) ----

PLAIN = {  # decision codes in words for the audience; the code itself stays visible next to it
    "MANDATE_NOT_ACTIVE": "the mandate is not active", "MANDATE_EXPIRED": "the mandate has expired",
    "CATEGORY_NOT_IN_SCOPE": "outside what the mandate allows", "COUNTERPARTY_NOT_IN_SCOPE": "money was asked for a wrong account",
    "COUNTERPARTY_NOT_TRUSTED": "the seller failed the bank's checks", "AMOUNT_ABOVE_LIMIT": "the amount is above the limit",
    "BUDGET_EXCEEDED": "the monthly budget would be exceeded", "QUOTE_MISMATCH": "the payment request differs from the deal",
    "VELOCITY_EXCEEDED": "too many payments too fast", "CONFIRMATION_REQUIRED": "a person must confirm this amount",
    "VAT_LIABILITY_RISK": "Czech VAT liability risk (§109)", "UNDER_REVIEW": "the payment is under review at the bank",
    "TERMS_MISMATCH": "the offer's terms fall short of the confirmed task terms",
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

    # 2b. the counterparty check, prefetched during the talk: registries + our Apify actor (public facts only)
    web = website.latest(seller.get("ico"))
    if web and not web["hidden"]:
        steps.append(_step("check", "The bank checked the seller", "done", "ARES, VAT payers, ISIR, EU sanctions and "
                           "the web footprint.", apify=web))

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
    # 5. the delivery through the bank, checked by the confirmed predicates (code, no model)
    checks = every("delivery.autocodes")
    if checks:
        c = checks[-1]["payload"]
        ordered = (deal.get("terms") or {}).get("count", 0)
        parts = [f"{c.get('accepted_items', 0)} of {ordered} items passed the agreed conditions"]
        if c.get("rejected_items"):
            parts.append(f"{c['rejected_items']} rejected (never handed to the buyer)")
        if c.get("pending_items"):
            parts.append(f"{c['pending_items']} wait for a registry or source answer")
        steps.append(_step("verify", f"The seller delivered through the bank ({len(checks)} version"
                           f"{'s' if len(checks) != 1 else ''})", "done", ", ".join(parts) + ".", ts=checks[-1]["ts"]))
    # 6. payouts by the deal's rule, the final void
    lg = ledger(deal) if lock else None
    caps = every("escrow_captured")
    s_ = disputes.get(did)
    if caps:
        open_ = s_ is not None and not s_["closed_at"]
        text = f"{_fmt_money(lg['captured'], asset)} paid to {_name(seller['name'])}"
        if s_ is not None:
            text += f" under rule {s_['rule']}" + (f" ({s_['b_pct']} % guarantee)" if s_["b_pct"] else "")
        text += "."
        if open_ and s_["window_end_at"]:
            text += " The rest waits in the safe until the claim window ends."
        steps.append(_step("pay", "The seller was paid for accepted items", "now" if open_ else "done",
                           text, ts=caps[-1]["ts"], tx=caps[-1]["payload"].get("tx_hash"),
                           deadline=s_["window_end_at"] if open_ and s_ else None))
    po = ramp.deal_view(deal) if caps else None
    steps += _payout_steps(po, seller, s_)
    for cl in (s_ or {}).get("claims", []):
        steps.append(_step("claim", f"The buyer filed a {cl['rule']} claim", "done",
                           f"{len(cl['items'])} item(s), {_fmt_money(cl['amount'], asset)} back to the buyer "
                           "without investigation (rule B)." if cl["rule"] == "B" else f"{len(cl['items'])} item(s).",
                           ts=cl["filed_at"]))
    void = first("escrow_voided")
    if void:
        steps.append(_step("return", "The rest went back to the buyer", "done",
                           f"{_fmt_money(lg['returned'], asset)} returned to {_name(buyer['name'])} in one final void.",
                           ts=void["ts"], tx=void["payload"].get("tx_hash")))
    if stopped and not lock:
        steps.append(_step("money", "No money moved", "skipped",
                           "The bank stopped this before anything was signed: balances are unchanged on chain."))

    money_now = ({"buyer_out": lg["total"], "in_escrow": lg["in_escrow"], "to_seller": lg["captured"],
                  "returned": lg["returned"], "on_account": po["paid_out"] if po else None,
                  "account": po and po["account"]} if lg else None)
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


def _payout_steps(po: dict | None, seller: dict, s_: dict | None) -> list[dict]:
    """tCZK paid on to the seller's bank account (bank/ramp.py): shown only for a seller with payout_mode='account'."""
    if po is None:
        return []
    if po["off"]:
        return [_step("payout", "The crowns stay in the seller's wallet", "skipped", f"Payout to the account is off: {po['off']}.")]
    steps = []
    if po["last"]:
        more = s_ is not None and not s_["closed_at"]
        steps.append(_step("payout", f"The seller received {_fmt_money(po['paid_out'], 'tCZK')} on its bank account",
                           "now" if more else "done",
                           f"{_fmt_money(po['paid_out'], 'tCZK')} (what reached its wallet after the bank's fee) credited "
                           f"to account {po['account']} of {_name(seller['name'])}. The bank burnt the tCZK and paid out crowns: "
                           "the seller never had to hold the token." + (" The rest follows when the deal settles." if more else ""),
                           ts=po["last"]["updated_at"], tx=po["last"]["tx_hash"]))
    for o in po["open"]:
        steps.append(_step("payout_open", f"{_fmt_money(o['amount'], 'tCZK')} is in the seller's wallet, payout not done",
                           "blocked" if o["state"] == "failed" else "now",
                           f"The capture stands; the payout to account {po['account']} "
                           + ("failed and can be retried." if o["state"] == "failed" else "waits for the burn's receipt."),
                           ts=o["updated_at"], tx=o["tx_hash"], retry=o["ref"]))
    return steps


# ---- pages ----

@router.get("/")
def home():
    return RedirectResponse("/story", status_code=303)


@router.get("/story", response_class=HTMLResponse)
def story_page(request: Request, deal_id: str | None = None, busy: str = "", low: str = ""):
    from scripts import demo_scenarios as ds
    sc = ds.SCENARIOS.get(low)  # a scripted run the buyer's wallet could not pay: say why
    error = (ds.balance_problem(sc["asset"], SCRIPTED_BUDGET_EUR_CENTS) or "") if sc else ""
    return _story_page(request, deal_id, busy=bool(busy), error=error)


def _story_page(request: Request, deal_id: str | None = None, busy=False, form: dict | None = None, error=""):
    from scripts import demo_scenarios as ds
    recent = db.rows("SELECT d.deal_id, d.asset, d.state, c.name FROM deals d LEFT JOIN sellers s"
                     " ON s.seller_id=d.seller_id LEFT JOIN companies c ON c.id=s.company_id"
                     " ORDER BY d.created_at DESC LIMIT 8")
    form = form or {"task": ds.TASK, "budget": "5", "asset": "tCZK", "seller": "", "mode": "honest", "say": "",
                    "opening": "list", "data": "companies", "rule": ""}
    bal = ds.buyer_balances()
    wallet = [f"{v / 10 ** money.decimals(a):.2f} {a}" for a, v in bal["values"].items()]
    return page(request, "story.html", s=story(deal_id), deal_id=deal_id, scenarios=ds.SCENARIOS, recent=recent,
                run=run_view(), busy=busy, error=error, form=form, ds=ds, wallet=wallet,
                wallet_age=int(time.time() - bal["at"]) if bal["at"] else None)


@router.get("/story/panel", response_class=HTMLResponse)
def story_panel(request: Request, deal_id: str | None = None):
    return templates.TemplateResponse(request, "_story.html", {"s": story(deal_id), "deal_id": deal_id,
                                                               "run": run_view()})


@router.get("/bank", response_class=HTMLResponse)
def bank_page(request: Request, view: str = "client"):
    from scripts.demo_scenarios import SCENARIOS
    from scripts.demo_scenarios import busy as run_busy
    view = "compliance" if view == "compliance" else "client"
    return page(request, "bank.html", view=view, rows=feed(view), panel=panel(), alarms=alarms(),
                scenarios=SCENARIOS, names=_names(), busy=run_busy())


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
                has_confirm=hasattr(deals, "confirm"), owner=_owner(mandate_id))


LIMIT_FIELDS = {"per_deal_max": "The per-deal limit", "confirm_above": "The confirmation threshold",
                "budget": "The monthly limit", "task_budget_max": "The task budget cap"}


def limits_from_form(form: dict, current: dict) -> dict:
    """The mandate form (euros) -> policy changes in euro cents, with plain messages (ValueError) for the page.
    mandate.amend validates again and stores a new immutable version with a new policy_hash."""
    out = {}
    for key, label in LIMIT_FIELDS.items():
        raw = str(form.get(key) or "").strip().replace(",", ".").replace("€", "").strip()
        if not raw:
            if key == "task_budget_max":
                out[key] = None  # no cap beyond each task's own budget
                continue
            raise ValueError(f"{label} is required.")
        try:
            c = round(float(raw) * 100)
        except ValueError:
            raise ValueError(f"{label} must be a number of euros.") from None
        if not 1 <= c <= 10_000_000:
            raise ValueError(f"{label} must be between 0,01 € and 100 000 €.")
        out[key] = str(c)
    per_deal, confirm, month = (int(out[k]) for k in ("per_deal_max", "confirm_above", "budget"))
    if confirm >= per_deal:
        raise ValueError("The confirmation threshold must be below the per-deal limit, otherwise a person is never asked.")
    if per_deal > month:
        raise ValueError("The per-deal limit must not exceed the monthly limit.")
    if out["task_budget_max"] is not None and int(out["task_budget_max"]) > month:
        raise ValueError("The task budget cap must not exceed the monthly limit.")
    assets = [a for a in config.ASSETS if form.get(f"asset_{a}")]
    if not assets:
        raise ValueError("Allow at least one asset (EURC or tCZK).")
    policy = current["policy"]
    changes = {"per_deal_max": {**policy["per_deal_max"], "value": out["per_deal_max"]},
               "confirm_above": {**policy["confirm_above"], "value": out["confirm_above"]},
               "budget": {**policy["budget"], "value": out["budget"]}, "assets_allowed": assets,
               "task_budget_max": out["task_budget_max"] and {"value": out["task_budget_max"], "currency": "EUR"}}
    return {k: v for k, v in changes.items() if v != policy.get(k)}


@router.post("/mandate/edit")
async def mandate_edit(request: Request):
    """A person sets new limits: a new mandate version (policy_hash) that the next pay() enforces. The agent has
    no tool for this. The actor is the mandate owner's person, set here: a "who" in the form is ignored."""
    from bank import mandate
    form = dict(await request.form())
    mandate_id = str(form.get("mandate_id") or DEMO_MANDATE)
    current = mandate.current(mandate_id)
    if current is None:
        raise HTTPException(404, "unknown mandate")
    who = human_of(_owner(mandate_id))
    try:
        changes = limits_from_form(form, current)
        if not changes:
            return RedirectResponse(f"/mandate?mandate_id={mandate_id}&ok=Nothing changed: same limits.",
                                    status_code=303)
        version = mandate.amend(mandate_id, changes, who)
    except ValueError as e:
        return RedirectResponse(f"/mandate?mandate_id={mandate_id}&error={str(e)[:200]}", status_code=303)
    return RedirectResponse(f"/mandate?mandate_id={mandate_id}&ok=Version {version} is in force: the next payment "
                            "is checked against it.", status_code=303)


@router.post("/mandate/deals/{deal_id}/{action}")
def mandate_action(deal_id: str, action: str):
    """The human in the bank UI confirms a payment above the threshold or acknowledges the §109 VAT risk
    (once per seller and asset). Same functions as POST /api/deals/{id}/confirm|ack-vat, called in-process:
    the browser never holds the API's human token. The actor is the buyer's person, set here."""
    deal = _deal_or_404(deal_id)
    if action not in ("confirm", "ack-vat"):
        raise HTTPException(400, "unknown action")
    who = human_of(deal["buyer_id"])
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
        r["ledger"], r["settlement"] = ledger(r), disputes.get(r["deal_id"])
    return page(request, "deals.html", rows=rows, names=_names())


@router.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(request: Request, deal_id: str, error: str = "", ok: str = ""):
    """The deal page a human can follow (§11): the conditions in words and who confirmed them, what came, what
    failed and why, where the money went, what happens if nobody acts and until when."""
    from bank import verifier
    deal = _deal_or_404(deal_id)
    decisions = [{"client": explain_row(r, "client"), "compliance": explain_row(r, "compliance"), "row": r}
                 for r in db.rows("SELECT * FROM decisions WHERE deal_id=? ORDER BY id", (deal_id,))]
    task, terms = deal.get("criteria") or {}, deal.get("terms") or {}
    confirmed = {receipts.jcs(p) for p in task.get("predicates") or []}
    conditions = [{"text": verifier.describe(p), "by_seller": receipts.jcs(p) not in confirmed}
                  for p in (terms.get("predicates") or (deal.get("offer") or {}).get("predicates")
                            or task.get("predicates") or [])]
    s = disputes.get(deal_id)
    from bank import claims_c
    seller = _seller(deal["seller_id"])
    return page(request, "deal.html", deal=deal, seller=seller, ledger=ledger(deal), c=claims_c.view(deal_id),
                web=website.latest(seller.get("ico")), payout=ramp.deal_view(deal),
                items=_items(deal), conditions=conditions, terms=terms, task=task, s=s,
                silence=disputes.silence_text(deal, s) if s else "", scale=disputes.config_note(),
                business_days=_business_days(s), step_text=STEP_TEXT, error=error, ok=ok,
                attempts=db.rows("SELECT * FROM delivery_attempts WHERE deal_id=? ORDER BY id", (deal_id,)),
                txs=db.rows("SELECT * FROM onchain_txs WHERE deal_id=? ORDER BY created_at", (deal_id,)),
                decisions=decisions, events=journal.entries(deal_id, limit=80))


@router.get("/deciders/{deal_id}", response_class=HTMLResponse)
def decider_page(request: Request, deal_id: str, error: str = "", ok: str = ""):
    """The decider's case (§7C.6): pseudonyms instead of companies, the sampled items with the item's value and the
    value the bank read from its own snapshot at delivery. In the demo a person on stage holds the decider keys."""
    from bank import claims_c, deciders
    _deal_or_404(deal_id)
    c = claims_c.get(deal_id)
    if c is None:
        raise HTTPException(404, "no C claim")
    case = deciders.get(c["id"]) or {}
    sampled = [dict(c["items"][i], n=i) for i in (case.get("sample") or [])]
    return page(request, "decider.html", deal_id=deal_id, c=c, case=case, sampled=sampled, error=error, ok=ok,
                deadline=deciders.deadline(case) if case.get("sample") is not None else None)


@router.post("/deciders/{deal_id}")
async def decider_decide(request: Request, deal_id: str):
    """The decider signs with its own token (bank/deciders.py tokens): the decider id comes from the token."""
    from bank import claims_c, deciders
    form = await request.form()
    decider = deciders.by_token(str(form.get("token", "")).strip())
    if decider is None:
        return RedirectResponse(f"/deciders/{deal_id}?error=Unknown decider token: nothing was decided.",
                                status_code=303)
    verdicts = {k.removeprefix("item-"): v for k, v in form.items() if k.startswith("item-")}
    try:
        claims_c.decide(deal_id, decider, verdicts)
    except (claims_c.ClaimError, deciders.DeciderError) as e:
        return RedirectResponse(f"/deciders/{deal_id}?error={e}", status_code=303)
    return RedirectResponse(f"/deciders/{deal_id}?ok=decided", status_code=303)


def _business_days(s: dict | None) -> int | None:
    """'Up to N business days' for every deal (§8.4: an AML hold must not stand out)."""
    if s is None or s["closed_at"]:
        return None
    end = max(x for x in (s["deadline_at"], s["window_end_at"] or 0))
    return max(1, int((end - time.time()) // config.BUSINESS_DAY_S) + 1)


@router.post("/deals/{deal_id}/accept")
def accept_deal(deal_id: str):
    """Rule A: a person of the buyer confirms acceptance before the delivery deadline."""
    deal = _deal_or_404(deal_id)
    try:
        disputes.accept(deal_id, human_of(deal["buyer_id"]))
    except disputes.DisputeError as e:
        return RedirectResponse(f"/deals/{deal_id}?error={e}", status_code=303)
    return RedirectResponse(f"/deals/{deal_id}?ok=accepted", status_code=303)


@router.post("/deals/{deal_id}/accept-item/{version}/{idx}")
def accept_item(deal_id: str, version: int, idx: int):
    """'Accept anyway' for one rejected item: only a person, within the lock, before the final void."""
    deal = _deal_or_404(deal_id)
    who = human_of(deal["buyer_id"])
    s = disputes.get(deal_id)
    try:
        if s is None or s["voided"] or s["closed_at"]:
            raise receipts.Refused(409, "the deal is settled")
        receipts.waive(deal_id, [(version, idx)], who, int(deal["terms"]["count"]))
    except receipts.Refused as e:
        return RedirectResponse(f"/deals/{deal_id}?error={e}", status_code=303)
    disputes.advance(deal_id, actor=who)
    return RedirectResponse(f"/deals/{deal_id}?ok=item accepted", status_code=303)


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


@router.get("/statement", response_class=HTMLResponse)
def statement_page(request: Request, mandate_id: str = DEMO_MANDATE, error: str = "", ok: str = ""):
    from bank import mandate
    rows = db.rows("SELECT * FROM deals WHERE mandate_id=? ORDER BY created_at", (mandate_id,))
    totals: dict[str, dict] = {}
    for r in rows:
        r["ledger"] = ledger(r)
        t = totals.setdefault(r["asset"] or "EURC", {"total": 0, "captured": 0, "returned": 0, "in_escrow": 0})
        t["total"] += r["ledger"]["total"] if r["ledger"]["locked"] else 0
        for k in ("captured", "returned", "in_escrow"):
            t[k] += r["ledger"][k]
    owner = db.one("SELECT company_id FROM mandates WHERE mandate_id=?", (mandate_id,))
    return page(request, "statement.html", mandate_id=mandate_id, rows=rows, totals=totals, names=_names(),
                summary=mandate.summary(mandate_id), accounts=ramp.accounts(), ramp_ops=ramp.ops(limit=20),
                ramp_off=ramp.off_reason(), owner=owner["company_id"] if owner else "alfa", error=error, ok=ok)


@router.post("/ramp/{action}")
def ramp_action(action: str, company_id: str = Form(""), amount: str = Form(""),
                ref: str = Form(""), next: str = Form("/statement")):
    """A person moves crowns between the company's bank account and its wallet (deposit = mint, withdraw = burn), or
    a bank officer retries a payout. Human only, like the other bank-UI actions (same-origin, actor set by the
    server); the agent has no tool for this. The logged-in person acts for the demo mandate's company only."""
    back = next if next.startswith("/deals/") or next in ("/statement", "/story") else "/statement"
    sep = "&" if "?" in back else "?"
    if action not in ("deposit", "withdraw", "retry"):
        raise HTTPException(400, "unknown action")
    owner = _owner(DEMO_MANDATE)
    if action != "retry" and company_id != owner:
        raise HTTPException(403, "a person moves only their own company's crowns")
    who = BANK_OFFICER if action == "retry" else human_of(owner)
    try:
        if action == "retry":
            o = ramp.retry(ref, who)
        else:
            atomic = money.to_atomic("tCZK", amount.replace(" ", "").replace(",", "."))
            o = (ramp.deposit if action == "deposit" else ramp.withdraw)(company_id, atomic, who)
    except ramp.RampError as e:
        return RedirectResponse(f"{back}{sep}error={e}", status_code=303)
    except (ValueError, ArithmeticError):
        return RedirectResponse(f"{back}{sep}error=Enter an amount in Kč, e.g. 100,00.", status_code=303)
    what = f"{o['direction']} {money.fmt('tCZK', o['amount'])}"
    if o["state"] == "failed":
        return RedirectResponse(f"{back}{sep}error={what} failed on chain, nothing moved", status_code=303)
    text = "done" if o["state"] == "done" else "sent, waiting for the receipt"
    return RedirectResponse(f"{back}{sep}ok={what}: {text}", status_code=303)


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
    return templates.TemplateResponse(request, "_reconcile.html", {"rows": out, "supply": ramp.reconcile()})


# ---- the demo console: scripted scenarios and the viewer's own deal, one run at a time ----

TOOL_TEXT = {"find_seller": ("Our agent looks for a data seller", "the agent looks for a seller"),
             "check_seller": ("Our agent asks the bank to check the seller", "the bank checks the seller"),
             "pay": ("Our agent asks the bank to pay", "the bank decides"),
             "fetch_delivery": ("Our agent fetches the delivery through the bank", "the delivery is checked"),
             "file_claim": ("Our agent files a claim", "a claim is filed"),
             "get_statement": ("Our agent reads the mandate's statement", None)}
WAITING_S = 8  # no new journal entry for this long while running: "waiting for the model…"


def run_steps(since_id: int) -> dict:
    """The current run's journal in plain steps for the live banner. Decisions pass codes.client_view (what the
    audience sees is what the client may see); the seller's words are marked as untrusted data."""
    rows = db.rows("SELECT * FROM journal WHERE id>? ORDER BY id LIMIT 600", (since_id,))
    ours = [r["deal_id"] for r in rows if r["event"] == "deal_created" and r["deal_id"]]
    asset_of = {d: "EURC" for d in ours}
    asset_of.update({r["deal_id"]: r["asset"] or "EURC" for r in db.rows(
        "SELECT deal_id, asset FROM deals WHERE deal_id IN (%s)" % ",".join("?" * len(ours)), tuple(ours))} if ours else {})
    names, steps, turn, seen, paying = _names(), [], 0, set(), False

    def add(r, text, tone="info", phase=None, **kw):
        steps.append({"ts": r["ts"], "time": _fmt_ts(r["ts"]), "text": text, "tone": tone, "phase": phase, **kw})

    for r in rows:
        did, e, p = r["deal_id"], r["event"], db.loads(r["payload_json"]) or {}
        if did and did not in asset_of:
            continue  # another deal's timer or settlement, not this run
        a = asset_of.get(did, "EURC")
        if e == "agent_turn" and r["actor"] == "agent:buyer":
            for tc in p.get("tool_calls") or []:
                inp = tc.get("input") or {}
                if tc.get("name") == "negotiate":
                    turn += 1
                    add(r, f"Our agent → {names.get(inp.get('seller_id'), 'the seller')}: “{str(inp.get('message', ''))[:300]}”",
                        phase=f"agents are negotiating (turn {turn})", who="buyer")
                else:
                    text, phase = TOOL_TEXT.get(tc.get("name"), (f"Our agent calls {tc.get('name')}", None))
                    seller = names.get(inp.get("seller_id"))
                    add(r, text + (f" ({seller})" if seller else ""), phase=phase)
        elif e == "seller_reply" and r["actor"] == "agent:seller":
            add(r, f"The seller's agent: “{str(p.get('reply_text') or '')[:600]}”", "untrusted",
                f"agents are negotiating (turn {max(turn, 1)})", who="seller", untrusted=True)
        elif e == "offer_stored":
            add(r, f"Offer on the table: {_fmt_money(p.get('amount'), a)}, rule {p.get('rule') or '—'}",
                phase=f"agents are negotiating (turn {max(turn, 1)})")
        elif e == "screening.checked" and p.get("level") == "ok" and p.get("ico") not in seen and not paying:
            seen.add(p.get("ico"))  # the seller's prefetch; the delivered items' checks show as the delivery result
            add(r, f"The bank checked IČO {p.get('ico')} in the public registries: passed", "ok")
        elif e == "quote_accepted":
            paying = True
        elif e == "budget_reserved":
            add(r, f"The bank approved the payment (mandate version {p.get('version')})", "ok",
                "the money goes into escrow")
        elif e in ("pay_refused", "pay_requires_user_action", "x402_refused"):
            v = codes.client_view({"code": p.get("code"), "reasons": p.get("reasons") or []})
            plain = PLAIN.get(v["code"], v["code"])
            if e == "pay_requires_user_action":
                add(r, f"The bank asks a person first: {plain} ({v['code']}). Decide on the Mandate page.", "warn",
                    "waiting for a person", link="/mandate")
            else:
                add(r, f"The bank refused: {plain} ({v['code']}). No money moved.", "bad", "refused")
        elif e == "escrow_authorized":
            add(r, f"{_fmt_money(p.get('amount'), a)} locked in escrow", "ok", "the seller delivers through the bank")
        elif e == "delivery.autocodes":
            add(r, f"Delivery checked by code: {p.get('accepted_items', 0)} accepted, "
                   f"{p.get('rejected_items', 0)} rejected", "ok", "the money moves by the deal's rule")
        elif e == "escrow_captured":
            add(r, f"{_fmt_money(p.get('amount'), a)} paid to the seller", "ok", "the money moves by the deal's rule")
        elif e == "escrow_voided":
            add(r, "The unearned rest went back to the buyer (final void)", "ok")
        elif e in ("vat_risk_acknowledged", "payment_confirmed"):
            add(r, "A person " + ("acknowledged the VAT risk" if e == "vat_risk_acknowledged"
                                  else "confirmed the payment"), "ok")
        elif e == "demo.reset_failed":
            add(r, f"Could not reset the behaviour of {', '.join(p.get('sellers') or [])}", "bad")
        elif e == "demo.scenario":
            text, tone = outcome_text(p.get("outcome"))
            add(r, f"Finished: {text}", tone)
    return {"steps": steps, "turn": turn, "deals": ours}


def outcome_text(outcome: str | None) -> tuple[str, str]:
    """A run's outcome in plain words + tone."""
    if not outcome:
        return "no result yet", "info"
    if outcome.startswith("error"):
        return f"the run stopped with an error ({outcome.removeprefix('error: ')}). Nothing else was paid.", "bad"
    if outcome == "ok":
        return "the bank approved the payment; the money moves by the deal's rule.", "ok"
    if outcome in ("VAT_LIABILITY_RISK", "CONFIRMATION_REQUIRED"):
        return f"the bank stopped and asked a person: {PLAIN[outcome]}. Decide on the Mandate page.", "warn"
    if outcome in PLAIN:
        return f"the bank refused: {PLAIN[outcome]}. No money moved.", "bad"
    return {"done": ("the agent ended without paying.", "info"),
            "turn_limit": ("the agent stopped after too many turns; nothing was paid.", "warn"),
            "model_refused": ("the model refused to continue; nothing was paid.", "warn")}.get(
        outcome, (outcome, "info"))


def run_view(now: float | None = None) -> dict | None:
    """The current or last demo run for /story and GET /demo/run (None before the first run)."""
    from scripts import demo_scenarios as ds
    r = dict(ds.RUN)
    if not r.get("started"):
        return None
    now = time.time() if now is None else now
    x = run_steps(r.get("since_id") or 0)
    running = not r.get("finished")
    elapsed = (r.get("finished") or now) - r["started"]
    idle = now - max([s["ts"] for s in x["steps"]] + [r["started"]])
    phase = next((s["phase"] for s in reversed(x["steps"]) if s.get("phase")), "starting")
    text, tone = outcome_text(r.get("outcome")) if not running else ("", "info")
    return {"name": r.get("name"), "title": r.get("title"), "params": r.get("params"), "started": r["started"],
            "finished": r.get("finished"), "elapsed_s": round(elapsed, 1), "outcome": r.get("outcome"),
            "agent_text": r.get("agent_text"), "steps": x["steps"], "turn": x["turn"], "deals": x["deals"],
            "running": running, "phase": phase, "waiting": running and idle > WAITING_S, "ai": not config.MOCK,
            "slow": running and elapsed > ds.RUN_TIMEOUT_S, "outcome_text": text, "tone": tone,
            "deal_id": (x["deals"] or [None])[-1]}


SCRIPTED_BUDGET_EUR_CENTS = 500  # the scripted buyer's task budget (agents/buyer.py default)


def _start(name: str, params: dict | None = None) -> bool:
    """Start a run in a daemon thread unless one is running (the request never waits for the model)."""
    from scripts import demo_scenarios as ds
    if ds.busy():
        return False
    threading.Thread(target=ds.run_safe, args=(name,) if params is None else (name, params), daemon=True).start()
    for _ in range(20):  # up to 0.2 s for the thread to take the lock, so the next page already shows the run
        if ds.busy():
            break
        time.sleep(0.01)
    return True


@router.get("/demo/run")
def demo_run():
    return run_view() or {"name": None, "running": False, "steps": []}


@router.post("/demo/custom", response_class=HTMLResponse)
async def demo_custom(request: Request):
    from scripts import demo_scenarios as ds
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        params = ds.custom_params(form)
    except ValueError as e:  # the form comes back with the viewer's values and the message
        return _story_page(request, form={**form, "say": form.get("say", "")[:2000]}, error=str(e))
    if low := ds.balance_problem(params["asset"], params["budget"]):  # not a failing on-chain authorize
        return _story_page(request, form={**form, "say": form.get("say", "")[:2000]}, error=low)
    if not _start("custom", params):
        return RedirectResponse("/story?busy=1", status_code=303)
    return RedirectResponse("/story", status_code=303)


@router.post("/demo/{scenario}")
def demo(scenario: str, next: str = "/bank"):
    from scripts import demo_scenarios
    if scenario not in demo_scenarios.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    target = next if next in ("/bank", "/story") else "/bank"  # no open redirect
    if demo_scenarios.balance_problem(demo_scenarios.SCENARIOS[scenario]["asset"], SCRIPTED_BUDGET_EUR_CENTS):
        return RedirectResponse(f"/story?low={scenario}", status_code=303)
    started = _start(scenario)
    return RedirectResponse(target + ("" if started else "?busy=1"), status_code=303)


@router.post("/timer/tick")
def timer_tick():
    """One timer pass now (the demo need not wait up to 5 s)."""
    deadlines.tick()
    return RedirectResponse("/bank", status_code=303)
