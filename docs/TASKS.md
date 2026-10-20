# TASKS — tick-list (details and done-criteria: SPEC.md; layers = §15)

> **Internal working document**: the build tick-list of the team and its coding agent. Kept for history; the project is described in the [README](../README.md).

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

- [x] T1 Foundry project, OZ v5.7.0 (`draft-ERC3009.sol` exists in that tag), `TCZK is ERC3009, AccessControl`,
      roles, allowlist + `accountRef`, freeze, `forcedTransfer`, `decimals=2`, `bytes` overloads incl.
      `receiveWithAuthorization(..., bytes)`. Windows: if `foundryup` is slow to install, use py-solc-x
      + web3 like `prep/spikes/deploy_tczk.py` and keep going.
- [x] T2 Tests (§3.6): both overloads, replay, expiry, receive-not-to, allowlist/freeze, cancel, burn
      of delisted, forcedTransfer, strict mode; fork test of authorize → capture → void via collector.
- [x] T3 Deploy + allowlist per §3.3 (treasury, clients, collector `0x8612…ab88`, operator token
      store `0xd2b0C305F62427414e9624a2beD72a3792e15cf8`) + roles + demo mint. Fallback for the stage:
      the prep token `0x6a99C9e95DD220172d07D5f730f2e82B466D4B67` (already allowlisted, buyer holds 845.00).
- [x] T4 Bank facilitator (§4.3): `/x402/verify`, `/x402/settle`, `/x402/supported` for EURC **and**
      tCZK; screening in `on_before_verify` **and** `on_before_settle`; refusal = HTTP 200 `isValid:false`;
      sync `def` endpoints, one process, lock on settle, own retry with idempotency key.
      (T3: deploy proven on an anvil fork; the Base Sepolia broadcast waits for the bank key —
      command in `contracts/README.md`.)

## Stream Q — validation on real testnet transactions (SPEC §19)

- [x] Q1 Test scaffolding: `pytest` + `pytest-cov` config, `tests/unit/` fixtures (temp SQLite, fake
      clock, fake chain for unit level), `timed(stage, deal_id)` → `timings` table (§19.4), CI workflow
      (MOCK=1, no secrets, coverage report). (`.github/workflows/ci.yml`: pytest + branch coverage, forge without fork)
- [x] Q2 `scripts/smoke.py` (< 60 s): `/health` of bank and seller, facilitator `/supported` lists EURC
      and tCZK, RPC reachable, ETH/EURC/tCZK above thresholds, unpaid request → 402, one Bedrock call
      (or MOCK), ARES reachable, one 0.01 EURC payment lands. (9/9 on the anvil fork; `--target running|fork|sepolia`)
- [ ] Q3 `scripts/scenarios.py --runs 3 [--only N05] [--p0]`: P0 matrix §19.2 on Base Sepolia; each
      scenario asserts decision + on-chain balance/escrow deltas (read from chain) + journal; negatives
      assert "no money moved". N13/N14 = timer defaults with demo windows. Small amounts (`SCENARIO_PRICE_EUR`). N10/N11 send real reverting txs
      with manual gas.
  - [x] Runner + stack (`scripts/stack.py`), S01–S04, N01–N12 and A01 (API auth) pass 3/3 on an anvil fork of Base Sepolia (Q)
  - [x] S05, N13, N14 against bank.disputes + deadlines.tick(now + window), 3/3 on the fork (Q)
  - [x] One live P0 run on Base Sepolia (`--p0 --runs 1 --target sepolia`): 18/20 pass, N10/N11 not run (prep tCZK has no
        strict mode, buyer has no ETH); `reports/validation-sepolia.md` (Q)
  - [x] N10/N11 live on the strict-mode TCZK: direct transfer reverts `TransferViaBankOnly`, replayed signature reverts
        `ERC3009UsedAuthorization` (`reports/scenarios-sepolia-n10n11.json`, integrator)
- [ ] Q4 Fault injection via `FAULTS` (`settle_fail_once`, `registry_timeout`, `llm_bad_json`,
      `webhook_duplicate`, `webhook_bad_hmac`) with their scenarios; reconciliation of chain
      `Transfer` events vs DB vs statement; journal tamper test.
  - [x] settle_fail_once, registry_timeout, llm_bad_json (F01–F03, 3/3 on the fork + unit tests); reconciliation;
        `scripts/verify_journal.py` + tamper test (Q)
  - [ ] webhook_duplicate / webhook_bad_hmac — wait for the Tatum webhook (L7, xfail D3)
- [ ] Q5 Evals: `tests/evals/injections.jsonl` (20), `judge.jsonl` (40 labelled), explanation checks
      (sources subset, no non-public reason in client text, AI label); MOCK in CI, live Bedrock once.
  - [x] 20 injections through the buyer tool loop (money-level block 20/20), judge 40 items, explanations; MOCK in CI (Q)
  - [ ] Live Bedrock once (`LIVE=1 MOCK=0 pytest tests/evals/test_live_bedrock.py`)
- [ ] Q6 `scripts/report.py` → `reports/validation.md|json`: scenarios × runs with explorer links,
      attacks blocked X/X, wrong debits 0, p50/p95 per stage vs targets, coverage, evals, AWS cost.
      `/bank` panel: last decision ms + p95. P1 scenarios only if time is left.
  - [x] Report builder; fork-run report committed, labelled "anvil fork", tx hashes without links (Q)
  - [x] Base Sepolia report with explorer links (`reports/validation-sepolia.md`) (Q)
  - [ ] `/bank` panel: last decision ms + p95 (stream U)

## Layers

- [x] **L1 x402 EURC** — seller 402 with EURC option; buyer pays via the bank facilitator (fallback
      x402.org); tx hash in `PAYMENT-RESPONSE` and explorer. Client `allowed_assets` for EURC/tCZK.
      Route key `GET /data/:offer_id` (never `{offer_id}`); price via `DynamicPrice`; unpaid request → 402.
  - [x] Seller 402 with EURC + tCZK from the offer (`DynamicPrice`, `:offer_id` keys), buyer helper with
        `allowed_assets` caps, `scripts/demo_l1.py`; unpaid → 402 tested; paid run settled on an anvil fork (P)
  - [x] Live run on Base Sepolia through the bank facilitator: 0.01 EURC, tx 0xa0f7…441f; S01 in the P0 run (integrator, Q)
- [x] **L2 Mandate + agents + negotiation + attack** (stream A; the buyer agent runs through the real bank API with
      agent tokens — integrator; one live Bedrock run still to do) — mandate JSON + versions + hash (limits in euro
      cents: 10 € per deal, confirm > 6 €, 30 €/month; task budget 5 €; tCZK → EUR at `CZK_PER_EUR_DEMO`); atomic reservations
      (`BEGIN IMMEDIATE`); every refusal code reproduced by a test; buyer tool loop (manual
      `stop_reason == "tool_use"` loop, JSON+pydantic, MOCK mode); seller mode `injection` →
      `COUNTERPARTY_NOT_IN_SCOPE` / `AMOUNT_ABOVE_LIMIT`, journal source `untrusted:seller`; race test:
      10 parallel `pay()` with budget for 3 → exactly 3 reservations; minimal
      negotiation (1–2 turns: offer → counter) ends in an offer within the task budget (brief requirement).
      VAT risk acknowledged once per (seller IČO, asset) → `vat_risk_acknowledged` in a new mandate version.
- [x] **L3 Escrow + verifier** — PaymentInfo persisted before `authorize`; seller checks escrow
      on-chain before fulfilling; rules per item + Haiku judge flags (JSON+pydantic; invalid → flag all
      to human); no ARES check on Google Maps places (they have no IČO); `website` required;
      18/20 → capture 4.50 + void 0.50. Reuse `prep/spikes/escrow_spike.py` techniques.
  - [x] Escrow part (P): PaymentInfo stored before `authorize`, seller checks escrow on chain before
        delivering, authorize → capture 4.50 → void 0.50 proven on an anvil fork against the real escrow/collector
  - [x] Verifier rules + judge (stream S, judge only flags — items without the keyword go to a human); live Base Sepolia
        escrow runs S02–S05 with explorer links in `reports/validation-sepolia.md`
- [ ] **L4 Counterparty check** — IČO checksum, ARES, VAT payers SOAP (unreliable + published
      accounts), ISIR, EU sanctions XML (companies only, fuzzy ≥ 90), Mostly Right datasets (if key),
      Apify actor `company-check` (web footprint only: IČO on the site + Google Maps place; registries are
      checked by the bank) + 8 evals (incl. injection text on a site); live IČO check for legal entities
      only (forms 112/121); cache; two-level
      explanations; Delta blocked before payment.
  - [x] `bank/screening`: checksum, ARES, VAT payers SOAP, ISIR, EU sanctions (local index, non-public
        near-match), parallel 2 s fail-closed, 1 h cache, legal forms 112/121 only; parsers tested on answers
        recorded live 19.10; live ČEZ check passes (`LIVE=1`). Synthetic sellers from `fixtures/registries/`;
        Delta → level `blocked`, public `ico_mismatch_on_website` (S)
  - [x] Actor `actors/company-check` + 8 evals (mocked HTTP, injection site); ran locally with apify 4.1 (S)
  - [ ] Push the actor to Apify, record the Google Maps dataset (`fixtures/maps/record_maps.py`) — needs `APIFY_TOKEN`
  - [ ] Mostly Right datasets — code + tests ready, needs `MOSTLYRIGHT_API_KEY` (confirm the response shape)
  - [x] Delta refused before payment end to end (`tests/unit/test_integration.py`, N06 in the live P0 run)
  - [x] L3 verifier part (S): `bank/verifier.py` rules per item, flag-only judge (invalid JSON → all to a
        human), `settlement()` 18/20 → capture 4 500 000 / void 500 000; judge eval seed `tests/evals/judge.jsonl`
- [x] **L5 Dispute, UI, statement** — `dispute_terms` in the offer (§9.1); `bank/deadlines.py` timer
      thread every 5 s applying silence defaults (buyer silent → capture, seller silent → void part,
      decision timeout → rules verdict), journal `actor: timer`, never past authorizationExpiry − 1 h;
      5% holdback captured after its window; capture before the final void (no partial void);
      dispute card (items, rule results + violated criterion, deadlines and silence outcome, judge reasons with sources,
      seller statement, accept/reject per item) → `capture`/`void`; pages `/bank` (client/compliance
      toggle), `/mandate`, `/deals`, `/deals/{id}/dispute`, `/statement`; AI-generated label.
  - [x] `bank/disputes.py` (U): items in question wait in escrow, the rest captured at once minus the 5% holdback;
        buyer review (reject only with an agreed criterion + evidence), seller objection, bank decision; every
        step journalled `human:<who>`/`timer`; captures written down before sending (idem key per n), final void last
  - [x] `bank/deadlines.py` (U): timer thread every 5 s from the UI router's lifespan; silence defaults, holdback
        release, deadlines clamped to authorizationExpiry − 30 s, expiry alarm; fake-clock tests N13, N14,
        holdback, no double capture on repeated ticks / lost tx answer
  - [x] Pages (U): `/bank` (client view only via `codes.client_view`, compliance view, decision ms + p95, model
        cost), `/mandate` (versions + policy_hash, budget, VAT ack / confirm buttons), `/deals`, `/deals/{id}`,
        dispute card, `/statement` + chain reconcile; five §13 demo buttons; `DEMO_SIM=1` simulated chain (labelled)
  - [x] Wire `deals.auto_settle` → `disputes.settle` (integrator); S05/N13/N14 live on Base Sepolia (timer driven by the runner)
- [ ] **L6 Bank answer** — same deal in tCZK through escrow + bank facilitator; strict mode; VAT
      rule (§6.5): EURC → "VAT risk acknowledged" mark (acknowledged in scene 1), tCZK 125,00 Kč → passes
      (account published). `TCZK_NAME` must match the token in use.
  - [x] Strict-mode TCZK deployed and source-verified: 0x18245366edA63Ca583c11B27d1134F3d233A115F (T3, integrator)
  - [x] VAT rule both ways + tCZK deal without the question (integration tests); S04 (tCZK escrow) live on the prep token
  - [ ] One live tCZK escrow deal on the strict-mode token (the demo's final scene) before the demo
- [ ] **L7 Negotiation polish, Tatum, journal** — up to 3 turns ("20 for 6 €" refused by the task budget); Tatum webhook via tunnel with HMAC
      on raw body + dedupe by `txId` (fallback: RPC polling); `scripts/verify_journal.py`.
  - [x] AI seller agent in the seller service, up to 3 turns, code owns the price; `SELLER_OPENING=list`: 6 € refused
        by the 5 € task budget, the buyer agent haggles to 5 € and pays (integrator, tests in test_integration)
  - [x] `scripts/verify_journal.py` + tamper test (Q); RPC on the Tatum Starter plan (eth_call on)
  - [ ] Tatum webhook (public URL via a tunnel, HMAC, dedupe) — not built; F04/F05 stay xfail
- [ ] **L8 Polish** — README ("Built during the hackathon", "Existed before: prep/", AI-assistant
      disclosure, run instructions, Apify usage highlighted, list of what came from `prep/`, AWS credit
      spend from the per-call token log, numbers from `reports/validation.md`); three clean demo runs in a
      row within 4 minutes; `smoke.py` green right before the demo; freeze 13:30.
