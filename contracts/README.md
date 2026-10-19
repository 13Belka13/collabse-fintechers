# tCZK — the bank's tokenized deposit (SPEC §3)

`src/TCZK.sol`: `TCZK is ERC3009, AccessControl` on OpenZeppelin v5.7.0 (`draft-ERC3009.sol`), solc 0.8.31,
EVM `cancun`. ERC-20/EIP-712 name `"tCZK Deposit Token"`, version `"1"`, 2 decimals (haléře).

| Piece | What it does |
| --- | --- |
| Roles | `DEFAULT_ADMIN_ROLE`, `ISSUER_ROLE` (mint/burn), `COMPLIANCE_ROLE` (allowlist, freeze), `ENFORCER_ROLE` (forcedTransfer), `ROUTER_ROLE` (strict mode) |
| Allowlist | `allow(addr, accountRef)` / `disallow`; `accountRef = keccak256(salt ‖ account number)`, never the number |
| Freeze | `freeze` / `unfreeze`; no reason on chain (§6.7) |
| Money | `mint(to, amt, bankTxRef)`, `burn(from, amt, bankTxRef)` (burn is never checked: works for delisted clients), `forcedTransfer(from, to, amt, caseRef)` |
| EIP-3009 | OZ `(v,r,s)` functions + `bytes signature` overloads; `receiveWithAuthorization(..., bytes)` = `0x88b7ab63` (the escrow collector calls only this one) |
| `_update` | sender and receiver allowlisted and not frozen; **strict mode**: an ordinary transfer needs `ROUTER_ROLE` on `msg.sender`, else `TransferViaBankOnly(caller)`. Admin can switch it off with `setStrictMode(false)` (§3.4 fallback) |

ROUTER holders: the bank's x402 relayer (it also needs the role for verify's `eth_call` simulation), the
ERC3009PaymentCollector, `escrow.getTokenStore(operator)` and the OperatorRefundCollector.

## Setup and tests

```bash
git submodule update --init --recursive      # forge-std v1.11.0, openzeppelin-contracts v5.7.0
cd contracts
forge test                                   # 24 unit tests + 3 fork tests (fork needs network)
forge test --match-contract TCZKEscrowFork   # authorize → capture → void via the real collector/escrow
```

The fork test pins Base Sepolia block 48 319 000 (`FORK_URL` overrides the public RPC). It uses a fresh operator,
so the token store does not exist yet. The test shows that the store must be allowlisted and hold ROUTER
**before** the first `authorize`; without ROUTER, capture reverts and the money stays locked.

Python side (`bank/tczk.py`, needs no Foundry: ABI and bytecode are committed in `artifacts/TCZK.json`):

```bash
python -m pytest tests/unit/test_tczk.py                         # deploy + helpers on the in-memory chain
python -m pytest tests/unit/test_facilitator_chain.py            # SDK-signed tCZK via the bank facilitator (anvil)
FORK=1 python -m pytest tests/unit/test_facilitator_chain.py     # + EURC on an anvil fork of Base Sepolia
python -m bank.tczk artifact                                     # refresh the artifact after changing TCZK.sol
```

## Deployed on Base Sepolia (19.10.2026)

`TCZK = 0x18245366edA63Ca583c11B27d1134F3d233A115F` — [explorer](https://sepolia.basescan.org/address/0x18245366edA63Ca583c11B27d1134F3d233A115F),
**source verified** on [Blockscout](https://base-sepolia.blockscout.com/address/0x18245366edA63Ca583c11B27d1134F3d233A115F?tab=contract) (`forge verify-contract`, solc 0.8.31, cancun, optimizer 200),
deployed by the bank wallet `0xD23D…de48` with `python -m bank.tczk deploy` (block 48 325 135), then
`python -m bank.tczk setup <address>`: 7 roles (ISSUER/COMPLIANCE/ENFORCER for the bank, ROUTER for the relayer,
collector, operator token store and refund collector), 7 allowlisted wallets (treasury, Alfa, Beta, Gama, Delta,
collector, token store), 1000.00 Kč minted to Alfa; `strictMode = true`. About 3.0 M gas in 16 txs.
The first setup tx reverted out of gas: its gas was estimated by a node that had not seen the new contract yet
(~23 k), so setup now waits for the code and uses explicit gas, and is idempotent (re-run to resume).
Live proof: N10 direct transfer reverted `TransferViaBankOnly`, N11 replayed signature reverted
`ERC3009UsedAuthorization` (`reports/scenarios-sepolia-n10n11.json`).

## How to deploy to Base Sepolia

Deployer = the bank admin wallet (`BANK_ADMIN_PK`; in SPEC §16 it is also the relayer `0xD23D…de48`).
It becomes admin, issuer, compliance and enforcer, and by default the ROUTER relayer. Cost ≈ 4.0 M gas ≈
0.00005 ETH at 0.011 gwei (measured on the fork).

```bash
cd contracts
cast wallet import deployer --interactive    # paste BANK_ADMIN_PK once; stored encrypted in ~/.foundry/keystores
OPERATOR_ADDR=0x0469ceC178789072bc61435835D0BB38214D4e53 \
forge script script/Deploy.s.sol:Deploy --rpc-url https://sepolia.base.org --account deployer \
  --broadcast --verify --verifier blockscout --verifier-url https://base-sepolia.blockscout.com/api/
```

Optional env: `RELAYER_ADDR` (if the relayer is not the deployer), `ACCOUNT_REF_SALT` (default `collabse-demo`;
the same value must be in `.env` for `bank.tczk.account_ref`), `MINT_ALFA` (default `100000` = 1000.00 Kč).
The script allowlists the treasury, every wallet in `fixtures/companies.json`, the collector and the operator's
token store. It grants the ROUTER roles and mints to Alfa. Then:

1. Put the printed address into `.env`: `TCZK=0x…` and `TCZK_NAME="tCZK Deposit Token"` (wrong name = every signature fails).
2. `python -m bank.tczk info` → `strict: true`, collector and token store `allowed` and `router` all `true`.
3. Keep the record for the judges: `git add -f contracts/broadcast/Deploy.s.sol/84532/run-latest.json`.

**Fallback without Foundry:** `python -m bank.tczk deploy`. It does the same steps from the committed artifact,
using `BANK_ADMIN_PK` and `RPC_URL` from `.env`, and prints `TCZK=…`.

Both paths were run on an anvil fork of Base Sepolia (`anvil --fork-url https://sepolia.base.org`). A direct
`transfer` by a client was a real reverted tx with `TransferViaBankOnly`.

## Stage fallback: the prep token

`TCZK=0x6a99C9e95DD220172d07D5f730f2e82B466D4B67`, `TCZK_NAME="Demo Bank tCZK"` (pre-hackathon `prep/` token,
disclosed; already allowlists the buyer, the seller, the collector and the operator's token store; the buyer holds 845.00).
`bank.tczk` and the facilitator detect it by the name. The prep token has no roles, no on-chain freeze and no
strict mode, so only the bank-side freeze (`frozen_wallets`) applies.
