# Numbers — every measurable part of Collabse

> Generated 20.10 10:38 at e799084 (offline benchmarks); for scenario results see [validation-sepolia.md](validation-sepolia.md) and [validation.md](validation.md).

Generated 2026-10-20 10:38 CEST by `scripts/numbers.py` at commit `e799084`. Offline: DEMO_SIM (simulated chain, in-process seller), MOCK model, no keys, no transactions. Testnet and live-model figures are read from the committed reports named in *Source*. A prototype validated on testnet, not production-ready.

Machine: arm64 Darwin 25.6.0, Python 3.12.7, 10 CPUs, load avg 34.3. Micro-benchmarks ran on a shared laptop: p50 is the stable figure, p95 includes scheduler noise.


## code

| Metric | Value | How | Source |
|---|---|---|---|
| bank (Python): lines (non-blank, non-comment) | 7585 (5963) lines | line count of tracked source files | bank/**/*.py |
| bank UI templates: lines (non-blank, non-comment) | 537 (527) lines | line count of tracked source files | bank/templates/**/* |
| agents: lines (non-blank, non-comment) | 662 (529) lines | line count of tracked source files | agents/**/*.py+agents/prompts/**/*+prompts/**/* |
| seller: lines (non-blank, non-comment) | 469 (392) lines | line count of tracked source files | seller/**/*.py |
| scripts: lines (non-blank, non-comment) | 3895 (3280) lines | line count of tracked source files | scripts/**/*.py |
| tests: lines (non-blank, non-comment) | 6109 (4688) lines | line count of tracked source files | tests/**/*.py+tests/**/*.jsonl |
| Apify actor: lines (non-blank, non-comment) | 271 (220) lines | line count of tracked source files | actors/company-check/**/*.py+actors/company-check/Dockerfile |
| contracts (Solidity, own): lines (non-blank, non-comment) | 755 (618) lines | line count of tracked source files | contracts/src/**/*.sol+contracts/test/**/*.sol+contracts/script/**/*.sol |
| fixtures: lines (non-blank, non-comment) | 2657 (2657) lines | line count of tracked source files | fixtures/**/*.json |
| infra/deploy: lines (non-blank, non-comment) | 475 (441) lines | line count of tracked source files | infra/**/*+deploy/**/*+Dockerfile |
| total lines (all components above) | 23415 lines | sum |  |
| git commits on this branch | 119 | git rev-list --count HEAD | git |

## tests

| Metric | Value | How | Source |
|---|---|---|---|
| tests collected (unit + evals, incl. live/fork ones that skip offline) | 512 in 33 files tests | pytest --collect-only -q | tests/ |
| tests collected / passed / failed / skipped | 512 / 506 / 0 / 6 | pytest (MOCK=1, FORK=0, LIVE=0 as in CI), junit.xml | pytest run of this script |
| test suite runtime (with coverage) | 844.5 s | junit.xml total time |  |
|   evals: tests, seconds | 27, 79.7 | junit.xml per class prefix |  |
|   unit: tests, seconds | 485, 713.1 | junit.xml per class prefix |  |
| test suite CPU time (user + system) | 134.8 s | shell `time`; wall time above is mostly waiting (timeouts, timers, a loaded shared machine) | pytest.log |
| slowest test | 68.63 s (setup) tests/evals/test_injections.py::test_money_level_block_rate_is_100_percent | pytest --durations=15 |  |
| coverage of bank/: lines / branches | 89.8 / 82.8 (4768 statements, 1586 branches) % | pytest --cov=bank --cov-branch, coverage.json totals |  |
|   coverage bank/mandate.py: lines / branches | 96.8 / 91.7 % | coverage.json per file |  |
|   coverage bank/verifier.py: lines / branches | 89.8 / 84.1 % | coverage.json per file |  |
|   coverage bank/explain.py: lines / branches | 100.0 / 96.4 % | coverage.json per file |  |
|   coverage bank/escrow.py: lines / branches | 93.5 / 70.6 % | coverage.json per file |  |
|   coverage bank/deals.py: lines / branches | 88.6 / 79.2 % | coverage.json per file |  |
|   coverage bank/facilitator.py: lines / branches | 93.2 / 91.3 % | coverage.json per file |  |
|   coverage bank/journal.py: lines / branches | 100.0 / 100.0 % | coverage.json per file |  |

## lists

| Metric | Value | How | Source |
|---|---|---|---|
| decision codes (closed enum, SPEC §6.3) | 13 codes | len(bank.codes.Code) | bank/codes.py |
|   of them requires_user_action (confirm / VAT) | 2 codes | codes.SPEC status | bank/codes.py |
| predicate operators | 10 ops | len(verifier.OPS) | bank/verifier.py |
| item autocodes (per item) | 5 codes | len(verifier.ITEM_CODES) | bank/verifier.py |
| delivery autocodes (per delivery) | 6 codes | len(verifier.DELIVERY_CODES) | bank/verifier.py |
| bank-error codes (refunded from the bank's money) | 2 codes | len(verifier.BANK_ERROR_CODES) | bank/verifier.py |
| personal-data fields never stored | 7 fields | len(verifier.PERSONAL_FIELDS) | bank/verifier.py |
| journal event types (literal in code) | 75 types | regex over journal.append(...) and deals._set(...) literals | bank/ scripts/ agents/ seller/ |

## scenarios

| Metric | Value | How | Source |
|---|---|---|---|
| scenarios-fork: success | 5 scen., 15 runs: 15 pass, 0 xfail, 0 fail | status per result row | reports/scenarios-fork.json |
| scenarios-fork: negative/attack | 14 scen., 42 runs: 42 pass, 0 xfail, 0 fail | status per result row | reports/scenarios-fork.json |
| scenarios-fork: API auth | 1 scen., 3 runs: 3 pass, 0 xfail, 0 fail | status per result row | reports/scenarios-fork.json |
| scenarios-fork: fault injection | 5 scen., 15 runs: 9 pass, 6 xfail, 0 fail | status per result row | reports/scenarios-fork.json |
| scenarios-fork: reconciliation chain↔DB↔statement | 39/39 ok, wrong debits 0 deals | reconciliation block | reports/scenarios-fork.json |
| scenarios-fork: scenario wall time p50/p95 | 0.84 / 7.36 s | results[].seconds | reports/scenarios-fork.json |
| scenarios-sepolia-n10n11: negative/attack | 2 scen., 2 runs: 2 pass, 0 xfail, 0 fail | status per result row | reports/scenarios-sepolia-n10n11.json |
| scenarios-sepolia-n10n11: reconciliation chain↔DB↔statement | 0/0 ok, wrong debits 0 deals | reconciliation block | reports/scenarios-sepolia-n10n11.json |
| scenarios-sepolia-n10n11: scenario wall time p50/p95 | 11.91 / 15.41 s | results[].seconds | reports/scenarios-sepolia-n10n11.json |
| scenarios-sepolia-tczk-strict: success | 1 scen., 1 runs: 1 pass, 0 xfail, 0 fail | status per result row | reports/scenarios-sepolia-tczk-strict.json |
| scenarios-sepolia-tczk-strict: reconciliation chain↔DB↔statement | 1/1 ok, wrong debits 0 deals | reconciliation block | reports/scenarios-sepolia-tczk-strict.json |
| scenarios-sepolia-tczk-strict: scenario wall time p50/p95 | 27.72 / 27.72 s | results[].seconds | reports/scenarios-sepolia-tczk-strict.json |
| scenarios-sepolia: success | 5 scen., 5 runs: 5 pass, 0 xfail, 0 fail | status per result row | reports/scenarios-sepolia.json |
| scenarios-sepolia: negative/attack (N10/N11: precondition not met in the full run, re-run alone in scenarios-sepolia-n10n11: pass) | 14 scen., 14 runs: 12 pass, 0 xfail, 2 fail | status per result row | reports/scenarios-sepolia.json |
| scenarios-sepolia: API auth | 1 scen., 1 runs: 1 pass, 0 xfail, 0 fail | status per result row | reports/scenarios-sepolia.json |
| scenarios-sepolia: reconciliation chain↔DB↔statement | 12/12 ok, wrong debits 0 deals | reconciliation block | reports/scenarios-sepolia.json |
| scenarios-sepolia: scenario wall time p50/p95 | 14.94 / 33.6 s | results[].seconds | reports/scenarios-sepolia.json |

## timings (Base Sepolia)

| Metric | Value | How | Source |
|---|---|---|---|
| decision | 0.573 / 13.259 ms p50/p95 | n=44 samples, nearest rank | reports/scenarios-sepolia.json stages |
| screening | 21.67 / 129.45 ms p50/p95 | n=37 samples, nearest rank | reports/scenarios-sepolia.json stages |
| facilitator_verify | 375.274 / 809.099 ms p50/p95 | n=2 samples, nearest rank | reports/scenarios-sepolia.json stages |
| facilitator_settle | 325.883 / 1292.664 ms p50/p95 | n=2 samples, nearest rank | reports/scenarios-sepolia.json stages |
| refusal_to_client | 0.73 / 42.676 ms p50/p95 | n=13 samples, nearest rank | reports/scenarios-sepolia.json stages |
| api_pay | 123.897 / 6653.464 ms p50/p95 | n=24 samples, nearest rank | reports/scenarios-sepolia.json stages |
| quote | 18.03 / 23.769 ms p50/p95 | n=25 samples, nearest rank | reports/scenarios-sepolia.json stages |
| negotiate_proxy | 24.618 / 52.839 ms p50/p95 | n=25 samples, nearest rank | reports/scenarios-sepolia.json stages |
| notify_seller | 1800.902 / 2146.882 ms p50/p95 | n=12 samples, nearest rank | reports/scenarios-sepolia.json stages |
| escrow_authorize | 3230.716 / 4898.778 ms p50/p95 | n=12 samples, nearest rank | reports/scenarios-sepolia.json stages |
| escrow_capture | 1569.262 / 3341.081 ms p50/p95 | n=23 samples, nearest rank | reports/scenarios-sepolia.json stages |
| escrow_void | 1072.313 / 3588.987 ms p50/p95 | n=4 samples, nearest rank | reports/scenarios-sepolia.json stages |
| tx_in_block (all kinds) | 193.306 / 830.652 ms p50/p95 | n=40 samples, nearest rank | reports/scenarios-sepolia.json stages |
| tx_in_block:x402_settle | 158.122 / 158.122 ms p50/p95 | n=1 samples, nearest rank | reports/scenarios-sepolia.json stages |
| tx_in_block:authorize | 285.808 / 869.909 ms p50/p95 | n=12 samples, nearest rank | reports/scenarios-sepolia.json stages |
| tx_in_block:capture | 205.327 / 799.02 ms p50/p95 | n=23 samples, nearest rank | reports/scenarios-sepolia.json stages |
| tx_in_block:void | 135.611 / 764.624 ms p50/p95 | n=4 samples, nearest rank | reports/scenarios-sepolia.json stages |

## timings (fork)

| Metric | Value | How | Source |
|---|---|---|---|
| decision | 0.478 / 31.86 ms p50/p95 | n=144 samples, nearest rank | reports/scenarios-fork.json stages |
| screening | 21.961 / 123.888 ms p50/p95 | n=119 samples, nearest rank | reports/scenarios-fork.json stages |
| facilitator_verify | 12.576 / 610.784 ms p50/p95 | n=6 samples, nearest rank | reports/scenarios-fork.json stages |
| facilitator_settle | 660.591 / 841.558 ms p50/p95 | n=12 samples, nearest rank | reports/scenarios-fork.json stages |
| refusal_to_client | 3.459 / 47.351 ms p50/p95 | n=42 samples, nearest rank | reports/scenarios-fork.json stages |
| api_pay | 93.027 / 2502.436 ms p50/p95 | n=78 samples, nearest rank | reports/scenarios-fork.json stages |
| quote | 6.248 / 17.277 ms p50/p95 | n=81 samples, nearest rank | reports/scenarios-fork.json stages |
| negotiate_proxy | 22.499 / 62.623 ms p50/p95 | n=81 samples, nearest rank | reports/scenarios-fork.json stages |
| notify_seller | 15.57 / 25.188 ms p50/p95 | n=39 samples, nearest rank | reports/scenarios-fork.json stages |
| escrow_authorize | 1934.14 / 2543.94 ms p50/p95 | n=39 samples, nearest rank | reports/scenarios-fork.json stages |
| escrow_capture | 677.323 / 1741.794 ms p50/p95 | n=72 samples, nearest rank | reports/scenarios-fork.json stages |
| escrow_void | 688.872 / 1772.419 ms p50/p95 | n=12 samples, nearest rank | reports/scenarios-fork.json stages |
| tx_in_block (all kinds) | 641.403 / 752.085 ms p50/p95 | n=139 samples, nearest rank | reports/scenarios-fork.json stages |
| tx_in_block:x402_settle | 637.589 / 795.209 ms p50/p95 | n=9 samples, nearest rank | reports/scenarios-fork.json stages |
| tx_in_block:authorize | 642.984 / 752.618 ms p50/p95 | n=39 samples, nearest rank | reports/scenarios-fork.json stages |
| tx_in_block:capture | 640.484 / 752.085 ms p50/p95 | n=72 samples, nearest rank | reports/scenarios-fork.json stages |
| tx_in_block:void | 647.14 / 729.748 ms p50/p95 | n=12 samples, nearest rank | reports/scenarios-fork.json stages |
| tx_in_block:tczk_disallow | 636.743 / 636.743 ms p50/p95 | n=1 samples, nearest rank | reports/scenarios-fork.json stages |
| tx_in_block:attack_direct_transfer | 628.234 / 641.403 ms p50/p95 | n=3 samples, nearest rank | reports/scenarios-fork.json stages |
| tx_in_block:attack_replay | 619.222 / 736.443 ms p50/p95 | n=3 samples, nearest rank | reports/scenarios-fork.json stages |

## evals

| Metric | Value | How | Source |
|---|---|---|---|
| injection variants (+ controls) | 20 (+2) | tests/evals/injections.jsonl rows | reports/evals-live.json |
| money blocked: variants / model runs | 20/20 · 50/50 | lock went only to the registered payee | reports/evals-live.json |
| live model runs: money blocked / unsafe tool attempts | 17/17 · 0 | Opus 4.5 buyer, live Bedrock | reports/evals-live.json |
| outcome as expected (all / live) | 19/22 · 16/17 | per-variant expectation | reports/evals-live.json |
| item-level injections paid | 0 of 3 variants | injection inside delivered items | reports/evals-live.json |
| judge: items, precision, recall | 40, 1.0, 1.0 | tp 19 fp 0 fn 0 tn 21; eu.anthropic.claude-haiku-4-5-20251001-v1:0 | reports/evals-live.json |
| judge valid JSON rate | 1.0 | pydantic validation of the answer | reports/evals-live.json |
| explanations: texts / client texts clean / AI texts labelled | 64 / 32/32 / 29/29 | no hidden AML reason, sources subset, AI Act Art. 50 label | reports/evals-live.json |
| eval cases in tests/evals/injections.jsonl | 22 cases | non-empty lines | tests/evals/injections.jsonl |
| eval cases in tests/evals/predicates.jsonl | 27 cases | non-empty lines | tests/evals/predicates.jsonl |
| Apify actor company-check: eval tests (+ parametrized cases) | 11 (+4) | def test_ in tests/evals/test_company_check.py | tests/evals/test_company_check.py |

## tests (committed report)

| Metric | Value | How | Source |
|---|---|---|---|
| coverage of bank/ in the committed validation report (lines / branches) | 90.2 / 81.2 % | pytest --cov at commit 3af5a5b | reports/validation.json |

## DEMO_SIM

| Metric | Value | How | Source |
|---|---|---|---|
| scenario 'honest': wall time, outcome | 2.029 s, VAT_LIABILITY_RISK | demo_scenarios.run_safe in-process (simulated chain, MOCK model) | scripts/numbers.py |
| scenario 'honest': human VAT acknowledgement on /mandate -> bank pays | 4.292 s, ok | deals.ack_vat (what the /mandate button calls) | scripts/numbers.py |
| scenario 'fake_data': wall time, outcome | 1.524 s, ok | demo_scenarios.run_safe in-process (simulated chain, MOCK model) | scripts/numbers.py |
| scenario 'attack': wall time, outcome | 1.02 s, COUNTERPARTY_NOT_IN_SCOPE | demo_scenarios.run_safe in-process (simulated chain, MOCK model) | scripts/numbers.py |
| scenario 'suspicious': wall time, outcome | 0.616 s, COUNTERPARTY_NOT_TRUSTED | demo_scenarios.run_safe in-process (simulated chain, MOCK model) | scripts/numbers.py |
| scenario 'tczk': wall time, outcome | 0.494 s, CONFIRMATION_REQUIRED | demo_scenarios.run_safe in-process (simulated chain, MOCK model) | scripts/numbers.py |
| scenario 'tczk': human confirmation on /mandate -> bank pays | 0.349 s, ok | deals.confirm (what the /mandate button calls) | scripts/numbers.py |
| timer pass after the windows (rule B rest, voids) | 53.0 ms, 3 actions | deadlines.tick(now + 1 day) | bank/deadlines.py |
| five scenarios end to end (incl. the two human clicks as calls) | 10.32 s | wall time of the loop above | scripts/numbers.py |
| deals per minute (end to end, sequential, one bank process) | 29.1 deals/min | 5 deals / 10.3 s; depends on machine load (see header) | scripts/numbers.py |
| stage decision p50/p95 | 1.617 / 9.361 ms | bank timed(), n=12 | timings table of the sim run |
| stage api_pay p50/p95 | 74.74 / 741.109 ms | bank timed(), n=5 | timings table of the sim run |
| stage quote p50/p95 | 30.365 / 76.542 ms | bank timed(), n=7 | timings table of the sim run |
| stage negotiate_proxy p50/p95 | 62.415 / 251.894 ms | bank timed(), n=7 | timings table of the sim run |
| stage escrow_authorize p50/p95 | 1.987 / 2.812 ms | bank timed(), n=3 | timings table of the sim run |
| stage escrow_capture p50/p95 | 1.128 / 6.113 ms | bank timed(), n=6 | timings table of the sim run |
| stage screening p50/p95 | 105.346 / 521.356 ms | bank timed(), n=36 | timings table of the sim run |
| model calls per deal: buyer | 5.2 calls | MOCK calls in the timings table / 5 deals | timings table of the sim run |
| model calls per deal: criteria | 1.0 calls | MOCK calls in the timings table / 5 deals | timings table of the sim run |
| model calls per deal: seller | 1.4 calls | MOCK calls in the timings table / 5 deals | timings table of the sim run |
| journal entries written by the five scenarios / distinct event types | 171 / 31 | SELECT FROM journal | sim db |
| journal hash chain after the run | ok | journal.verify() | bank/journal.py |
| simulated escrow txs by kind (five scenarios) | authorize 3, capture 6, void 1 | onchain_txs rows (simulated) | sim db |

## UI render

| Metric | Value | How | Source |
|---|---|---|---|
| GET /story | 25.685 / 117.999 ms p50/p95 | TestClient, 50 requests after the five scenarios, HTTP [200], 16329 bytes | scripts/numbers.py |
| GET /bank | 26.481 / 218.862 ms p50/p95 | TestClient, 50 requests after the five scenarios, HTTP [200], 27366 bytes | scripts/numbers.py |
| GET /mandate | 16.24 / 56.73 ms p50/p95 | TestClient, 50 requests after the five scenarios, HTTP [200], 11339 bytes | scripts/numbers.py |
| GET /deals | 21.568 / 54.877 ms p50/p95 | TestClient, 50 requests after the five scenarios, HTTP [200], 11351 bytes | scripts/numbers.py |
| GET /deals/{id} | 37.103 / 104.152 ms p50/p95 | TestClient, 50 requests after the five scenarios, HTTP [200], 23748 bytes | scripts/numbers.py |
| GET /statement | 29.751 / 91.416 ms p50/p95 | TestClient, 50 requests after the five scenarios, HTTP [200], 12223 bytes | scripts/numbers.py |
| GET /statement/reconcile | 41.871 / 137.866 ms p50/p95 | TestClient, 50 requests after the five scenarios, HTTP [200], 847 bytes | scripts/numbers.py |

## bench

| Metric | Value | How | Source |
|---|---|---|---|
| mandate decision (decide: mandate + cached check + explanation + timing row) | 0.478 / 4.807 ms p50/p95 | 1000 calls with the arguments of the first DEMO_SIM decision (result: ok), SQLite on local disk | bank/mandate.py |
| predicate engine per 20-item delivery (7 predicates) | 6.99 / 140.985 ms p50/p95 | 200 calls of verifier.evaluate with a real delivery's arguments (registry from fixtures) | bank/verifier.py |
| predicate engine per item | 0.231 / 2.112 ms p50/p95 | 1000 calls on one item | bank/verifier.py |
| Merkle root of 20 items (JCS + SHA-256) | 0.67 / 5.955 ms p50/p95 | 500 calls of receipts.merkle_root | bank/receipts.py |
| Merkle proof build + verify (one item) | 0.711 / 22.815 ms p50/p95 | 500 calls merkle_proof + verify_proof | bank/receipts.py |
| manifest EIP-712 signing | 9.785 / 119.107 ms p50/p95 | 300 calls, throwaway key | bank/receipts.py |
| manifest signature recovery (verify) | 15.659 / 72.251 ms p50/p95 | 300 calls of manifest_signer | bank/receipts.py |

## model

| Metric | Value | How | Source |
|---|---|---|---|
| buyer (claude-opus-4-5-20251101): calls, tokens in/out, $ | 67, 132334/11299, $1.0386 | live eval run, tokens logged per call x list price (+10 % EU profile) | reports/evals-live.json |
| buyer: mean latency per call | 3671 ms | stage wall ms / calls (no per-call samples kept -> no p50/p95) | reports/evals-live.json |
| criteria (claude-opus-4-5-20251101): calls, tokens in/out, $ | 17, 1564/901, $0.0334 | live eval run, tokens logged per call x list price (+10 % EU profile) | reports/evals-live.json |
| criteria: mean latency per call | 1830 ms | stage wall ms / calls (no per-call samples kept -> no p50/p95) | reports/evals-live.json |
| explain (claude-haiku-4-5-20251001): calls, tokens in/out, $ | 29, 4828/2195, $0.0174 | live eval run, tokens logged per call x list price (+10 % EU profile) | reports/evals-live.json |
| explain: mean latency per call | 1059 ms | stage wall ms / calls (no per-call samples kept -> no p50/p95) | reports/evals-live.json |
| judge (claude-haiku-4-5-20251001): calls, tokens in/out, $ | 4, 7769/5001, $0.0361 | live eval run, tokens logged per call x list price (+10 % EU profile) | reports/evals-live.json |
| judge: mean latency per call | 5453 ms | stage wall ms / calls (no per-call samples kept -> no p50/p95) | reports/evals-live.json |
| eval run total: calls, tokens in/out, $ | 117, 146495/19396, $1.1254 | sum | reports/evals-live.json |
| buyer agent: live calls per deal | 3.94 calls | 67 buyer calls / 17 live deals | reports/evals-live.json |
| $ per deal (estimate: live $/call x calls per deal) | 0.0811 USD | buyer: live avg per deal; other stages: MOCK call count per deal in DEMO_SIM x live $/call | evals-live + sim |
|   $ per deal: buyer | 0.06109 USD | as above |  |
|   $ per deal: criteria | 0.00196 USD | as above |  |
|   $ per deal: judge (20-item delivery) | 0.01803 USD | as above |  |

## bench

| Metric | Value | How | Source |
|---|---|---|---|
| journal append (1000 entries) | 0.29 / 2.808 ms p50/p95 | journal.append (hash chain + SQLite insert) on a fresh DB | bank/journal.py |
| journal full-chain verify (1000 entries) | 0.1023 s (102.28 µs/entry), ok=True | journal.verify() recomputes every hash | bank/journal.py |
| journal append (10000 entries) | 0.189 / 3.562 ms p50/p95 | journal.append (hash chain + SQLite insert) on a fresh DB | bank/journal.py |
| journal full-chain verify (10000 entries) | 0.1747 s (17.47 µs/entry), ok=True | journal.verify() recomputes every hash | bank/journal.py |
| budget race: 10 parallel pay(), budget for 3 | 20 rounds, overspend 0, paid per round [3] | threads behind a barrier on the offline bank (mandate.reserve, BEGIN IMMEDIATE); sum(reservations) <= budget | agents/offline.py + bank/mandate.py |
| budget race: wall time for 10 parallel pay() | 162.514 / 301.292 ms p50/p95 | 20 rounds | scripts/numbers.py |

## on-chain

| Metric | Value | How | Source |
|---|---|---|---|
| gas: authorize | 161439 gas | median of 13 receipts | reports/credits.json (median gasUsed of Base Sepolia receipts) |
| gas: capture | 68475 gas | median of 25 receipts | reports/credits.json (median gasUsed of Base Sepolia receipts) |
| gas: void | 61208 gas | median of 5 receipts | reports/credits.json (median gasUsed of Base Sepolia receipts) |
| gas: x402_settle | 82518 gas | median of 2 receipts | reports/credits.json (median gasUsed of Base Sepolia receipts) |
| gas: mint | 71222 gas | median of 1 receipts | reports/credits.json (median gasUsed of Base Sepolia receipts) |
| Base mainnet gas price | 0.006 gwei | eth_gasPrice https://mainnet.base.org (now) |  |
| L2 execution cost per deal: rule A (authorize + capture) | 229,914 gas = 0.4138 ct EUR cents | gas x mainnet gas price x ETH 3000 € (L1 data fee not included) | reports/credits.json (median gasUsed of Base Sepolia receipts) |
| L2 execution cost per deal: rule B (authorize + 2 captures) | 298,389 gas = 0.5371 ct EUR cents | gas x mainnet gas price x ETH 3000 € (L1 data fee not included) | reports/credits.json (median gasUsed of Base Sepolia receipts) |
| L2 execution cost per deal: rule B with rejects (authorize + 2 captures + void) | 359,597 gas = 0.6473 ct EUR cents | gas x mainnet gas price x ETH 3000 € (L1 data fee not included) | reports/credits.json (median gasUsed of Base Sepolia receipts) |
| L2 execution cost per deal: x402 direct (settle) | 82,518 gas = 0.1485 ct EUR cents | gas x mainnet gas price x ETH 3000 € (L1 data fee not included) | reports/credits.json (median gasUsed of Base Sepolia receipts) |
| tariff vs gas: 5 € deal, T0 rule A (0 bps) | fee 0.00 € − gas 0.4138 ct = margin -0.0041 € (T0 billed monthly, nothing at capture) EUR | config.TARIFF_BPS x amount − L2 gas cost of the flow | bank/config.py |
| tariff vs gas: 100 € deal, T1 rule B (120 bps) | fee 1.20 € − gas 0.5371 ct = margin 1.1946 € EUR | config.TARIFF_BPS x amount − L2 gas cost of the flow | bank/config.py |
| tariff vs gas: 100 € deal, T1 rule A (80 bps) | fee 0.80 € − gas 0.4138 ct = margin 0.7959 € EUR | config.TARIFF_BPS x amount − L2 gas cost of the flow | bank/config.py |
| tariff vs gas: 1000 € deal, T2 rule C (150 bps) | fee 15.00 € − gas 0.6473 ct = margin 14.9935 € EUR | config.TARIFF_BPS x amount − L2 gas cost of the flow | bank/config.py |
| tariff vs gas: the 5 € demo deal at demo scale (T1, rule B, 120 bps) | fee 6.00 ct − gas 0.5371 ct = margin 5.46 ct EUR | deals.tier_of(500 cents) with DEMO_MONEY_DIVISOR=100 | bank/config.py |

## Apify

| Metric | Value | How | Source |
|---|---|---|---|
| company-check runs: n, $ median / max, runtime median | 5, $0.0122 / $0.3702, 26.0 s | GET /v2/actor-runs (last runs of the account) | reports/credits.json |
| account usage this cycle / limit | $0.0000 / $105 USD | GET /v2/users/me/limits | reports/credits.json |
| actor check functions | 9 functions | def in checks.py | actors/company-check/my_actor/checks.py |

## Not measurable offline — needs the live run

| What | Why | How to measure live |
|---|---|---|
| block → /bank feed latency (SPEC target ≤ 5 s) | needs a real block and the Tatum webhook or poller; bank_feed n=0 in both committed reports | run the five scenarios on Base Sepolia with the bank's poller on; stage bank_feed in the timings table |
| tx in block on Base Sepolia for the demo flow (not the matrix) | simulated chain offline | python -m scripts.demo_scenarios honest (no --sim) with funded keys; tx_in_block:* stages |
| Bedrock latency p50/p95 per model | evals-live.json keeps per-stage totals only; AWS creds expired | LIVE=1 pytest tests/evals/test_live_bedrock.py, then timings rows with model IS NOT NULL |
| seller-agent negotiation spend (AI seller haggles ≤ 3 turns) | not part of the live eval run | run the demo with SELLER_OPENING=list and MOCK=0; timings stage 'seller' |
| actual AWS bill so far | Cost Explorer needs valid credentials | AWS console → Billing, or aws ce get-cost-and-usage |
| L1 data fee per tx on Base mainnet | depends on Ethereum blob fees; Sepolia l1Fee (~200 wei) is not representative | eth_estimateGas + GasPriceOracle.getL1Fee on mainnet for the same calldata |
| facilitator verify p95 on a warm process | both reports include the first call that builds the facilitator (610 ms on the fork) | drop the first sample per process in the live run |
| Apify actor runtime/cost per check in the live demo | account runs listed are earlier test runs | run the demo with APIFY_TOKEN and read /v2/actor-runs after it |
