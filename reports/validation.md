# Validation report — anvil fork of Base Sepolia (chain id 84532) at block 48323122

> **Network: anvil fork of Base Sepolia (local), not the public testnet.** The real deployed contracts (EURC,
> AuthCaptureEscrow, ERC-3009 collector) forked at the block above, a freshly deployed tCZK, throwaway keys.
> The transactions are real EVM transactions on the local fork; their hashes exist only there, so they have
> **no explorer links**. The same matrix on Base Sepolia is a separate run (`--target sepolia`, keys from .env).

Generated 2026-10-19 14:57 from `reports/scenarios-fork.json` at commit `0c4810d` by `scripts/report.py`. A prototype validated on a fork of the testnet, ready for a bank sandbox pilot — not production-ready.

## Summary

- **24 scenarios** (19 P0 from SPEC §19.2 + 5 fault injections), 10 of them attacks or refusals, **3 runs each** = 72 scenario runs: 57 pass, 15 xfail (pending fix named below), 0 fail.
- **Attacks stopped before any money moved: 30/30** attack runs (balances equal on chain, the bank's signer nonces unchanged, no tx for the deal; N10/N11 reverted on chain); with the expected refusal code: 30/30.
- **Wrong debits: 0** — every buyer debit on chain since the first scenario is a lock or a payment a scenario expected. Transfers to the attacker address 0xdEaD: 0.
- **Reconciliation chain ↔ DB ↔ statement: OK** — 30/30 escrow deals (locked = captured + voided + still capturable, from Transfer events and paymentState; captured = journal = statement). Journal hash chains: verified.
- Injection evals: money-level block **20/20** variants (35/35 model runs). Judge (mock (keyword heuristic)): precision 0.62, recall 0.42 on 40 labelled items.
- Coverage of bank/: lines 91.1 %, branches 83.3 %.
- Model spend in the scenario runs: 48 calls, 0 in / 0 out tokens, $0.0000 (estimate at list prices, `bank/timing.py`; MOCK calls cost $0).

## Scenarios × runs

| ID | Scenario | Run 1 | Run 2 | Run 3 | Transactions (last run) |
|---|---|---|---|---|---|
| S01 | x402 EURC through the bank facilitator | PASS | PASS | PASS | x402_settle `0x53a37c84…bd8959` |
| S02 | Escrow EURC, all accepted | PASS | PASS | PASS | authorize `0x97e5a14d…e76899`, capture `0x30e9f7b0…e17082` |
| S03 | Escrow EURC, 18 of 20 | PASS | PASS | PASS | authorize `0xf1c48b3c…4cdc29`, capture `0x03fa3dbc…62efe4`, void `0x7ae581c3…30c323` |
| S04 | Same deal in tCZK through escrow | PASS | PASS | PASS | authorize `0x6a8e8df1…7efbd3`, capture `0x98ffde5f…d2c496`, void `0x9f9345b4…cb9326` |
| S05 | Dispute decided for the seller | XFAIL (D2) | XFAIL (D2) | XFAIL (D2) | — |
| N01 | Amount above the per-deal limit | PASS | PASS | PASS | — |
| N02 | Race: 10 parallel pay() with budget for 3 | PASS | PASS | PASS | authorize `0x12054c40…f4048f`, authorize `0x81762a43…0f0a26`, authorize `0x3b9a3674…839439`, capture `0xb27c1f90…cf5570`, capture `0x772b6039…667345`, capture `0x87806f0a…742abb` |
| N03 | payTo swapped in the 402 | PASS | PASS | PASS | — |
| N04 | Price swapped in the 402 | PASS | PASS | PASS | — |
| N05 | Injection in the seller's answer | PASS | PASS | PASS | — |
| N06 | Delta: IČO on site ≠ ARES | PASS | PASS | PASS | — |
| N07 | Garbage result → full void | PASS | PASS | PASS | authorize `0x48af83f0…bb641a`, void `0x76a3315a…2e1ae1` |
| N08 | VAT payer in EURC → acknowledged once | PASS | PASS | PASS | authorize `0xcd03d976…56d79c`, capture `0x919152bf…47879b`, authorize `0xc1a4f102…db4011`, capture `0xae13fd25…61464a` |
| N09 | tCZK to a payee off the allowlist | PASS | PASS | PASS | — |
| N10 | Direct tCZK transfer bypassing the bank | PASS | PASS | PASS | attack_direct_transfer `0x73537024…0bd45c` |
| N11 | Replay of the same EIP-3009 signature | PASS | PASS | PASS | x402_settle `0x42ed251e…7a19a1`, attack_replay `0x4588bbc0…c05059` |
| N12 | Unpaid request to a paid route | PASS | PASS | PASS | — |
| N13 | Buyer silent on flagged items → timer captures | XFAIL (D2) | XFAIL (D2) | XFAIL (D2) | — |
| N14 | Seller silent after a rejection → timer voids | XFAIL (D2) | XFAIL (D2) | XFAIL (D2) | — |
| F01 | settle_fail_once → exactly one tx | PASS | PASS | PASS | x402_settle `0x74b6addb…6f9b71` |
| F02 | registry_timeout → under review (fail-closed) | PASS | PASS | PASS | — |
| F03 | llm_bad_json → to a human, no money moves | PASS | PASS | PASS | authorize `0x3e724b25…781997`, void `0x50ee6d87…589527` |
| F04 | webhook_duplicate → counted once | XFAIL (D3) | XFAIL (D3) | XFAIL (D3) | — |
| F05 | webhook_bad_hmac → rejected | XFAIL (D3) | XFAIL (D3) | XFAIL (D3) | — |

### Pending fixes behind the xfails

| Defect | What | Scenarios |
|---|---|---|
| D2 | dispute timer and dispute API not merged (stream U: bank/deadlines.py, dispute decisions; N13/N14/S05) | N13, N14, S05 |
| D3 | Tatum webhook /webhooks/tatum not built (L7): no HMAC check, no dedupe by txId | F04, F05 |

## Speed (SPEC §19.4, measured in the matrix runs)

Fork caveat: anvil mines on demand, so *tx in block* here is local and says nothing about Base's 2 s blocks; registry checks use the recorded fixtures of the synthetic companies.

The `decision` samples include N02, where 10 concurrent pay() calls queue on one SQLite write lock (BEGIN IMMEDIATE) by design; `facilitator_verify` includes the first call of each bank process, which builds the facilitator.

| Stage | Target p95 | n | p50 | p95 | Met |
|---|---|---|---|---|---|
| Mandate decision + cached check + journal (`decision`) | ≤ 50 ms | 126 | 0.6 ms | 58.4 ms | no |
| Bank facilitator verify (with screening) (`facilitator_verify`) | ≤ 500 ms | 6 | 15.6 ms | 568.7 ms | no |
| New counterparty: ARES + VAT + ISIR + sanctions (`screening`) | ≤ 2500 ms | 103 | 26.6 ms | 136.3 ms | yes |
| Signature → tx in block (settle, authorize, capture, void) (`tx_in_block:*`) | ≤ 6000 ms | 82 | 622.3 ms | 675.5 ms | yes |
| Tx in block → row in the /bank feed (`bank_feed`) | ≤ 5000 ms | 0 | — | — | not measured |
| Refusal → message to the client (`refusal_to_client`) | ≤ 100 ms | 42 | 7.8 ms | 84.1 ms | yes |

## Coverage (SPEC §19.5)

| Target (SPEC §19.5) | Actual | Met |
|---|---|---|
| bank/ lines ≥ 80 % | 91.1 % | yes |
| bank/mandate.py branches ≥ 90 % | 92.4 % | yes |
| bank/verifier.py branches ≥ 90 % | 97.1 % | yes |
| bank/explain.py branches ≥ 90 % | 96.4 % | yes |
| bank/facilitator.py branches ≥ 90 % | 91.3 % | yes |
| bank/screening/__init__.py branches ≥ 90 % | 89.7 % | no |

bank/ total: lines 91.1 %, branches 83.3 % (reported, not a gate).

## Evals (SPEC §19.3) — tests/evals/harness.py, MOCK (scripted models, keyword-heuristic judge)

- Injections (20 variants + 2 controls, buyer tool loop against the real bank API with a fake escrow lock): money-level block 20/20; outcome as expected (refusal code, or the agreed offer paid to the registered wallet) 20/20; honest controls paid 2/2.
- Agent behaviour (scripted MOCK buyer — a property of the script, not of a model): it followed the injected text in 2/15 runs; the compromised model tries every unsafe call by construction and the tool layer rejected all of them.
- Injected delivered items paid anyway: 0/3 item variants (judge quality; capped at the per-item price inside the escrowed amount, never a new payee).
- Judge on 40 labelled items: precision 0.62, recall 0.42, valid JSON 100 %, injection items flagged: no.
- Explanations: cited sources ⊆ evidence 64/64; client texts without a non-public reason or forbidden word 32/32; AI texts labelled 29/29.

## Model cost

Scenario runs: 48 model calls, 0 input / 0 output tokens, **$0.0000** — estimate at list prices (`bank/timing.py` PRICES); Bedrock bills separately. Mode: MOCK (scripted replies).

## Reproduce

```bash
python -m scripts.scenarios --runs 3 --target fork
pytest --cov=bank --cov-branch --cov-report=json:reports/coverage.json
python -m scripts.report --target fork
```

Stack: minted 400.00 EURC via the token's master minter (fork only); tCZK `0xDd96E0cd15EBA8201528274D03066aAEc97EafBB`; bank signers relayer `0x44b6E7980A9eDa35c730CDc190C480d61dEfD1bD`, admin `0x30c48ec196cFD47088A6Cdb124c4328799ce2B42`, operator `0x5DcBa4520BAd10283f42d6774Ed4403aC5Ec1F2b`, alfa `0x5408849294332AC30B0EcFC700f971825c6D23fE`.
