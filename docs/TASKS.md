# TASKS — tick-list (details and done-criteria: SPEC.md; layers = §15; deal model: MODEL_V2.md)

> **Internal working document**: the build tick-list of the team and its coding agent. Kept for history; the project is described in the [README](../README.md).

Hacking 12:00 Mon → code freeze 13:30 Tue. LLM steps run in MOCK mode in CI and offline; live Bedrock through the
team's AWS account (EU inference profiles). After each task: run its check, commit, tick here.
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

- [x] T1 Foundry project, OZ v5.7.0 (`draft-ERC3009.sol` exists in that tag), `TCZK is ERC3009, AccessControl`,
      roles, allowlist + `accountRef`, freeze, `forcedTransfer`, `decimals=2`, `bytes` overloads incl.
      `receiveWithAuthorization(..., bytes)`. Python path without Foundry: `python -m bank.tczk deploy` from the
      committed artifact.
- [x] T2 Tests (§3.6): both overloads, replay, expiry, receive-not-to, allowlist/freeze, cancel, burn
      of delisted, forcedTransfer, strict mode; fork test of authorize → capture → void via collector.
- [x] T3 Deploy + allowlist per §3.3 (treasury, clients, collector `0x8612…ab88`, operator token
      store `0xd2b0C305F62427414e9624a2beD72a3792e15cf8`) + roles + demo mint: strict-mode TCZK
      `0x18245366edA63Ca583c11B27d1134F3d233A115F`, source-verified (`contracts/README.md`).
- [x] T4 Bank facilitator (§4.3): `/x402/verify`, `/x402/settle`, `/x402/supported` for EURC **and**
      tCZK; screening in `on_before_verify` **and** `on_before_settle`; refusal = HTTP 200 `isValid:false`;
      sync `def` endpoints, one process, lock on settle, own retry with idempotency key.

## Stream Q — validation on real testnet transactions (SPEC §19)

- [x] Q1 Test scaffolding: `pytest` + `pytest-cov` config, `tests/unit/` fixtures (temp SQLite, fake
      clock, fake chain for unit level), `timed(stage, deal_id)` → `timings` table (§19.4), CI workflow
      (MOCK=1, no secrets, coverage report). (`.github/workflows/ci.yml`: pytest + branch coverage, forge without fork)
- [x] Q2 `scripts/smoke.py` (< 60 s): `/health` of bank and seller, facilitator `/supported` lists EURC
      and tCZK, RPC reachable, ETH/EURC/tCZK above thresholds, unpaid request → 402, one Bedrock call
      (or MOCK), ARES reachable, one 0.01 EURC payment lands. (`--target running|fork|sepolia`)
- [x] Q3 `scripts/scenarios.py --runs N [--only N05] [--p0]`: P0 matrix §19.2; each scenario asserts decision +
      on-chain balance/escrow deltas (read from chain) + journal; negatives assert "no money moved".
  - [x] Runner + stack (`scripts/stack.py`), S01–S05, N01–N14 and A01 (API auth) pass 3/3 on an anvil fork of Base Sepolia
  - [x] Live P0 run on Base Sepolia with explorer links (`reports/validation-sepolia.md`)
  - [x] N10/N11 live on the strict-mode TCZK: direct transfer reverts `TransferViaBankOnly`, replayed signature reverts
        `ERC3009UsedAuthorization` (`reports/scenarios-sepolia-n10n11.json`)
- [x] Q4 Fault injection via `FAULTS` (`settle_fail_once`, `registry_timeout`, `llm_bad_json`) and webhook
      scenarios (`webhook_duplicate`, `webhook_bad_hmac`); reconciliation of chain `Transfer` events vs DB vs statement;
      journal tamper test.
  - [x] settle_fail_once, registry_timeout, llm_bad_json (F01–F03, 3/3 on the fork + unit tests); reconciliation;
        `scripts/verify_journal.py` + tamper test
  - [x] webhook_duplicate / webhook_bad_hmac: `bank/webhooks.py` + `tests/unit/test_webhooks.py`; scenarios F04/F05
- [x] Q5 Evals: `tests/evals/injections.jsonl` (20), explanation checks (sources subset, no non-public reason in
      client text, AI label), predicate eval `tests/evals/predicates.jsonl`; MOCK in CI.
  - [x] 20 injections through the buyer tool loop (money-level block 20/20), explanations; MOCK in CI
  - [x] Live Bedrock run (`tests/evals/test_live_bedrock.py` → `reports/evals-live.json`, Opus 4.5 / Haiku 4.5 EU profiles)
- [x] Q6 `scripts/report.py` → `reports/validation.md|json`: scenarios × runs with explorer links,
      attacks blocked X/X, wrong debits 0, p50/p95 per stage vs targets, coverage, evals, AWS cost.
  - [x] Report builder; fork-run report labelled "anvil fork"
  - [x] Base Sepolia report with explorer links (`reports/validation-sepolia.md`)
  - [x] `/bank` panel: last decision ms + p95 (`bank/templates/_feed.html`)

## Layers

- [x] **L1 x402 EURC** — seller 402 with EURC option; buyer pays via the bank facilitator (fallback
      x402.org); tx hash in `PAYMENT-RESPONSE` and explorer. Client `allowed_assets` for EURC/tCZK.
      Route key `GET /data/:offer_id` (never `{offer_id}`); price via `DynamicPrice`; unpaid request → 402.
  - [x] Seller 402 with EURC + tCZK from the offer (`DynamicPrice`, `:offer_id` keys), buyer helper with
        `allowed_assets` caps, `scripts/demo_l1.py`; unpaid → 402 tested; paid run settled on an anvil fork
  - [x] Live run on Base Sepolia through the bank facilitator: 0.01 EURC, tx 0xa0f7…441f; S01 in the P0 run
- [x] **L2 Mandate + agents + negotiation + attack** (the buyer agent runs through the real bank API with
      agent tokens) — mandate JSON + versions + hash (limits in euro
      cents: 10 € per deal, confirm > 6 €, 30 €/month; task budget 5 €; tCZK → EUR at `CZK_PER_EUR_DEMO`); atomic reservations
      (`BEGIN IMMEDIATE`); every refusal code reproduced by a test; buyer tool loop (manual
      `stop_reason == "tool_use"` loop, JSON+pydantic, MOCK mode); seller mode `injection` →
      `COUNTERPARTY_NOT_IN_SCOPE` / `AMOUNT_ABOVE_LIMIT`, journal source `untrusted:seller`; race test:
      10 parallel `pay()` with budget for 3 → exactly 3 reservations; negotiation ends in an offer within the
      task budget. VAT risk acknowledged once per (seller IČO, asset) → `vat_risk_acknowledged` in a new mandate version.
- [x] **L3 Escrow + verifier** (v1 model; the acceptance part is replaced by V2 below) — PaymentInfo persisted
      before `authorize`; seller checks escrow on-chain before fulfilling; 18/20 → capture 4.50 + void 0.50.
  - [x] Escrow part: PaymentInfo stored before `authorize`, seller checks escrow on chain before
        delivering, authorize → capture 4.50 → void 0.50 proven on an anvil fork against the real escrow/collector
  - [x] Live Base Sepolia escrow runs S02–S05 with explorer links in `reports/validation-sepolia.md`
- [x] **L4 Counterparty check** — IČO checksum, ARES, VAT payers SOAP (unreliable + published
      accounts), ISIR, EU sanctions XML (companies only, fuzzy ≥ 90), Mostly Right datasets (keyed),
      Apify actor `company-check` (web footprint only: IČO on the site + Google Maps place; registries are
      checked by the bank) + 8 evals (incl. injection text on a site); live IČO check for legal entities
      only (forms 112/121); cache; two-level explanations; Delta blocked before payment.
  - [x] `bank/screening`: checksum, ARES, VAT payers SOAP, ISIR, EU sanctions (local index, non-public
        near-match), parallel 2 s fail-closed, 1 h cache, legal forms 112/121 only; parsers tested on answers
        recorded live 19.10; live ČEZ check passes (`LIVE=1`). Synthetic sellers from `fixtures/registries/`;
        Delta → level `blocked`, public `ico_mismatch_on_website`
  - [x] Actor `actors/company-check` + 8 evals (mocked HTTP, injection site); pushed to Apify as
        `savvy_sauger/company-check`, live runs in `actors/company-check/README.md`
  - [x] Google Maps dataset recorded from Apify (`fixtures/maps/brno_warehouses_recorded.json`, `fixtures/maps/record_maps.py`)
  - [x] Mostly Right datasets connector `bank/screening/datasets.py` (KYB flags + counterparty records, flag mapping
        unit-tested); enabled when `MOSTLYRIGHT_API_KEY` is set
  - [x] Delta refused before payment end to end (`tests/unit/test_integration.py`, N06 in the live P0 run)
- [x] **L5 Dispute, UI, statement** — `dispute_terms` in the offer; `bank/deadlines.py` timer
      thread every 5 s applying silence defaults, journal `actor: timer`, deadlines clamped before
      authorizationExpiry; capture before the final void (no partial void); pages `/bank` (client/compliance
      toggle), `/mandate`, `/deals`, `/deals/{id}`, `/statement`; AI-generated label. (Dispute rules A < B < C of
      v2.6: see V5/V6.)
  - [x] `bank/disputes.py`: every step journalled `human:<who>`/`timer`; captures written down before sending
        (idem key per n), final void last
  - [x] `bank/deadlines.py`: timer thread every 5 s from the UI router's lifespan; silence defaults, deadlines
        clamped to authorizationExpiry − 30 s, expiry alarm; fake-clock tests N13, N14, no double capture on
        repeated ticks / lost tx answer
  - [x] Pages: `/bank` (client view only via `codes.client_view`, compliance view, decision ms + p95, model
        cost), `/mandate` (versions + policy_hash, budget, VAT ack / confirm buttons), `/deals`, `/deals/{id}`,
        `/statement` + chain reconcile; demo buttons; `DEMO_SIM=1` simulated chain (labelled)
  - [x] Wire `deals.auto_settle` → `disputes.advance`; S05/N13/N14 live on Base Sepolia (timer driven by the runner)
- [x] **L6 Bank answer** — same deal in tCZK through escrow + bank facilitator; strict mode; VAT
      rule (§6.5): EURC → "VAT risk acknowledged" mark, tCZK 125,00 Kč → passes (account published).
      `TCZK_NAME` must match the token in use.
  - [x] Strict-mode TCZK deployed and source-verified: 0x18245366edA63Ca583c11B27d1134F3d233A115F
  - [x] VAT rule both ways + tCZK deal without the question (integration tests)
  - [x] S04 (tCZK escrow) live on the strict-mode token (`reports/scenarios-sepolia-tczk-strict.json`)
- [x] **L7 Negotiation, Tatum, journal** — up to 3 turns ("20 for 6 €" refused by the task budget); Tatum webhook
      with HMAC on raw body + dedupe by `txId` (RPC polling as the second path); `scripts/verify_journal.py`.
  - [x] AI seller agent in the seller service, up to 3 turns, code owns the price; `SELLER_OPENING=list`: 6 € refused
        by the 5 € task budget, the buyer agent haggles to 5 € and pays (tests in test_integration)
  - [x] `scripts/verify_journal.py` + tamper test; RPC on the Tatum Starter plan (eth_call on)
  - [x] `bank/webhooks.py`: `POST /webhooks/tatum` (HMAC-SHA512 on the raw body, constant-time compare, dedupe by
        `txId`, amounts re-read from the chain receipt) + RPC poller; `scripts/tatum_subscribe.py`
- [ ] **L8 Release** — README ("Built during the hackathon", pre-hackathon `prep/` listed, AI-assistant
      disclosure, run instructions, Apify usage, numbers from `reports/`); `smoke.py` green before the demo;
      freeze 13:30.

## Model v2.6 (docs/MODEL_V2.md §15; replaces SPEC §8/§9)

- [x] V1 `bank/receipts.py`: I-JSON/NFC, JCS (RFC 8785), Merkle tree (leaf 0x00‖uint64_be‖JCS, node 0x01‖l‖r,
      odd node promoted) with vectors in `fixtures/merkle_vectors.json`, EIP-712 manifest, seller key registry
- [x] V2 `bank/verifier.py`: predicate engine (AND only, auto-required fields, RE2, json_schema limits without
      external `$ref`, unique/max_count over versions and the task, registry_active bound to ARES/ISIR, source);
      no LLM on the money path; predicate eval `tests/evals/predicates.jsonl`
- [x] V3 delivery through the bank: signed manifest, bank root, quotas, versions (P never decreases), only
      accepted items reach the buyer (with proofs); "accept anyway" by a person
- [x] V4 escrow terms with the seller's `verify_escrow`: feeReceiver = clearing address, min = max fee = tariff,
      salt keccak(deal_id‖n), long expiries, "paid" over seller PaymentInfos
  - [x] Fee capture to the clearing address on an anvil fork of Base Sepolia (`tests/unit/test_escrow_fork.py`)
- [x] V5 rules A and B (§8.2 schedule, one final void, B slots at pay, no void on a failed capture)
- [x] V6 rule C: `bank/snapshots.py`, admissibility at filing, fees to the clearing address, negotiation and
      fee deadlines, `bank/deciders.py` (pool ≥ 3, commit-reveal, drand + committed salt, sample), split-off of D,
      execution after 10 business days; decider page `/deciders/{deal_id}`
- [x] V7 mandate §2: tier_sum, dispute_floor, require_b_for / require_c_above_eur with person-confirmed fallbacks,
      unprotected daily cap, claim-fee budget; demo scale ÷100 / ÷90 in `bank/config.py`
- [x] V8 buyer agent tools `fetch_delivery`, `file_claim`; scenario runner and demo scripts on the new schedule

## Roadmap (bank pilot)

- Rule C: paid re-review by the losing side (two more deciders, majority of three), 5 % random re-review,
  queue limits for C cases under review (MODEL_V2 §7C), salt commitment on chain.
- EvSM extract for related sellers (hook `mandate.related_sellers`), sessions with vouchers (MODEL_V2 §4),
  open seller statistics (MODEL_V2 §9).
- Live ČNB daily rate instead of the fixed demo rate; `refund` of captured amounts through the operator refund collector.
