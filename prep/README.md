# prep/ — pre-hackathon work (disclosed)

Written on 2026-10-18, before the `hackathon-start` tag, as learning spikes to de-risk the build.
Disclosed to the organisers in advance. It is reference material, not the product; judged work
is everything after `hackathon-start`.

| File | What it proved |
|---|---|
| `spikes/seller_x402.py`, `spikes/buyer_x402.py` | EURC payment through the public x402 facilitator on Base Sepolia (3 real payments) |
| `spikes/escrow_spike.py` | Coinbase AuthCaptureEscrow from Python: contracts linked; `authorize` valid in simulation; `run` does lock → partial capture → void |
| `contracts/DepositToken.sol`, `spikes/deposit_token_test.py` | Demo tokenized deposit tCZK: allowlist, bank mint, EIP-3009; an x402 SDK payload settles on it (in-memory chain) |
| `spikes/escrow_spike.py run EURC` · `run TCZK` | Live on Base Sepolia: lock → partial capture → void, for EURC and tCZK (tx links in `docs/REFERENCE.md` §8a) |
| `spikes/escrow_recover.py` | Recovers PaymentInfo from `PaymentAuthorized` logs and voids the remainder (used once after a nonce failure) |
| `spikes/deploy_tczk.py` | Deployed tCZK `0x6a99…4B67` on Base Sepolia, allowlisted clients + escrow infra, minted to buyer (idempotent) |
| `spikes/tczk_x402_pay.py` | x402 SDK-signed tCZK payment settled on-chain by the bank |
| `spikes/balances.py`, `spikes/token_info.py`, `spikes/gen_wallets.py` | Helpers: balances, on-chain token metadata, throwaway testnet wallets |

`deposit_token_test.py` expects the contract at `contracts/DepositToken.sol` relative to the
working directory.
