# Mandate: immutable versions + hash, the pre-payment decision and atomic budget reservations (SPEC §6.1-6.5).
# All limits are integer euro cents (stored as strings in the policy, as in §6.1); tCZK is converted at
# config.CZK_PER_EUR_DEMO via bank.money, rounded up. decide() is pure code over SQLite — no model, no network.
# The budget truth is only the reservations table: SUM(amount) of reserved/committed rows in the period.
import calendar
import hashlib
import re
import time
from datetime import UTC, date, datetime, timedelta

from bank import codes, config, core, db, explain, journal, money, timing
from bank.codes import Code

DEMO_MANDATE_ID = "m-alfa-1"
VAT_LAW_URL = "https://www.zakonyprolidi.cz/cs/2004-235#p109"
VAT_REGISTER_URL = "https://adisspr.mfcr.cz/dpr/DphReg"
FIXED = ("mandate_id", "version", "company_ico")  # amend() may not touch these
DEFAULTS = {
    "category": "data",
    "per_deal_max": {"value": "1000", "currency": "EUR"},
    "budget": {"value": "3000", "currency": "EUR", "period": "month"},
    "confirm_above": {"value": "600", "currency": "EUR"},
    "assets_allowed": ["EURC", "tCZK"],
    "counterparties": "checked_only",  # or a list of allowed seller IČO
    "vat_risk_acknowledged": [],       # [{"ico", "asset", "at": "YYYY-MM-DD", "by"}]
    "purpose": None,
    "velocity": {"max_deals": 10, "window_s": 60},
    "active": True,
}


def policy_hash(policy: dict) -> str:
    return hashlib.sha256(db.dumps(policy).encode()).hexdigest()


def cents(limit) -> int:
    """{"value": "1000", "currency": "EUR"} or "1000" -> 1000 euro cents."""
    value = limit["value"] if isinstance(limit, dict) else limit
    if limit is None or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"amount must be euro cents as a digit string, got {limit!r}")
    return int(value)


def _validate(policy: dict):
    per_deal, confirm = cents(policy["per_deal_max"]), cents(policy["confirm_above"])
    if confirm >= per_deal:
        raise ValueError("confirm_above must be below per_deal_max, otherwise confirmation never triggers")
    if per_deal > cents(policy["budget"]):
        raise ValueError("per_deal_max must not exceed the budget")
    if policy["budget"].get("period", "month") != "month":
        raise ValueError("only a monthly budget is supported")
    if not set(policy["assets_allowed"]) <= set(config.ASSETS):
        raise ValueError(f"unknown asset in {policy['assets_allowed']}")
    date.fromisoformat(policy["valid_until"])


def _store(c, mandate_id: str, version: int, policy: dict, by: str) -> int:
    _validate(policy)
    h = policy_hash(policy)
    c.execute("INSERT INTO mandate_versions (mandate_id, version, policy_json, policy_hash, approved_at, approved_by)"
              " VALUES (?,?,?,?,?,?)", (mandate_id, version, db.dumps(policy), h, time.time(), by))
    c.execute("UPDATE mandates SET current_version=? WHERE mandate_id=?", (version, mandate_id))
    journal.append(by, "mandate_version", {"mandate_id": mandate_id, "version": version, "policy_hash": h})
    return version


def create(mandate_id: str, company_id: str, policy: dict, by: str) -> int:
    """Version 1. Missing fields come from DEFAULTS (§6.1 demo limits); valid 30 days unless given."""
    co = core.company(company_id)
    if co is None:
        raise ValueError(f"unknown company {company_id}")
    full = {**DEFAULTS, **v2_defaults(), "valid_until": (date.today() + timedelta(days=30)).isoformat(),
            "fx_demo": {"CZK_per_EUR": str(config.CZK_PER_EUR_DEMO)}, **policy,
            "mandate_id": mandate_id, "version": 1, "company_ico": co["ico"]}
    with db.immediate() as c:
        c.execute("INSERT INTO mandates (mandate_id, company_id, current_version) VALUES (?,?,0)",
                  (mandate_id, company_id))
        return _store(c, mandate_id, 1, full, by)


def ensure_demo(company_id: str = "alfa", by: str = "human:demo") -> dict:
    """The demo mandate m-alfa-1 with §6.1 limits (idempotent; needs scripts.seed first)."""
    if current(DEMO_MANDATE_ID) is None:
        create(DEMO_MANDATE_ID, company_id, {}, by)
    return current(DEMO_MANDATE_ID)


def current(mandate_id: str) -> dict | None:
    row = db.one("SELECT v.* FROM mandates m JOIN mandate_versions v ON v.mandate_id = m.mandate_id"
                 " AND v.version = m.current_version WHERE m.mandate_id=?", (mandate_id,))
    if row is None:
        return None
    return {"mandate_id": mandate_id, "version": row["version"], "policy": db.loads(row["policy_json"]),
            "policy_hash": row["policy_hash"], "approved_at": row["approved_at"], "approved_by": row["approved_by"]}


def versions(mandate_id: str) -> list[dict]:
    rows = db.rows("SELECT * FROM mandate_versions WHERE mandate_id=? ORDER BY version", (mandate_id,))
    for r in rows:
        r["policy"] = db.loads(r.pop("policy_json"))
    return rows


def amend(mandate_id: str, changes: dict, by: str) -> int:
    """New immutable version with a new policy_hash; older versions and their spend stay."""
    if set(changes) & set(FIXED):
        raise ValueError(f"cannot change {set(changes) & set(FIXED)}")
    with db.immediate() as c:
        m = current(mandate_id)
        if m is None:
            raise ValueError(f"unknown mandate {mandate_id}")
        version = m["version"] + 1
        return _store(c, mandate_id, version, {**m["policy"], **changes, "version": version}, by)


def acknowledge_vat(mandate_id: str, seller_ico: str, asset: str, by: str) -> int:
    """The human accepts the §109 VAT risk once per (seller IČO, asset) -> new version. Idempotent.
    Read and write in one transaction, so two acknowledgements at once cannot drop each other."""
    if asset not in config.ASSETS:
        raise ValueError(f"unknown asset {asset}")
    with db.immediate():
        m = current(mandate_id)
        if m is None:
            raise ValueError(f"unknown mandate {mandate_id}")
        acks = m["policy"]["vat_risk_acknowledged"]
        if any(a["ico"] == seller_ico and a["asset"] == asset for a in acks):
            return m["version"]
        pair = {"ico": seller_ico, "asset": asset, "at": date.today().isoformat(), "by": by}
        return amend(mandate_id, {"vat_risk_acknowledged": [*acks, pair]}, by)


# ---- spend from the reservations table ----

def _month_start(now: float) -> float:
    d = datetime.fromtimestamp(now, UTC)
    return datetime(d.year, d.month, 1, tzinfo=UTC).timestamp()


def _month_end(now: float) -> float:
    d = datetime.fromtimestamp(now, UTC)
    return _month_start(now) + calendar.monthrange(d.year, d.month)[1] * 86400


def _spent(c, mandate_id: str, now: float, except_deal: str | None = None) -> int:
    row = c.execute("SELECT COALESCE(SUM(amount),0) s FROM reservations WHERE mandate_id=? AND"
                    " state IN ('reserved','committed') AND created_at >= ? AND created_at < ? AND deal_id IS NOT ?",
                    (mandate_id, _month_start(now), _month_end(now), except_deal)).fetchone()
    return row["s"]


def _recent(c, mandate_id: str, since: float, except_deal: str | None) -> int:
    return c.execute("SELECT COUNT(*) n FROM reservations WHERE mandate_id=? AND created_at >= ? AND deal_id IS NOT ?",
                     (mandate_id, since, except_deal)).fetchone()["n"]


# ---- the decision ----

def _norm_account(acct: str | None) -> str | None:
    """'000000-1803729684/9999', '1803729684/9999' or CZ IBAN -> '1803729684/9999' (prefix kept if non-zero).
    Same normal form as screening.normalize_account (§6.5)."""
    if not acct:
        return None
    s = re.sub(r"\s", "", acct).upper()
    m = re.fullmatch(r"CZ\d{2}(\d{4})(\d{6})(\d{10})", s)
    if m:
        bank, pre, num = m.groups()
    else:
        m = re.fullmatch(r"(?:(\d{1,6})-)?(\d{1,10})/(\d{4})", s)
        if not m:
            return s
        pre, num, bank = m.groups()
    pre, num = int(pre or 0), int(num)
    return f"{pre}-{num}/{bank}" if pre else f"{num}/{bank}"


def _field(deal: dict, name: str):
    """deal dicts come either parsed ({"task": {...}}) or as DB rows ({"task_json": "..."})."""
    value = deal.get(name)
    return value if value is not None else db.loads(deal.get(f"{name}_json"))


def _asset_of(quote_asset: str | None) -> str | None:
    if quote_asset in config.ASSETS:
        return quote_asset
    for name, a in config.ASSETS.items():
        if quote_asset and a["address"] and a["address"].lower() == quote_asset.lower():
            return name
    return None


def _mandate_url(mandate_id: str, version: int) -> str:
    return f"{config.BANK_URL}/mandate?mandate_id={mandate_id}&version={version}"


def decide(mandate_id: str, deal: dict, quote: dict, screening: dict | None, now: float | None = None) -> dict:
    """Pre-payment decision (§6.3 order). deal: {deal_id, seller_id, task, offer, confirmed_by?};
    quote: the 402 option {asset (name or address), amount (atomic), pay_to|payTo};
    screening: screening.cached(ico) or None. Returns the Decision shape from bank.codes."""
    deal_id = deal.get("deal_id")
    with timing.timed("decision", deal_id):
        d = _decide(mandate_id, deal, quote, screening, time.time() if now is None else now)
        d.setdefault("marks", [])
        d["explanation"] = explain.template(d)
        return d


def _decide(mandate_id, deal, quote, screening, now) -> dict:
    deal_id = deal.get("deal_id")
    m = current(mandate_id)
    if m is None:
        return codes.refuse(Code.MANDATE_NOT_ACTIVE, [codes.reason(
            "mandate_missing", True, f"Mandate {mandate_id} does not exist.")], deal_id=deal_id)
    p, version = m["policy"], m["version"]
    src = _mandate_url(mandate_id, version)
    extra = {"deal_id": deal_id, "mandate_id": mandate_id, "mandate_version": version,
             "policy_hash": m["policy_hash"]}

    def refuse(code, reason_code, text, source_url=src, public=True, **evidence):
        return codes.refuse(code, [codes.reason(reason_code, public, text, source_url, **evidence)], **extra)

    # 1. active / expired
    if not p.get("active", True):
        return refuse(Code.MANDATE_NOT_ACTIVE, "mandate_paused", f"Mandate {mandate_id} v{version} is paused.")
    today = datetime.fromtimestamp(now, UTC).date()
    if today > date.fromisoformat(p["valid_until"]):
        return refuse(Code.MANDATE_EXPIRED, "mandate_expired", f"Mandate valid until {p['valid_until']}.",
                      valid_until=p["valid_until"])

    # 2. category and asset
    seller = core.seller(deal.get("seller_id") or "")
    if seller and seller["category"] != p["category"]:
        return refuse(Code.CATEGORY_NOT_IN_SCOPE, "category_not_allowed",
                      f"Category {seller['category']!r} is not in the mandate (allowed: {p['category']!r}).")
    asset = _asset_of(quote.get("asset"))
    if asset not in p["assets_allowed"]:
        return refuse(Code.CATEGORY_NOT_IN_SCOPE, "asset_not_allowed",
                      f"Asset {quote.get('asset')!r} is not allowed by the mandate ({', '.join(p['assets_allowed'])}).")

    # 3. counterparty in scope: the payee must be the seller's registered wallet
    pay_to = quote.get("pay_to") or quote.get("payTo") or ""
    if seller is None:
        return refuse(Code.COUNTERPARTY_NOT_IN_SCOPE, "seller_not_registered",
                      f"Seller {deal.get('seller_id')!r} is not in the bank's seller registry.")
    if not seller["wallet_address"] or pay_to.lower() != seller["wallet_address"].lower():
        return refuse(Code.COUNTERPARTY_NOT_IN_SCOPE, "payee_not_registered_wallet",
                      f"Payee {pay_to or '(none)'} is not {seller['name']}'s registered wallet.",
                      pay_to=pay_to, registered=seller["wallet_address"])
    allowed = p.get("counterparties", "checked_only")
    if isinstance(allowed, list) and seller["ico"] not in allowed:
        return refuse(Code.COUNTERPARTY_NOT_IN_SCOPE, "counterparty_not_listed",
                      f"{seller['name']} (IČO {seller['ico']}) is not on the mandate's counterparty list.")

    # 4. the 402 quote must be the agreed offer
    amount = int(quote.get("amount") or 0)
    offer = _field(deal, "offer")
    if not offer:
        return refuse(Code.QUOTE_MISMATCH, "no_agreed_offer", "There is no agreed offer for this deal.")
    if _asset_of(offer.get("asset")) != asset or int(offer.get("amount") or -1) != amount:
        return refuse(Code.QUOTE_MISMATCH, "quote_differs_from_offer",
                      f"Payment request {money.fmt(asset, amount)} differs from the agreed offer "
                      f"{offer.get('price')} {offer.get('asset')}.", quote_amount=amount, offer=offer)
    eur = money.to_eur_cents(asset, amount)
    extra.update(asset=asset, amount=amount, eur_cents=eur)

    # 5. counterparty trusted (cache only; fail-closed)
    if screening is None:
        return codes.refuse(Code.UNDER_REVIEW, [codes.reason(
            "screening_not_ready", False, "No cached counterparty check yet.")], **extra)
    level = screening.get("level")
    if level == "blocked":
        return codes.refuse(Code.COUNTERPARTY_NOT_TRUSTED, screening.get("reasons") or [codes.reason(
            "screening_blocked", False, "The counterparty check blocked this seller.")], **extra)
    if level != "ok":
        return codes.refuse(Code.UNDER_REVIEW, screening.get("reasons") or [codes.reason(
            "screening_review", False, f"Counterparty check level {level!r}.")], **extra)

    # 6. amount: task budget (separate, lower cap) and per-deal limit
    task = _field(deal, "task") or {}
    if task.get("budget") is not None and eur > cents(task["budget"]):
        return refuse(Code.AMOUNT_ABOVE_LIMIT, "task_budget",
                      f"{money.fmt(asset, amount)} is above the task budget {cents(task['budget']) / 100:.2f} €.",
                      eur_cents=eur, limit=cents(task["budget"]))
    if p.get("task_budget_max") is not None and eur > cents(p["task_budget_max"]):  # the human's cap on any task
        return refuse(Code.AMOUNT_ABOVE_LIMIT, "task_budget_max",
                      f"{money.fmt(asset, amount)} is above the mandate's task budget cap "
                      f"{cents(p['task_budget_max']) / 100:.2f} €.", eur_cents=eur, limit=cents(p["task_budget_max"]))
    if eur > cents(p["per_deal_max"]):
        return refuse(Code.AMOUNT_ABOVE_LIMIT, "per_deal_max",
                      f"{money.fmt(asset, amount)} is above the per-deal limit {cents(p['per_deal_max']) / 100:.2f} €.",
                      eur_cents=eur, limit=cents(p["per_deal_max"]))

    # 7. monthly budget (all versions of this mandate count)
    c = db.conn()
    spent, budget = _spent(c, mandate_id, now, deal_id), cents(p["budget"])
    if spent + eur > budget:
        return refuse(Code.BUDGET_EXCEEDED, "monthly_budget",
                      f"{eur / 100:.2f} € would exceed the monthly budget: {spent / 100:.2f} of {budget / 100:.2f} € used.",
                      spent=spent, budget=budget, eur_cents=eur)

    # 8. velocity
    vel = p.get("velocity") or {}
    if vel and _recent(c, mandate_id, now - vel["window_s"], deal_id) >= vel["max_deals"]:
        return refuse(Code.VELOCITY_EXCEEDED, "velocity",
                      f"More than {vel['max_deals']} payments within {vel['window_s']} s.", **vel)

    # 9. VAT rule §6.5
    marks = []
    vat = screening.get("vat")
    if vat is None:
        return codes.refuse(Code.UNDER_REVIEW, [codes.reason(
            "vat_status_unknown", False, "VAT register status is not in the cached check.")], **extra)
    vat_src = vat.get("source_url") or VAT_REGISTER_URL
    if vat.get("payer") or vat.get("unreliable") in ("ANO", "NE"):
        if vat.get("unreliable") == "ANO":
            return codes.refuse(Code.COUNTERPARTY_NOT_TRUSTED, [codes.reason(
                "vat_unreliable_payer", True, f"{seller['name']} is an unreliable VAT payer.", vat_src)], **extra)
        risk = None
        if asset == "EURC":
            risk = codes.reason("paid_in_virtual_asset", True,
                                "Paying a Czech VAT payer in EURC (not to a published bank account) makes the buyer "
                                "liable for the seller's VAT (§109 Act 235/2004).", VAT_LAW_URL)
        elif _norm_account(core.bound_account(pay_to)) not in {_norm_account(a) for a in vat.get("published_accounts", [])}:
            risk = codes.reason("account_not_published", True,
                                "The account bound to the tCZK wallet is not published in the VAT register.", vat_src)
        if risk:
            ack = next((a for a in p["vat_risk_acknowledged"] if a["ico"] == seller["ico"] and a["asset"] == asset), None)
            if ack is None:
                return codes.refuse(Code.VAT_LIABILITY_RISK, [risk], **extra)
            marks.append(f"VAT risk acknowledged {ack['at']}")

    # 10. human confirmation above the threshold
    if eur > cents(p["confirm_above"]) and not deal.get("confirmed_by"):
        return codes.refuse(Code.CONFIRMATION_REQUIRED, [codes.reason(
            "confirm_above", True, f"{eur / 100:.2f} € is above the confirmation threshold "
                                   f"{cents(p['confirm_above']) / 100:.2f} €.", src)], marks=marks, **extra)
    if deal.get("confirmed_by"):
        marks.append(f"confirmed by {deal['confirmed_by']}")
    return codes.ok(marks=marks, **extra)


# ---- reservations (§6.2) ----

def reserve(mandate_id: str, deal_id: str, eur_cents: int) -> dict:
    """Atomic check-then-insert under BEGIN IMMEDIATE. Idempotent per deal. BUDGET_EXCEEDED on overflow."""
    if eur_cents <= 0:
        raise ValueError("reservation must be positive")
    now = time.time()
    with db.immediate() as c:
        m = current(mandate_id)
        if m is None:
            return codes.refuse(Code.MANDATE_NOT_ACTIVE, [codes.reason(
                "mandate_missing", True, f"Mandate {mandate_id} does not exist.")], deal_id=deal_id)
        row = c.execute("SELECT * FROM reservations WHERE mandate_id=? AND deal_id=?", (mandate_id, deal_id)).fetchone()
        if row and row["state"] in ("reserved", "committed"):
            if row["amount"] != eur_cents and row["state"] == "reserved":
                raise ValueError(f"{deal_id} is reserved for {row['amount']}, not {eur_cents}: release it first")
            return codes.ok(deal_id=deal_id, reservation_id=row["id"], eur_cents=row["amount"], idempotent=True)
        p = m["policy"]
        src = _mandate_url(mandate_id, m["version"])
        # decide() ran outside this transaction: re-check what can change in between (version, time, parallel pays)
        if not p.get("active", True) or datetime.fromtimestamp(now, UTC).date() > date.fromisoformat(p["valid_until"]):
            return codes.refuse(Code.MANDATE_NOT_ACTIVE, [codes.reason(
                "mandate_not_active", True, f"Mandate {mandate_id} v{m['version']} is not active.", src)], deal_id=deal_id)
        vel = p.get("velocity") or {}
        if vel and _recent(c, mandate_id, now - vel["window_s"], deal_id) >= vel["max_deals"]:
            return codes.refuse(Code.VELOCITY_EXCEEDED, [codes.reason(
                "velocity", True, f"More than {vel['max_deals']} payments within {vel['window_s']} s.", src)],
                deal_id=deal_id)
        budget, spent = cents(p["budget"]), _spent(c, mandate_id, now)
        if spent + eur_cents > budget:
            d = codes.refuse(Code.BUDGET_EXCEEDED, [codes.reason(
                "monthly_budget", True, f"{eur_cents / 100:.2f} € would exceed the monthly budget: "
                                        f"{spent / 100:.2f} of {budget / 100:.2f} € used.",
                _mandate_url(mandate_id, m["version"]), spent=spent, budget=budget)], deal_id=deal_id)
            d["explanation"] = explain.template(d)
            journal.append("bank", "budget_refused", {"mandate_id": mandate_id, "eur_cents": eur_cents,
                                                      "spent": spent, "budget": budget}, deal_id=deal_id)
            return d
        if row:  # a released reservation for the same deal is reused (UNIQUE(mandate_id, deal_id))
            c.execute("UPDATE reservations SET state='reserved', amount=?, committed=0, mandate_version=?,"
                      " created_at=?, updated_at=? WHERE id=?", (eur_cents, m["version"], now, now, row["id"]))
            rid = row["id"]
        else:
            rid = c.execute("INSERT INTO reservations (mandate_id, mandate_version, deal_id, amount, state, created_at)"
                            " VALUES (?,?,?,?,'reserved',?)", (mandate_id, m["version"], deal_id, eur_cents, now)).lastrowid
        journal.append("bank", "budget_reserved", {"mandate_id": mandate_id, "version": m["version"],
                                                   "eur_cents": eur_cents, "spent_before": spent}, deal_id=deal_id)
    return codes.ok(deal_id=deal_id, reservation_id=rid, eur_cents=eur_cents)


def commit(deal_id: str, eur_cents: int) -> dict:
    """After a capture: add the captured euro cents. The reservation keeps holding its full amount until release()."""
    with db.immediate() as c:
        row = c.execute("SELECT * FROM reservations WHERE deal_id=?", (deal_id,)).fetchone()
        if row is None:
            raise ValueError(f"no reservation for {deal_id}")
        committed = row["committed"] + eur_cents
        if eur_cents < 0 or committed > row["amount"] or row["state"] == "released":
            raise ValueError(f"commit {eur_cents} on {deal_id} exceeds its reservation ({row['committed']}/{row['amount']})")
        c.execute("UPDATE reservations SET committed=?, state='committed', updated_at=? WHERE id=?",
                  (committed, time.time(), row["id"]))
        journal.append("bank", "budget_committed", {"eur_cents": eur_cents, "committed": committed}, deal_id=deal_id)
    return {"deal_id": deal_id, "committed": committed, "amount": row["amount"], "state": "committed"}


def release(deal_id: str) -> dict:
    """After the final void: the uncaptured rest goes back to the budget (amount := committed). Idempotent."""
    with db.immediate() as c:
        row = c.execute("SELECT * FROM reservations WHERE deal_id=?", (deal_id,)).fetchone()
        if row is None:
            raise ValueError(f"no reservation for {deal_id}")
        state = "committed" if row["committed"] else "released"
        amount = row["committed"] if row["committed"] else row["amount"]
        if (row["state"], row["amount"]) != (state, amount) or row["state"] == "reserved":
            c.execute("UPDATE reservations SET state=?, amount=?, updated_at=? WHERE id=?",
                      (state, amount, time.time(), row["id"]))
            journal.append("bank", "budget_released", {"released": row["amount"] - row["committed"],
                                                       "committed": row["committed"]}, deal_id=deal_id)
    return {"deal_id": deal_id, "state": state, "committed": row["committed"]}


def summary(mandate_id: str, now: float | None = None) -> dict | None:
    """Statement for get_statement / the /statement page."""
    m = current(mandate_id)
    if m is None:
        return None
    now = time.time() if now is None else now
    p = m["policy"]
    rows = db.rows("SELECT deal_id, mandate_version, amount, committed, state, created_at FROM reservations"
                   " WHERE mandate_id=? ORDER BY id", (mandate_id,))
    month = [r for r in rows if _month_start(now) <= r["created_at"] < _month_end(now) and r["state"] != "released"]
    used = sum(r["amount"] for r in month)
    committed = sum(r["committed"] for r in month)
    return {"mandate_id": mandate_id, "version": m["version"], "policy_hash": m["policy_hash"],
            "valid_until": p["valid_until"], "per_deal_max_eur_cents": cents(p["per_deal_max"]),
            "confirm_above_eur_cents": cents(p["confirm_above"]), "budget_eur_cents": cents(p["budget"]),
            "committed_eur_cents": committed, "reserved_eur_cents": used - committed,
            "remaining_eur_cents": cents(p["budget"]) - used, "assets_allowed": p["assets_allowed"],
            "vat_risk_acknowledged": p["vat_risk_acknowledged"], "reservations": rows}


# ---- MODEL_V2 §2: protection the mandate requires, by tier_sum ----

RULES = ("A", "B", "C")
TIERS = ("T0", "T1", "T2")


def v2_defaults() -> dict:
    """§2 defaults at the demo scale (§14: euro ÷ MONEY_DIVISOR, windows ÷ TIME_DIVISOR). Euro cents as strings."""
    m, t = config.MONEY_DIVISOR, config.TIME_DIVISOR
    c = lambda eur: str(eur * 100 // m)  # noqa: E731
    return {
        "unprotected_budget_eur_per_day": c(50), "unprotected_per_seller_eur_per_day": c(20),
        "session_budget_eur_per_day_per_seller": c(20),
        "dispute_floor": [
            {"tier_sum_max": c(20), "min_tier": "T0"},
            {"tier_sum_max": c(500), "min_tier": "T1", "min_window_s": 1800 / t, "allowed_quality": ["A", "B"]},
            {"tier_sum_max": None, "min_tier": "T2", "min_window_s": 72 * 3600 / t,
             "allowed_quality": ["A", "B", "C"]}],
        "require_b_for": ["data_purchase"], "require_c_above_eur": c(2000),
        "claim_fee_budget_eur_per_month": c(1000),
    }


def _p(policy: dict, key: str):
    return policy[key] if key in policy else v2_defaults()[key]


def related_sellers(seller_id: str) -> list[str]:
    """Sellers linked to this one by a common beneficiary (EvSM, bank/evsm.py), this one included."""
    from bank import core, evsm
    me = core.seller(seller_id)
    if me is None:
        return [seller_id]
    icos = evsm.related_icos(me["ico"])
    return sorted({seller_id} | {s["seller_id"] for s in core.sellers() if s["ico"] in icos})


def tier_sum(deal: dict, eur_cents: int, now: float) -> dict:
    """§2: the largest of (this deal; deals with this seller in 24 h incl. this one; deals of this task with this
    seller and its related sellers incl. this one). Sessions would count here too (not built: none exist).
    Deals already locked or past their decision count; refused ones do not. 24 h is demo-scaled (config.span)."""
    sellers = related_sellers(deal["seller_id"])
    marks = ",".join("?" * len(sellers))
    live = "('authorized','delivered','dispute_c','settled','awaiting_human')"
    day = db.rows(f"SELECT asset, amount FROM deals WHERE buyer_id=? AND seller_id=? AND state IN {live}"
                  " AND deal_id<>? AND created_at>=?", (deal["buyer_id"], deal["seller_id"], deal["deal_id"],
                                                        now - config.span(86400)))
    task_hash = (_field(deal, "criteria") or {}).get("hash")
    task = db.rows(f"SELECT asset, amount FROM deals WHERE buyer_id=? AND seller_id IN ({marks}) AND state IN {live}"
                   " AND deal_id<>? AND json_extract(criteria_json,'$.hash')=?",
                   (deal["buyer_id"], *sellers, deal["deal_id"], task_hash)) if task_hash else []
    eur = lambda rows: sum(money.to_eur_cents(r["asset"], int(r["amount"] or 0)) for r in rows)  # noqa: E731
    parts = {"deal": eur_cents, "seller_24h": eur_cents + eur(day), "task_with_seller": eur_cents + eur(task)}
    return {"tier_sum": max(parts.values()), **parts}


def requirements(mandate_id: str, deal: dict, now: float | None = None) -> dict:
    """What the seller needs to know to offer terms the mandate accepts (sent with each negotiation turn): the
    sums this deal adds to, and where B and C are required. The seller knows its own deals with this buyer."""
    now = time.time() if now is None else now
    m = current(mandate_id)
    p = m["policy"] if m else {}
    ts = tier_sum(deal, 0, now)
    return {"prior_eur_cents": ts["tier_sum"], "require_b": (_field(deal, "criteria") or {}).get("category")
            in _p(p, "require_b_for"), "require_c_above_eur_cents": cents(_p(p, "require_c_above_eur")),
            "min_window_s": {f["min_tier"]: f.get("min_window_s", 0) for f in _p(p, "dispute_floor")}}


def floor_for(policy: dict, ts: int) -> dict:
    for f in _p(policy, "dispute_floor"):
        if f.get("tier_sum_max") is None or ts <= cents(f["tier_sum_max"]):
            return f
    return _p(policy, "dispute_floor")[-1]


def _unprotected_today(mandate_id: str, seller_id: str | None, now: float, except_deal: str) -> int:
    sql = ("SELECT r.amount FROM reservations r JOIN deals d ON d.deal_id=r.deal_id WHERE r.mandate_id=?"
           " AND r.state IN ('reserved','committed') AND r.created_at>=? AND r.deal_id<>?"
           " AND json_extract(d.terms_json,'$.tier')='T0'")
    args = [mandate_id, now - config.span(86400), except_deal]
    if seller_id:
        sql, args = sql + " AND d.seller_id=?", args + [seller_id]
    return sum(r["amount"] for r in db.rows(sql, tuple(args)))


def protection(mandate_id: str, deal: dict, terms: dict, eur_cents: int, now: float | None = None) -> dict:
    """§2–§4 at pay, before the reservation: the tier and rule the mandate requires for this deal's tier_sum.
    Changes terms in place (tier lifted to the floor, tier_sum, fee reservation) and returns a Decision:
    TERMS_MISMATCH (the offer is below the floor), CONFIRMATION_REQUIRED (C or B impossible -> a person may take
    the lower protection), BUDGET_EXCEEDED (unprotected daily caps, the claim-fee budget), or ok with marks."""
    from bank import claims_c
    now = time.time() if now is None else now
    m = current(mandate_id)
    if m is None:
        return codes.refuse(Code.MANDATE_NOT_ACTIVE, [codes.reason("mandate_missing", True, "No mandate.")])
    p, src = m["policy"], _mandate_url(mandate_id, m["version"])
    ts = tier_sum(deal, eur_cents, now)
    floor = floor_for(p, ts["tier_sum"])
    rule, marks = terms["rule"], []
    need_tier = floor["min_tier"]
    if terms["category"] in _p(p, "require_b_for") and need_tier == "T0":
        need_tier = "T1"  # a final data delivery never goes unprotected (§2)
    terms["tier"] = max(terms["tier"], need_tier, key=TIERS.index)
    terms["tier_sum"] = ts["tier_sum"]
    terms["fee_bps"] = config.TARIFF_BPS.get((terms["tier"], rule), terms["fee_bps"])

    def refuse(code, reason_code, text, **ev):
        d = codes.refuse(code, [codes.reason(reason_code, True, text, src, **ev)])
        d["explanation"] = explain.template(d)
        return d

    need = 0
    if terms["category"] in _p(p, "require_b_for"):
        need = 1
    if ts["tier_sum"] > cents(_p(p, "require_c_above_eur")):
        need = 2
    have = RULES.index(rule)
    if have < need:
        has_source = any(x["op"] == "source" for x in (_field(deal, "criteria") or {}).get("predicates", []))
        if need == 2 and not has_source and have >= 1:
            if not deal.get("confirmed_by"):
                return refuse(Code.CONFIRMATION_REQUIRED, "c_unavailable",
                              f"The mandate requires rule C above {cents(_p(p, 'require_c_above_eur')) / 100:.2f} € "
                              "(sum with this seller and this task), but the task has no published source template: "
                              "the fields are protected by B only. A person can confirm the deal under B.",
                              tier_sum=ts)
            marks.append("B instead of C, confirmed by a person (no source template for C)")
        else:
            return refuse(Code.TERMS_MISMATCH, "rule_below_mandate",
                          f"The mandate requires rule {RULES[need]} or higher for this deal "
                          f"(tier sum {ts['tier_sum'] / 100:.2f} €); the offer has rule {rule}.", tier_sum=ts)
    if rule not in floor.get("allowed_quality", RULES) and rule != "C":
        return refuse(Code.TERMS_MISMATCH, "rule_not_allowed_in_tier", f"Rule {rule} is not allowed at this tier.")
    if rule != "A" and float(terms["window_s"]) < float(floor.get("min_window_s") or 0):
        return refuse(Code.TERMS_MISMATCH, "window_below_mandate",
                      f"The mandate requires a claim window of at least {floor['min_window_s']:g} s here.")
    if terms["tier"] == "T0":  # unprotected: one shared daily cap across sellers, and a cap per seller
        total, mine = (_unprotected_today(mandate_id, None, now, deal["deal_id"]),
                       _unprotected_today(mandate_id, deal["seller_id"], now, deal["deal_id"]))
        cap, per_seller = cents(_p(p, "unprotected_budget_eur_per_day")), cents(_p(p, "unprotected_per_seller_eur_per_day"))
        if total + eur_cents > cap or mine + eur_cents > per_seller:
            return refuse(Code.BUDGET_EXCEEDED, "unprotected_daily_cap",
                          f"Unprotected payments today would exceed {cap / 100:.2f} € in total or "
                          f"{per_seller / 100:.2f} € with this seller.", total=total, seller=mine)
    terms["fee_reserved_eur_cents"] = 0
    if rule == "C":
        f_max = claims_c.fee_max_eur_cents(int(terms["count"]), eur_cents, claims_c.lost_c(deal["buyer_id"], now))
        used = fee_budget_used(mandate_id, now, deal["deal_id"])
        budget = cents(_p(p, "claim_fee_budget_eur_per_month"))
        if used + f_max > budget:
            if not deal.get("confirmed_by"):
                return refuse(Code.CONFIRMATION_REQUIRED, "claim_fee_budget",
                              f"The claim-fee budget cannot reserve the largest C fee ({f_max / 100:.2f} €): "
                              f"{used / 100:.2f} of {budget / 100:.2f} € reserved this month. A person can confirm.")
            marks.append("C without a reserved claim fee, confirmed by a person")
        else:
            terms["fee_reserved_eur_cents"] = f_max
    return codes.ok(marks=marks)


# ---- the claim-fee budget (§2): reserved at pay, shrunk to F at filing, released when no C claim came ----

def fee_budget_used(mandate_id: str, now: float, except_deal: str | None = None) -> int:
    row = db.one("SELECT COALESCE(SUM(amount),0) s FROM fee_reservations WHERE mandate_id=? AND state IN"
                 " ('reserved','spent') AND created_at>=? AND created_at<? AND deal_id IS NOT ?",
                 (mandate_id, _month_start(now), _month_end(now), except_deal))
    return row["s"]


def reserve_fee(mandate_id: str, deal_id: str, eur_cents: int):
    if eur_cents <= 0:
        return
    with db.immediate():
        db.run("INSERT INTO fee_reservations (mandate_id, deal_id, amount, state, created_at) VALUES (?,?,?,?,?)"
               " ON CONFLICT(deal_id) DO UPDATE SET amount=excluded.amount, state='reserved'",
               (mandate_id, deal_id, eur_cents, "reserved", time.time()))
        journal.append("bank", "claim_fee_reserved", {"eur_cents": eur_cents}, deal_id=deal_id)


def set_fee_reservation(deal_id: str, eur_cents: int):
    """At filing: the reservation becomes the actual fee F (never more than reserved)."""
    with db.immediate():
        db.run("UPDATE fee_reservations SET amount=MIN(amount, ?), state='spent' WHERE deal_id=?", (eur_cents, deal_id))


def release_fee(deal_id: str):
    """The window closed without a C claim: the reservation goes back to the budget."""
    with db.immediate():
        row = db.one("SELECT state FROM fee_reservations WHERE deal_id=?", (deal_id,))
        if row and row["state"] == "reserved":
            db.run("UPDATE fee_reservations SET state='released' WHERE deal_id=?", (deal_id,))
            journal.append("bank", "claim_fee_released", {}, deal_id=deal_id)
