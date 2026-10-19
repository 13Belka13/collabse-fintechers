# TASKS — tick-list (details and done-criteria: SPEC.md; layers = §15)

Hacking 12:00 Mon → code freeze 13:30 Tue. AWS Toolbox 12:00–13:30 is mandatory for at least one
person (Bedrock logins) — start with work that needs no AWS; LLM steps run in MOCK mode until then.
Minimum for stage: layers 1–3 + 5. After each task: run its check, commit, tick here.
**Every function that moves money or refuses money gets its unit test in the same commit** (SPEC §19).

## Streams (separate folders, can run in parallel)

| Stream | Owns | Starts with |
|---|---|---|
| **T — tCZK** (from hour 1, independent) | `contracts/`, `bank/facilitator.py` | T1 |
| **P — payments & escrow** | `bank/escrow.py`, seller x402 | L1 |
| **A — agents & mandate** | `agents/`, `bank/mandate.py`, `bank/explain.py` | L2 |
| **S — screening & verifier** | `bank/screening/`, `bank/verifier.py`, `actors/` | L4 |
| **U — UI & demo** | `bank/templates/`, `scripts/demo_*.py` | L5 |
| **Q — validation** (from hour 3–4) | `tests/`, `scripts/{smoke,scenarios,report}.py`, `reports/`, `.github/workflows/` | Q1 |

## Stream T — bank token tCZK (SPEC §3)

- [ ] T1 Foundry project, OZ v5.7.0 (`draft-ERC3009.sol` exists in that tag), `TCZK is ERC3009, AccessControl`,
      roles, allowlist + `accountRef`, freeze, `forcedTransfer`, `decimals=2`, `bytes` overloads incl.
      `receiveWithAuthorization(..., bytes)`. Windows: if `foundryup` is slow to install, use py-solc-x
      + web3 like `prep/spikes/deploy_tczk.py` and keep going.
- [ ] T2 Tests (§3.6): both overloads, replay, expiry, receive-not-to, allowlist/freeze, cancel, burn
      of delisted, forcedTransfer, strict mode; fork test of authorize → capture → void via collector.
- [ ] T3 Deploy + allowlist per §3.3 (treasury, clients, collector `0x8612…ab88`, operator token
      store `0xd2b0C305F62427414e9624a2beD72a3792e15cf8`) + roles + demo mint. Fallback for the stage:
      the prep token `0x6a99C9e95DD220172d07D5f730f2e82B466D4B67` (already allowlisted, buyer holds 845.00).
- [ ] T4 Bank facilitator (§4.3): `/x402/verify`, `/x402/settle`, `/x402/supported` for EURC **and**
      tCZK; screening in `on_before_verify` **and** `on_before_settle`; refusal = HTTP 200 `isValid:false`;
      sync `def` endpoints, one process, lock on settle, own retry with idempotency key.

## Stream Q — validation on real testnet transactions (SPEC §19)

- [ ] Q1 Test scaffolding: `pytest` + `pytest-cov` config, `tests/unit/` fixtures (temp SQLite, fake
      clock, fake chain for unit level), `timed(stage, deal_id)` → `timings` table (§19.4), CI workflow
      (MOCK=1, no secrets, coverage report).
- [ ] Q2 `scripts/smoke.py` (< 60 s): `/health` of bank and seller, facilitator `/supported` lists EURC
      and tCZK, RPC reachable, ETH/EURC/tCZK above thresholds, unpaid request → 402, one Bedrock call
      (or MOCK), ARES reachable, one 0.01 EURC payment lands.
- [ ] Q3 `scripts/scenarios.py --runs 3 [--only N05] [--p0]`: P0 matrix §19.2 on Base Sepolia; each
      scenario asserts decision + on-chain balance/escrow deltas (read from chain) + journal; negatives
      assert "no money moved". N13/N14 = timer defaults with demo windows. Small amounts (`SCENARIO_PRICE_EUR`). N10/N11 send real reverting txs
      with manual gas.
- [ ] Q4 Fault injection via `FAULTS` (`settle_fail_once`, `registry_timeout`, `llm_bad_json`,
      `webhook_duplicate`, `webhook_bad_hmac`) with their scenarios; reconciliation of chain
      `Transfer` events vs DB vs statement; journal tamper test.
- [ ] Q5 Evals: `tests/evals/injections.jsonl` (20), `judge.jsonl` (40 labelled), explanation checks
      (sources subset, no non-public reason in client text, AI label); MOCK in CI, live Bedrock once.
- [ ] Q6 `scripts/report.py` → `reports/validation.md|json`: scenarios × runs with explorer links,
      attacks blocked X/X, wrong debits 0, p50/p95 per stage vs targets, coverage, evals, AWS cost.
      `/bank` panel: last decision ms + p95. P1 scenarios only if time is left.

## Layers

- [ ] **L1 x402 EURC** — seller 402 with EURC option; buyer pays via the bank facilitator (fallback
      x402.org); tx hash in `PAYMENT-RESPONSE` and explorer. Client `allowed_assets` for EURC/tCZK.
      Route key `GET /data/:offer_id` (never `{offer_id}`); price via `DynamicPrice`; unpaid request → 402.
- [x] **L2 Mandate + agents + negotiation + attack** (stream A, MOCK + `agents/offline.py`; wiring to P's
      `bank/api.py` and one live Bedrock run still to do) — mandate JSON + versions + hash (limits in euro
      cents: 10 € per deal, confirm > 6 €, 30 €/month; task budget 5 €; tCZK → EUR at `CZK_PER_EUR_DEMO`); atomic reservations
      (`BEGIN IMMEDIATE`); every refusal code reproduced by a test; buyer tool loop (manual
      `stop_reason == "tool_use"` loop, JSON+pydantic, MOCK mode); seller mode `injection` →
      `COUNTERPARTY_NOT_IN_SCOPE` / `AMOUNT_ABOVE_LIMIT`, journal source `untrusted:seller`; race test:
      10 parallel `pay()` with budget for 3 → exactly 3 reservations; minimal
      negotiation (1–2 turns: offer → counter) ends in an offer within the task budget (brief requirement).
      VAT risk acknowledged once per (seller IČO, asset) → `vat_risk_acknowledged` in a new mandate version.
- [ ] **L3 Escrow + verifier** — PaymentInfo persisted before `authorize`; seller checks escrow
      on-chain before fulfilling; rules per item + Haiku judge flags (JSON+pydantic; invalid → flag all
      to human); no ARES check on Google Maps places (they have no IČO); `website` required;
      18/20 → capture 4.50 + void 0.50. Reuse `prep/spikes/escrow_spike.py` techniques.
- [ ] **L4 Counterparty check** — IČO checksum, ARES, VAT payers SOAP (unreliable + published
      accounts), ISIR, EU sanctions XML (companies only, fuzzy ≥ 90), Mostly Right datasets (if key),
      Apify actor `company-check` (web footprint only: IČO on the site + Google Maps place; registries are
      checked by the bank) + 8 evals (incl. injection text on a site); live IČO check for legal entities
      only (forms 112/121); cache; two-level
      explanations; Delta blocked before payment.
- [ ] **L5 Dispute, UI, statement** — `dispute_terms` in the offer (§9.1); `bank/deadlines.py` timer
      thread every 5 s applying silence defaults (buyer silent → capture, seller silent → void part,
      decision timeout → rules verdict), journal `actor: timer`, never past authorizationExpiry − 1 h;
      5% holdback captured after its window; capture before the final void (no partial void);
      dispute card (items, rule results + violated criterion, deadlines and silence outcome, judge reasons with sources,
      seller statement, accept/reject per item) → `capture`/`void`; pages `/bank` (client/compliance
      toggle), `/mandate`, `/deals`, `/deals/{id}/dispute`, `/statement`; AI-generated label.
- [ ] **L6 Bank answer** — same deal in tCZK through escrow + bank facilitator; strict mode; VAT
      rule (§6.5): EURC → "VAT risk acknowledged" mark (acknowledged in scene 1), tCZK 125,00 Kč → passes
      (account published). `TCZK_NAME` must match the token in use.
- [ ] **L7 Negotiation polish, Tatum, journal** — up to 3 turns ("20 for 6 €" refused by the task budget); Tatum webhook via tunnel with HMAC
      on raw body + dedupe by `txId` (fallback: RPC polling); `scripts/verify_journal.py`.
- [ ] **L8 Polish** — README ("Built during the hackathon", "Existed before: prep/", AI-assistant
      disclosure, run instructions, Apify usage highlighted, list of what came from `prep/`, AWS credit
      spend from the per-call token log, numbers from `reports/validation.md`); three clean demo runs in a
      row within 4 minutes; `smoke.py` green right before the demo; freeze 13:30.
