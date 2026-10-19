# INTERFACES — contracts between streams (stage 0, 19.10)

SPEC.md stays the master for *what*; this file fixes *how modules talk*, so streams can build in
parallel. Change a signature here only together with its callers, in one commit.

## Shared foundation (done, owned by the integrator — extend, don't fork)

| Module | Use it for |
|---|---|
| `bank.config` | all settings (`config.MOCK`, `config.FAULTS`, `config.ASSETS[asset]` = address/decimals/name/version, `config.CZK_PER_EUR_DEMO`, …). Tests: `monkeypatch.setenv(...)` + `config.reload()` |
| `bank.db` | `db.conn()`, `db.one/rows/run(sql, params)`, `with db.immediate() as c:` (BEGIN IMMEDIATE), `db.dumps` (canonical JSON). Schema = SPEC §12 in `db.SCHEMA`; add columns there, not in your module |
| `bank.money` | `to_atomic(asset, "5.00")`, `to_eur_cents(asset, atomic)` (rounds **up**), `from_eur_cents`, `fmt` ("5,00 €", "125,00 Kč"), `per_item_split(total, count, accepted)` |
| `bank.chain` | `chain.w3()`, `chain.sender(pk).send(call_or_txdict, kind=, deal_id=, idem_key=, gas=, expect_revert=)` → `{tx_hash, status, block_number, gas_used, link, receipt}`; raises `chain.TxReverted` unless `expect_revert`. **Every money tx passes an `idem_key`** (`f"{deal_id}:{kind}:{n}"`). `chain.balance(asset, addr)`, `chain.transfers(token, from_block, frm=, to=)`, `chain.read_retry(fn, ok)`, `chain.tx_link(h)` |
| `bank.timing` | `with timing.timed("decision", deal_id):` for every stage in §19.4; `timing.log_llm` is called by `bank.llm` for you |
| `bank.journal` | `journal.append(actor, event, payload, deal_id=)`; actors: `bank`, `timer`, `human:<name>`, `agent:buyer`, `agent:seller`; text that came from a counterparty → `payload["source"] = "untrusted:seller"` |
| `bank.core` | `core.company(id)`, `company_by_ico`, `company_by_wallet`, `bound_account(wallet)`, `seller(seller_id)`, `sellers(category)`, `wallet_key(company_id)` (testnet key from `.env`, never logged) |
| `bank.codes` | closed enum `Code` (§6.3) + `codes.reason(code, public, text, source_url, **evidence)`, `codes.ok()`, `codes.refuse(Code.X, reasons)`, `codes.client_view(decision)` — **anything shown to the client or its agent goes through `client_view`** |
| `bank.llm` | `llm.create(role="main"/"fast", system, messages, stage=, deal_id=, tools=, effort=)`; `llm.json_call(role, system, prompt, Schema, default, stage=)` → `(value, ok)`; `llm.register_mock(stage, fn)` — every stage that calls a model registers a MOCK reply in the same module |
| `scripts/seed.py` | synthetic companies (`fixtures/companies.json`): `alfa` buyer; sellers `s-beta` (honest, VAT payer, published account), `s-gama` (fake_data), `s-delta` (IČO mismatch on site) |

## Decision shape (every pay/screen/refuse result)

```python
{"status": "ok" | "refused" | "requires_user_action", "code": "AMOUNT_ABOVE_LIMIT" | None,
 "reasons": [{"code": "vat_unreliable_payer", "public": True, "text": "...", "source_url": "...", "evidence": {...}}],
 "explanation": "...", "next_tool": "amend_mandate" | "confirm" | ... | None, ...extra}
```
Stored in `decisions` (`codes_json`, `evidence_json`, `explanation_client` = `client_view` text,
`explanation_compliance` = full text). Template text first; AI text (Haiku) may replace it later, labelled.

## Deal lifecycle (`deals.state`)

`negotiating → offered → quoted → (refused | awaiting_human) → authorized → delivered → verified →
(disputed) → settled → closed`. One writer per transition, inside `db.immediate()`; journal every change.

## Stream contracts

**A — mandate & agents** (`bank/mandate.py`, `bank/explain.py`, `agents/`)
```python
mandate.create(mandate_id, company_id, policy: dict, by: str) -> int            # version 1
mandate.amend(mandate_id, changes: dict, by: str) -> int                        # new version, new policy_hash
mandate.current(mandate_id) -> dict                                             # {version, policy, policy_hash}
mandate.acknowledge_vat(mandate_id, seller_ico, asset, by) -> int               # adds pair, new version
mandate.decide(mandate_id, deal: dict, quote: dict, screening: dict, now=None) -> Decision   # pure code, no I/O beyond SQLite
mandate.reserve(mandate_id, deal_id, eur_cents) -> Decision                     # BEGIN IMMEDIATE; BUDGET_EXCEEDED on overflow
mandate.commit(deal_id, eur_cents) / mandate.release(deal_id)
explain.render(decision, audience="client"|"compliance", deal_id=None) -> {"text", "cited_sources", "ai_generated"}
```
`mandate.decide` applies the VAT rule §6.5 using `screening["vat"]` and the acknowledged pairs.
Agent tools call the **bank HTTP API** (below), never bank modules and never the chain.

**S — screening & verifier** (`bank/screening/`, `bank/verifier.py`, `actors/company-check/`)
```python
screening.check(ico: str, wallet: str | None = None, website_url: str | None = None, live=False) -> dict
#   {"level": "ok"|"review"|"blocked", "reasons": [reason...], "sources": [url...],
#    "vat": {"payer": bool, "unreliable": "ANO"|"NE"|"NENALEZEN"|None, "published_accounts": ["pre-num/bank", ...]},
#    "legal_form": "112", "fetched_at": ts, "demo_fixture": bool}
screening.prefetch(ico, wallet=None, website_url=None) -> None                  # background thread, fills cache
screening.cached(ico) -> dict | None                                            # money path: cache only, never network
screening.normalize_account(s) -> "pre-num/bank"                                # IBAN or internal -> internal format
verifier.check_items(items: list[dict], criteria: dict) -> list[dict]           # per item {placeId, ok, failed:[criterion], ...}
verifier.judge(items, criteria, deal_id) -> list[dict]                          # LLM flags only; invalid JSON -> flag all
verifier.settlement(deal_id) -> {"capture_now": int, "held": int, "void": int, "items": [...]}  # atomic units
```
Registries: parallel, 2 s timeout, fail-closed → `level="review"` with a non-public `registry_timeout` reason.
Synthetic companies → recorded fixtures in `fixtures/registries/`. Live mode only for forms 112/121.

**P — payments & escrow** (`bank/escrow.py`, `bank/deals.py`, `seller/app.py`)
```python
escrow.payment_info(deal) -> dict                 # §5.1 fields; persisted to deals.payment_info_json BEFORE authorize
escrow.authorize(deal_id) -> tx                   # signs ReceiveWithAuthorization for the buyer, idem_key f"{deal_id}:authorize"
escrow.capture(deal_id, amount, n) -> tx          # idem_key f"{deal_id}:capture:{n}"
escrow.void(deal_id) -> tx                        # idem_key f"{deal_id}:void"
escrow.state(deal_id) -> {"collected": bool, "capturable": int, "refundable": int}   # from chain
deals.pay(deal_id) -> Decision                    # fetch 402 quote -> compare with offer (payTo, asset, amount) ->
                                                  # screening.cached -> mandate.decide -> reserve -> authorize -> notify seller
```
Seller HTTP (`:8001`): `POST /negotiate {deal_id, buyer_ico, task, message}` → `{reply_text, offer?}`;
`GET /order/{offer_id}` behind x402 (route key `GET /order/:offer_id`, DynamicPrice from the offer);
`POST /order/{offer_id}/escrow {payment_info, payment_hash, tx_hash}` → seller verifies on chain, then delivers;
delivery goes to the bank: `POST /api/deals/{deal_id}/delivery {items}`. L1 direct pay: `GET /data/{offer_id}` (key `GET /data/:offer_id`).

**T — tCZK & facilitator** (`contracts/`, `bank/facilitator.py`)
FastAPI `APIRouter(prefix="/x402")`: `POST /verify`, `POST /settle`, `GET /supported` (EURC + tCZK). Bank
screening hook `facilitator.screen(payload, requirements) -> code | None` in **both** `on_before_verify` and
`on_before_settle`; refusal → HTTP 200 `{"isValid": false, "invalidReason": code}`. tCZK admin helpers
`tczk.allow(addr, ref)`, `tczk.freeze(addr)`, `tczk.mint(to, amt, ref)` via `chain.sender(config.BANK_ADMIN_PK)`.

**U — UI** (`bank/templates/`, `bank/ui.py`): reads tables + `journal.entries`; writes only through the module
functions above (`mandate.amend`, dispute decisions). Pages per SPEC §13.

**Q — validation** (`tests/`, `scripts/smoke.py|scenarios.py|report.py`, `reports/`, `.github/workflows/`):
asserts against the chain (`chain.balance`, `escrow.state`, `chain.transfers`), never only our DB.

## Bank HTTP API for agents (`:8000/api`, JSON) — ids only, no addresses, no amounts to pay

| Method | Path | Body → result |
|---|---|---|
| GET | `/api/sellers?category=data` | → `[{seller_id, name, ico, profile_url}]` |
| GET | `/api/sellers/{seller_id}/check` | → `{level, public_reasons[]}` (client view) |
| POST | `/api/deals` | `{buyer_id, seller_id, mandate_id, task}` → `{deal_id}` (starts screening prefetch) |
| POST | `/api/deals/{deal_id}/negotiate` | `{message}` → `{reply_text, offer?}` (bank proxies the seller, stores offers, marks source untrusted) |
| POST | `/api/deals/{deal_id}/pay` | `{}` → client-view Decision |
| GET | `/api/statement?mandate_id=` | → mandate summary |
| POST | `/api/deals/{deal_id}/delivery` | seller → bank, `{items}` |

## Ownership (to avoid merge conflicts)

`bank/app.py` is shared: each stream adds **one** `app.include_router(...)` line. `bank/db.py` schema changes:
append-only columns/tables. Everything else: only your folders (TASKS "Streams").
