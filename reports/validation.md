# Validation report — anvil fork of Base Sepolia (chain id 84532) at block 48324278

> **Network: anvil fork of Base Sepolia (local), not the public testnet.** The real deployed contracts (EURC,
> AuthCaptureEscrow, ERC-3009 collector) forked at the block above, a freshly deployed tCZK, throwaway keys.
> The transactions are real EVM transactions on the local fork; their hashes exist only there, so they have
> **no explorer links**. The same matrix on Base Sepolia is a separate run (`--target sepolia`, keys from .env).

Generated 2026-10-19 15:37 from `reports/scenarios-fork.json` at commit `42a83de` by `scripts/report.py`. A prototype validated on a fork of the testnet, ready for a bank sandbox pilot — not production-ready.

## Summary

- **25 scenarios** (19 P0 from SPEC §19.2, 5 fault injections, 1 extra: A01 bank API auth), 11 of them attacks or refusals, **3 runs each** = 75 scenario runs: 69 pass, 6 xfail (pending fix named below), 0 fail.
- **Attacks stopped before any money moved: 33/33** executed attack runs (balances equal on chain, the bank's signer nonces unchanged, no tx for the deal; N10/N11 as real reverted txs); with the expected refusal code: 33/33.
- Transactions: 138 landed on chain; 42 signed transactions were dropped as dead after a nonce clash and re-sent under the same idempotency key (they never landed — see reconciliation).
- Cost of these runs: 6.540000 EURC and 21.00 Kč left the buyer for good (captures + direct x402 payments, Transfer events; amounts voided back are not counted); gas used by landed txs: 12783958.
- **Wrong debits: 0** — every buyer debit on chain since the first scenario is a lock or a payment a scenario expected. Transfers to the attacker address 0xdEaD: 0.
- **Reconciliation chain ↔ DB ↔ statement: OK** — 39/39 escrow deals (locked = captured + voided + still capturable, from Transfer events and paymentState; captured = journal = statement). Journal hash chains: verified.
- Injection evals: money-level block **20/20** variants (35/35 model runs). Judge (mock (keyword heuristic)): precision 0.62, recall 0.42 on 40 labelled items.
- Coverage of bank/: lines 90.2 %, branches 81.7 %.
- Model spend in the scenario runs: 60 calls, 0 in / 0 out tokens, $0.0000 (estimate at list prices, `bank/timing.py`; MOCK calls cost $0).

## Scenarios × runs

| ID | Scenario | Run 1 | Run 2 | Run 3 | Transactions (last run) |
|---|---|---|---|---|---|
| S01 | x402 EURC through the bank facilitator | PASS | PASS | PASS | x402_settle `0x7ab4e0bb…cd2671` |
| S02 | Escrow EURC, all accepted | PASS | PASS | PASS | authorize `0x7a5c7a65…76ab6d`, capture `0x0c5437e0…b4778f`, capture `0x81e5a2e8…24d79c` |
| S03 | Escrow EURC, 18 of 20 | PASS | PASS | PASS | authorize `0xb4204fd7…43a6a9`, capture `0x551d0e67…1f1533`, capture `0x57504380…90c811`, void `0xbd23fabf…2171fa` |
| S04 | Same deal in tCZK through escrow | PASS | PASS | PASS | authorize `0xc4acacea…72c719`, capture `0x7b9e57fd…849dbb`, capture `0x87402e1b…b87a39`, void `0xe3d9736d…5ed659` |
| S05 | Dispute decided for the seller | PASS | PASS | PASS | authorize `0xea599ff4…6d0e5c`, capture `0xbe427f61…8228e6`, capture `0x7ae9eea7…099c2b`, capture `0x80020399…09fd4d`, capture `0xd5c449fc…ad29e6` |
| N01 | Amount above the per-deal limit | PASS | PASS | PASS | — |
| N02 | Race: 10 parallel pay() with budget for 3 | PASS | PASS | PASS | authorize `0x23bcdc77…833a9b`, authorize `0x0f87bfdb…a718f2`, authorize `0x13ff1b9a…b3819a`, capture `0x49b2a854…51f439`, capture `0x71ac3a22…62a61d`, capture `0x052cf5e3…178c9b`, capture `0x737bdcfb…b677cf`, capture `0x2d2cb116…f6ab58`, capture `0xce04fc1b…f03b12` |
| N03 | payTo swapped in the 402 | PASS | PASS | PASS | — |
| N04 | Price swapped in the 402 | PASS | PASS | PASS | — |
| N05 | Injection in the seller's answer | PASS | PASS | PASS | — |
| N06 | Delta: IČO on site ≠ ARES | PASS | PASS | PASS | — |
| N07 | Garbage result → full void | PASS | PASS | PASS | authorize `0x1f4aec2a…c59b38`, void `0xa796931c…fa0fea` |
| N08 | VAT payer in EURC → acknowledged once | PASS | PASS | PASS | authorize `0x056baa63…1ea03b`, capture `0x4b97a495…d87ae8`, capture `0x9f3dea48…e66fef`, authorize `0xa260a5f5…36288a`, capture `0x90a1d1c7…ac8335`, capture `0xcd7c27ed…6634f0` |
| N09 | tCZK to a payee off the allowlist | PASS | PASS | PASS | — |
| N10 | Direct tCZK transfer bypassing the bank | PASS | PASS | PASS | attack_direct_transfer `0x42c26e39…6cb16c` |
| N11 | Replay of the same EIP-3009 signature | PASS | PASS | PASS | x402_settle `0x1ea00642…ad3ca1`, attack_replay `0xdfc567b9…eb04d0` |
| N12 | Unpaid request to a paid route | PASS | PASS | PASS | — |
| N13 | Buyer silent on flagged items → timer captures | PASS | PASS | PASS | authorize `0x85345a3b…1cddeb`, capture `0xd2b74c10…5519e2` |
| N14 | Seller silent after a rejection → timer voids | PASS | PASS | PASS | authorize `0xbc52d575…7daf4f`, capture `0x11b67ef2…7d22f7`, capture `0x058a79dc…8fb6cb`, void `0xdeb01e47…eacfd7` |
| A01 | Bank API auth: tokens, other company's mandate/deal | PASS | PASS | PASS | — |
| F01 | settle_fail_once → exactly one tx | PASS | PASS | PASS | x402_settle `0x87bcf0ce…827d5d` |
| F02 | registry_timeout → under review (fail-closed) | PASS | PASS | PASS | — |
| F03 | llm_bad_json → to a human, no money moves | PASS | PASS | PASS | authorize `0xf2e46977…cdeab7`, capture `0x994d7e00…ab2a59` |
| F04 | webhook_duplicate → counted once | XFAIL (D3) | XFAIL (D3) | XFAIL (D3) | — |
| F05 | webhook_bad_hmac → rejected | XFAIL (D3) | XFAIL (D3) | XFAIL (D3) | — |

### Pending fixes behind the xfails

| Defect | What | Scenarios |
|---|---|---|
| D3 | Tatum webhook /webhooks/tatum not built (L7): no HMAC check, no dedupe by txId | F04, F05 |

## Speed (SPEC §19.4, measured in the matrix runs)

Fork caveat: anvil mines on demand, so *tx in block* here is local and says nothing about Base's 2 s blocks; registry checks use the recorded fixtures of the synthetic companies.

The `decision` samples include N02, where 10 concurrent pay() calls queue on one SQLite write lock (BEGIN IMMEDIATE) by design; `facilitator_verify` includes the first call of each bank process, which builds the facilitator.

| Stage | Target p95 | n | p50 | p95 | Met |
|---|---|---|---|---|---|
| Mandate decision + cached check + journal (`decision`) | ≤ 50 ms | 144 | 0.5 ms | 31.9 ms | yes |
| Bank facilitator verify (with screening) (`facilitator_verify`) | ≤ 500 ms | 6 | 12.6 ms | 610.8 ms | no |
| New counterparty: ARES + VAT + ISIR + sanctions (`screening`) | ≤ 2500 ms | 119 | 22.0 ms | 123.9 ms | yes |
| Signature → tx in block (settle, authorize, capture, void) (`tx_in_block:*`) | ≤ 6000 ms | 139 | 641.4 ms | 752.1 ms | yes |
| Tx in block → row in the /bank feed (`bank_feed`) | ≤ 5000 ms | 0 | — | — | not measured |
| Refusal → message to the client (`refusal_to_client`) | ≤ 100 ms | 42 | 3.5 ms | 47.4 ms | yes |

## Coverage (SPEC §19.5)

| Target (SPEC §19.5) | Actual | Met |
|---|---|---|
| bank/ lines ≥ 80 % | 90.2 % | yes |
| bank/mandate.py branches ≥ 90 % | 92.4 % | yes |
| bank/verifier.py branches ≥ 90 % | 97.1 % | yes |
| bank/explain.py branches ≥ 90 % | 96.4 % | yes |
| bank/facilitator.py branches ≥ 90 % | 91.3 % | yes |
| bank/screening/__init__.py branches ≥ 90 % | 89.7 % | no |

bank/ total: lines 90.2 %, branches 81.7 % (reported, not a gate).

## Evals (SPEC §19.3) — tests/evals/harness.py, MOCK (scripted models, keyword-heuristic judge)

- Injections (20 variants + 2 controls, buyer tool loop against the real bank API with a fake escrow lock): money-level block 20/20; outcome as expected (refusal code, or the agreed offer paid to the registered wallet) 20/20; honest controls paid 2/2.
- Agent behaviour (scripted MOCK buyer — a property of the script, not of a model): it followed the injected text in 2/15 runs; the compromised model tries every unsafe call by construction and the tool layer rejected all of them.
- Injected delivered items paid anyway: 0/3 item variants (judge quality; capped at the per-item price inside the escrowed amount, never a new payee).
- Judge on 40 labelled items: precision 0.62, recall 0.42, valid JSON 100 %, injection items flagged: no.
- Explanations: cited sources ⊆ evidence 64/64; client texts without a non-public reason or forbidden word 32/32; AI texts labelled 29/29.

## Model cost

Scenario runs: 60 model calls, 0 input / 0 output tokens, **$0.0000** — estimate at list prices (`bank/timing.py` PRICES); Bedrock bills separately. Mode: MOCK (scripted replies).

## Reproduce

```bash
python -m scripts.scenarios --runs 3 --target fork
pytest --cov=bank --cov-branch --cov-report=json:reports/coverage.json
python -m scripts.report --target fork
```

Stack: minted 400.00 EURC via the token's master minter (fork only); tCZK `0xDd96E0cd15EBA8201528274D03066aAEc97EafBB`; bank signers relayer `0x44b6E7980A9eDa35c730CDc190C480d61dEfD1bD`, admin `0x30c48ec196cFD47088A6Cdb124c4328799ce2B42`, operator `0x5DcBa4520BAd10283f42d6774Ed4403aC5Ec1F2b`, alfa `0x5408849294332AC30B0EcFC700f971825c6D23fE`.
