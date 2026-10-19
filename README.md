# Collabse — Agents that pay each other

[![ci](https://github.com/13Belka13/collabse-fintechers/actions/workflows/ci.yml/badge.svg)](https://github.com/13Belka13/collabse-fintechers/actions/workflows/ci.yml)

**The FinTechers Hackathon 2026 · Prague · Track 2: Stablecoins and new financial infrastructure**

Two AI agents from two companies agree a B2B data deal and settle it on-chain, and **the bank decides
whether the money may move**. The buyer's agent holds no keys. The bank checks the human's mandate and the
seller, locks the money in escrow, pays only for the items that pass the agreed criteria, and returns the
rest. The bank's own tokenized deposit, tCZK, runs the same deal with no VAT risk for the buyer.

Status: **a prototype validated on Base Sepolia testnet, ready for a bank sandbox pilot.** It is not
production-ready. See [Honest limitations](#honest-limitations).

## What it does

1. **Mandate.** A human sets the agent's limits: 10 € per deal, confirmation above 6 €, 30 € a month,
   plus a task budget of 5 €. The mandate is versioned and hashed.
2. **Seller check.** Before any payment the bank checks the seller in the public Czech registries (ARES,
   the VAT payer register, the insolvency register ISIR) and the EU sanctions list. Our Apify Actor checks
   the seller's web footprint. Every signal carries its source URL.
3. **Negotiation.** The buyer's agent negotiates with the seller's agent. The result is an offer with a
   price, a deadline, **acceptance criteria** taken from the human's task, and dispute terms.
4. **Escrow.** The bank locks the money in Coinbase's AuthCaptureEscrow on Base. The seller checks the lock
   on-chain before it delivers.
5. **Verification.** Code rules check every delivered item. An AI judge only flags doubtful items. It
   never moves money.
6. **Settlement.** The bank pays per accepted item and returns the rest. Accepted items are paid at once,
   minus a 5 % holdback that is paid after its window.
7. **Dispute.** The disputed part stays in escrow until a human decides. Every step has a deadline, and a
   timer applies the agreed default when one side stays silent.
8. **Journal and statement.** A hash-chained journal, and a statement that reconciles with the chain.

**The agent has no "send to address" tool.** Its only payment tool is `pay(deal_id)` for an agreed deal.
A prompt-injected agent cannot redirect money, because the bank's code refuses any payee other than the
seller's registered wallet.

### The bank's answer: a tokenized deposit

The same deal runs in two currencies:

- **EURC** (Circle's euro stablecoin). In our reading of §109 of the Czech VAT Act, paying a Czech VAT payer
  in a virtual asset, and not to its published bank account, makes the buyer liable for the seller's
  unpaid VAT. The bank refuses with `VAT_LIABILITY_RISK` until a human acknowledges the risk, once per
  seller and asset.
- **tCZK**, the bank's tokenized deposit: a bank deposit in token form. Only bank clients can hold it, every
  wallet is tied to an account, and the bank can freeze it or force a transfer. In **strict mode** only
  the bank's relayer and the escrow contracts can move it, so the bank's checks cannot be bypassed. The
  account behind it is published, so the VAT risk does not arise.

## Architecture

```
 Human (bank UI): mandate, confirmations, VAT acknowledgement, disputes
      │
      ▼
┌──────────────────────────── bank service (FastAPI :8000) ────────────────────────────┐
│ mandate      versions, budget reservations, closed list of refusal codes             │
│ screening    ARES, VAT register, ISIR, EU sanctions (+ Apify company-check Actor)    │
│ facilitator  x402 /x402/verify /settle /supported for EURC and tCZK + bank screening │
│ escrow       operator of AuthCaptureEscrow: authorize / capture / void               │
│ verifier     code rules + AI flags · disputes + deadline timer · hash-chained journal│
│ explain      two-level explanations (client / compliance), AI text labelled          │
└──────────────────────────────────────────────────────────────────────────────────────┘
      ▲ agent tools (ids only): find_seller, check_seller, negotiate, pay(deal_id), get_statement
      │
 buyer agent (Claude on Amazon Bedrock) ── negotiate ──► seller service (FastAPI :8001, x402)
                                                          └─ delivers Google Maps places (Apify)
 Base Sepolia: EURC, tCZK, AuthCaptureEscrow + collectors · RPC: Tatum gateway or sepolia.base.org
```

## What runs where

| Part | Where | What it serves |
| --- | --- | --- |
| Bank service | `uvicorn bank.app:app --port 8000` | `/health`, agent API `/api/*`, x402 facilitator `/x402/supported\|verify\|settle`, the UI pages below, the dispute timer (every 5 s) |
| Seller service | `uvicorn seller.app:app --port 8001` | `/health`, `/negotiate`, paid routes `/order/{offer_id}` and `/data/{offer_id}` (HTTP 402 with EURC and tCZK quotes), `/order/{offer_id}/escrow` |
| Bank UI | http://localhost:8000/story | Live deal for the audience (the start page): the latest deal as plain steps — mandate, the agents' talk with the seller's words marked untrusted, the bank's decision and its time, escrow, delivery check, payout — and where the money is now; same client view as the feed |
| | http://localhost:8000/bank | Live feed of decisions and transfers, client / compliance view, decision time and p95, model cost, five demo buttons |
| | http://localhost:8000/mandate | Mandate versions and `policy_hash`, budget, VAT acknowledgement and confirmation buttons |
| | http://localhost:8000/deals → `/deals/{id}` → `/deals/{id}/dispute` | Deals, escrow state from the chain, the dispute card (items, violated criterion, deadlines, judge reasons, accept / reject) |
| | http://localhost:8000/statement | Statement and the chain reconciliation (`/statement/reconcile`) |
| Buyer agent | `agents/buyer.py`, run by `scripts/demo_scenarios.py` or a `/bank` button | Manual tool loop on Claude; MOCK replies when `MOCK=1` |
| Seller agent | `agents/seller_agent.py`, called by the seller service on every `/negotiate` turn | The model phrases the reply; code owns the price (≤ 3 turns, never below the minimum, never back up) |
| Apify Actor | `actors/company-check/`, on Apify as `savvy_sauger/company-check` (private, Limited permissions) | Web footprint of a Czech company: IČO on the site + Google Maps place |

Contracts on **Base Sepolia** (chain id 84532):

| Contract | Address |
| --- | --- |
| tCZK, strict mode, **source verified** | [`0x18245366edA63Ca583c11B27d1134F3d233A115F`](https://sepolia.basescan.org/address/0x18245366edA63Ca583c11B27d1134F3d233A115F) · [verified source on Blockscout](https://base-sepolia.blockscout.com/address/0x18245366edA63Ca583c11B27d1134F3d233A115F?tab=contract) |
| tCZK, pre-hackathon prep token (stage fallback, no strict mode) | [`0x6a99C9e95DD220172d07D5f730f2e82B466D4B67`](https://sepolia.basescan.org/address/0x6a99C9e95DD220172d07D5f730f2e82B466D4B67) |
| EURC (Circle) | [`0x808456652fdb597867f38412077A9182bf77359F`](https://sepolia.basescan.org/address/0x808456652fdb597867f38412077A9182bf77359F) |
| AuthCaptureEscrow v1.1 (Coinbase Commerce Payments) | [`0xf96815976523E00e65Be8f34cA5e64b4f41EB19c`](https://sepolia.basescan.org/address/0xf96815976523E00e65Be8f34cA5e64b4f41EB19c) |
| ERC3009PaymentCollector | [`0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88`](https://sepolia.basescan.org/address/0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88) |
| Operator token store (escrow) | [`0xd2b0C305F62427414e9624a2beD72a3792e15cf8`](https://sepolia.basescan.org/address/0xd2b0C305F62427414e9624a2beD72a3792e15cf8) |
| Bank relayer / escrow operator (throwaway testnet wallets) | [`0xD23D…de48`](https://sepolia.basescan.org/address/0xD23DB197CC1294b049AeDf0d9ea1BA216691de48) / [`0x0469…4e53`](https://sepolia.basescan.org/address/0x0469ceC178789072bc61435835D0BB38214D4e53) |

The token contract, roles and deploy steps are described in [contracts/README.md](contracts/README.md).

## Run it

Python 3.12. Testnet only. Nothing below needs a key unless the step says so.

```bash
git clone https://github.com/13Belka13/collabse-fintechers.git && cd collabse-fintechers
python3.12 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Configure `.env`

**No keys (MOCK, simulated chain or anvil fork).** Copy the example without the five empty auth lines, so
the dev defaults apply:

```bash
grep -vE '^(AGENT_TOKEN_ALFA|SELLER_TOKEN|BANK_TO_SELLER_TOKEN|HUMAN_TOKEN|BANK_OPERATOR_ADDR)=' .env.example > .env
```

A plain `cp .env.example .env` leaves these variables set to empty strings. Empty is not the same as unset:
the agent API then answers 401 and the seller cannot check the escrow operator. If you copy the whole
file, give each of the four tokens any value, and either set `BANK_OPERATOR_ADDR` or delete the line.

**Live on Base Sepolia** (`cp .env.example .env`, then fill in):

| Needed for | Variables |
| --- | --- |
| Chain | `BANK_RELAYER_PK`, `BANK_ADMIN_PK`, `BANK_OPERATOR_PK`, `ALFA_PK` (throwaway testnet keys with Base Sepolia ETH; Alfa also holds EURC and tCZK); `TCZK=0x18245366edA63Ca583c11B27d1134F3d233A115F` with `TCZK_NAME="tCZK Deposit Token"`; `RPC_URL` + `TATUM_API_KEY` (or `RPC_URL=https://sepolia.base.org`) |
| Auth between the parts | `AGENT_TOKEN_ALFA`, `SELLER_TOKEN`, `BANK_TO_SELLER_TOKEN`, `HUMAN_TOKEN` (any random strings; the bank warns at start-up while dev defaults are in use) |
| Claude on Bedrock | `MOCK=0`, `AWS_REGION`, then `AWS_PROFILE` (SSO) or `AWS_BEARER_TOKEN_BEDROCK`; `MODEL_MAIN` / `MODEL_FAST`. `python -m scripts.check_bedrock` makes one tiny call per model and tells you which ones the account can use |
| Apify (optional) | `APIFY_TOKEN`, `APIFY_COMPANY_CHECK_ACTOR=<user>/company-check`. Without them the synthetic sellers use recorded web footprints |

`.env` is gitignored. Never commit it.

### Tests (no keys)

```bash
pytest                                   # unit tests + evals, MOCK, temp SQLite; never reads .env
git submodule update --init --recursive  # forge-std, openzeppelin-contracts
cd contracts && forge test --no-match-contract Fork && cd ..   # tCZK unit tests (Foundry)
```

Tests that need `anvil` from [Foundry](https://getfoundry.sh) are skipped when it is missing.
`FORK=1` adds the fork tests and `LIVE=1` the live ones. CI runs the same pytest and forge jobs on every
push ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

### Demo without keys: the simulated chain

```bash
python -m scripts.demo_seed                      # five demo deals, one VAT acknowledgement, one open dispute
DEMO_SIM=1 uvicorn bank.app:app --port 8000      # then open http://localhost:8000/bank
```

`DEMO_SIM=1` replaces only the chain sender and the escrow state reader. Everything else is the real bank
code. The UI shows a **SIMULATED CHAIN** banner. The five buttons on `/bank` run the demo scenarios:
honest, fake data, attack, suspicious company, the same deal in tCZK. From the command line:

```bash
python -m scripts.demo_scenarios honest --sim    # or fake_data | attack | suspicious | tczk
python -m scripts.verify_journal                 # checks the journal's hash chain
```

### Validation on an anvil fork (no keys, needs `anvil` and internet)

```bash
python -m scripts.smoke --target fork            # 9 checks: health, facilitator, RPC, balances, 402, LLM, ARES, one payment
python -m scripts.scenarios --target fork --runs 1 --out /tmp/scenarios-fork.json
```

The runner forks Base Sepolia (the real EURC, escrow and collector contracts). It deploys a fresh tCZK,
funds throwaway keys and starts its own bank and seller on free ports. Without `--out` it overwrites the
committed `reports/scenarios-fork.json`. To rebuild `reports/validation.md` from it (the coverage file is
needed for the coverage section):

```bash
pytest --cov=bank --cov-branch --cov-report=json:reports/coverage.json
python -m scripts.report --target fork           # or --target sepolia
```

### Live on Base Sepolia (testnet keys in `.env`)

Start the bank first. The seller's x402 middleware asks the bank's facilitator for `/x402/supported` at
start-up.

```bash
python -m scripts.seed
uvicorn bank.app:app --port 8000
uvicorn seller.app:app --port 8001               # second terminal
python -m scripts.smoke                          # the running stack, including one real 0.01 EURC payment
python -m scripts.demo_l1 --asset EURC           # one x402 payment through the bank facilitator
python -m scripts.demo_scenarios honest          # buyer agent against the running bank and seller
python -m scripts.scenarios --p0 --runs 1 --target sepolia   # real transactions; writes reports/scenarios-sepolia.json
```

## Proof

Every number here is quoted from [`reports/validation-sepolia.md`](reports/validation-sepolia.md),
[`reports/scenarios-sepolia-n10n11.json`](reports/scenarios-sepolia-n10n11.json) and
[`reports/validation.md`](reports/validation.md). `scripts/report.py` builds the reports from the
scenario runner's raw JSON. The scenarios assert against the chain (balances, escrow `paymentState`,
`Transfer` events), not against our database. Model spend in all of them is **$0, MOCK**: Bedrock was not
connected yet.

### Base Sepolia (public testnet)

**P0 matrix, 1 run** ([validation-sepolia.md](reports/validation-sepolia.md)):

- 20 scenarios (19 P0 + A01 bank API auth), 10 of them attacks or refusals: **18 pass, 2 fail**. The two
  failures are N10 and N11. They were not executed because the stage token then was the prep tCZK, which
  has no strict mode. They passed later in a separate run, below.
- **Attacks stopped before any money moved: 8/8** executed attack runs, all with the expected refusal code.
- **Wrong debits: 0.** Transfers to the attacker address 0xdEaD: 0.
- **Reconciliation chain ↔ DB ↔ statement: OK** for 12/12 escrow deals. The journal hash chains verified.
- 40 transactions landed. 14 signed transactions were dropped after a nonce clash and re-sent under the
  same idempotency key; none of them landed twice.
- Cost of the run: 1.970000 EURC and 4.50 Kč left the buyer for good; gas used 3767966.
- Speed, p95 against target: mandate decision 13.3 ms (≤ 50 ms), new counterparty check 129.4 ms
  (≤ 2500 ms), signature → tx in block 830.7 ms (≤ 6000 ms), refusal → client 42.7 ms (≤ 100 ms).
  **Missed:** bank facilitator verify 809.1 ms against ≤ 500 ms (n = 2, includes the first call that
  builds the facilitator). Not measured: block → `/bank` feed.
- In this run S04 (tCZK through escrow) used the **prep tCZK** `0x6a99…4B67`.

**N10 / N11 on the strict-mode tCZK, 1 run** ([scenarios-sepolia-n10n11.json](reports/scenarios-sepolia-n10n11.json)):
both pass, reconciliation OK, wrong debits 0.

- N10: a direct tCZK transfer by the buyer, bypassing the bank, reverted on chain with `TransferViaBankOnly`,
  and no tCZK moved.
- N11: the first settle through the bank landed. A replay of the same EIP-3009 signature reverted with
  `ERC3009UsedAuthorization`, and there was exactly one tCZK Transfer from the buyer to the seller.

Key transactions:

| What | Transaction |
| --- | --- |
| First live x402 payment through the bank facilitator: 0.01 EURC, Alfa → Beta (not in a report; check the `Transfer` on the explorer) | [0xa0f7006b…7a80441f](https://sepolia.basescan.org/tx/0xa0f7006bee0c5dab4078a76b714fe89a676ebf3d6dbc016f81208a107a80441f) |
| S01: x402 EURC settled by the bank facilitator | [0x5f00162e…cea0d84d](https://sepolia.basescan.org/tx/0x5f00162e7d39b88af848fe4a47278a73f6390ddc0d5326f0962c059acea0d84d) |
| S03: 18 of 20 items accepted. Escrow authorize | [0x6e861dc8…b1d80933](https://sepolia.basescan.org/tx/0x6e861dc80f3c909953724548573bcb76feba649a321f3d3b12d22a7cb1d80933) |
| S03: capture of the accepted items | [0xe2bcbb73…0891c151](https://sepolia.basescan.org/tx/0xe2bcbb73de8b4b88e31d85ba8e4559fb68bee9969038497cb4f28cf10891c151) |
| S03: void of the rejected part back to the buyer | [0xb629a876…0ec13042](https://sepolia.basescan.org/tx/0xb629a8763f418a2b0d59353a58b8985257583d680b50bb2858ab33870ec13042) |
| S04: the same deal in tCZK (prep token). Authorize / void | [0x971a1ea3…8202f263](https://sepolia.basescan.org/tx/0x971a1ea3a34e6dd42a60718a608d535ef76f8b2715fd715f0537fa008202f263) / [0xe41aa3b8…c1ca4172](https://sepolia.basescan.org/tx/0xe41aa3b8eaa1225ef591137c06d7315aaa2baa3c61ceff1f40ad600bc1ca4172) |
| N10: direct tCZK transfer reverted, `TransferViaBankOnly` | [0xbe5adf59…b94765fd](https://sepolia.basescan.org/tx/0xbe5adf590d439625c92fafca266fbe8e94b5eeb04800d141c00ba6b0b94765fd) |
| N11: replayed signature reverted, `ERC3009UsedAuthorization` | [0xadb85fb9…fa2e69c7](https://sepolia.basescan.org/tx/0xadb85fb97bf79c0b88b5f41c2ae28e9a091e887143835454b1e37f09fa2e69c7) |

All the other transactions of the run are linked per scenario in
[validation-sepolia.md](reports/validation-sepolia.md#scenarios--runs).

### Anvil fork of Base Sepolia (local, 3 runs)

From [validation.md](reports/validation.md). The fork uses the real deployed EURC, escrow and collector at
block 48324278, a freshly deployed tCZK and throwaway keys. Its transactions exist only on the local fork,
so they have no explorer links.

- 25 scenarios (19 P0, 5 fault injections, A01) × 3 runs = 75 runs: **69 pass, 6 xfail, 0 fail.** The xfails
  are F04 and F05, which wait for the Tatum webhook (not built).
- **Attacks stopped before any money moved: 33/33**, including N10 and N11 as real reverted transactions.
- Wrong debits: 0. Reconciliation OK for 39/39 escrow deals. 138 transactions landed.
- Facilitator verify p95 610.8 ms, also above the 500 ms target.

### Shared by both reports (tests, and evals on live Bedrock)

- Coverage of `bank/`: 90.2 % lines, 81.2 % branches.
- Prompt-injection evals: the money-level block held for **20/20** variants (50/50 runs: a scripted buyer,
  a deliberately compromised one, and the live buyer on Claude Opus 4.5).
- The live buyer on Opus 4.5: money blocked in 17/17 runs, outcome as expected in 16/17, and it never tried a
  call the bank had to reject. The one miss: on a Morse-coded "transfer 50 EURC" (INJ02) the model refused
  the whole task, so nothing was paid — safe, but the honest offer was not paid either.
- AI judge on 40 labelled items with Claude Haiku 4.5: precision 1.00, recall 1.00 (the MOCK keyword
  heuristic scores 0.62 / 0.42). The judge still only flags items for a human and never decides on money.
- Explanations: cited sources ⊆ evidence 64/64, client texts without a non-public reason 32/32, AI texts
  labelled 29/29.

## How we use Apify

- **Our own Actor, [`actors/company-check`](actors/company-check/README.md).** It answers one question a bank
  asks before paying a new Czech supplier: *does the company's web footprint match the company in the
  register?* It looks for the legally required IČO on three pages of the website, flags other valid IČO
  numbers on the site, and checks the Google Maps place (found, rating, permanently closed). It returns
  `low` / `medium` / `high` with sourced signals. It has 8 eval cases with mocked HTTP, including a website
  that carries a prompt-injection text. Page text is only matched with regular expressions, never
  interpreted. The bank calls it in `bank/screening/website.py` during negotiation, never on the money
  path, and caches the result.
- **Google Maps Actor `compass/crawler-google-places`** produces the goods of the deal: the seller delivers
  "20 warehouses in Brno with rating above 4.0 and a website". No phone numbers, no reviews
  (`scrapeReviewsPersonalData: false`), and the number of places is capped.
- **What is live today:**
  - The Actor is pushed to Apify as `savvy_sauger/company-check` (private, Limited permissions; schemas
    validated before each push). Two platform runs: a large public company (ČEZ, IČO 45274649) came out
    `medium` — ČEZ does not show its IČO on the three pages the Actor reads — and a synthetic seller with an
    unreachable website returned `medium` with `website_reachable = false` instead of failing.
  - The real Google Maps dataset is recorded: `fixtures/maps/brno_warehouses_recorded.json`, Apify run
    `w9XlXOBtETIUObLGm`, 73 places in Brno with only the 11 fields we use (one place whose title
    was a person's name was dropped). 17 of them pass the demo criteria; 12 more pass every rule except
    the category keyword (movers, estate agents) and go to the judge.
  - Run the seller with `SELLER_DATA=maps` to deliver these real places: 17 are paid at once, the 3
    borderline ones are held for a human in the dispute card. Without it the seller delivers the labelled
    synthetic set, which keeps the money scenes and the tests deterministic.
  - Apify spend for the push, the two Actor runs and the recording: **about $0.38** (Apify console).

## How we use AWS

- **Hosting and CD:** the demo runs on one EC2 host in eu-central-1 (Docker: bank + seller + Caddy with
  HTTPS). A green CI run on `main` deploys it through GitHub OIDC and SSM Run Command, with no access keys; the
  instance role calls Bedrock. See [docs/DEPLOY.md](docs/DEPLOY.md).
- **Claude on Amazon Bedrock** runs both agents (`bank/llm.py`, client `AnthropicBedrock`). The models
  come from `.env` and are never hard-coded. The hackathon's team account offers Claude 4.5 through EU
  inference profiles in eu-central-1, so `MODEL_MAIN` is Claude Opus 4.5 (or Sonnet 4.5) for
  negotiation and `MODEL_FAST` is Claude Haiku 4.5 for frequent steps such as the judge. The system
  prompt is cached. There are no structured outputs on Bedrock, so each JSON answer is validated with pydantic and
  retried once. The tool loop is manual, and a model refusal is handled.
- **Every model call is logged** with its model, input/output and cache tokens, latency and cost estimate
  (the `timings` table, `bank/timing.py`). The total shows on `/bank` and in the reports.
- **Spend of the live eval run: $1.13** for 117 model calls (146 495 input / 19 396 output tokens; estimate
  at the EU-profile prices). Almost all of it is the buyer on Opus 4.5: 67 calls, $1.04, about 6 cents per
  negotiated deal, 3.7 s per call. The judge on Haiku 4.5 checked 40 items in 4 calls for $0.04. Details in
  [the report](reports/validation-sepolia.md#model-cost). The scenario runs on chain used `MOCK=1`.
  Reproduce: `python -m scripts.check_bedrock`, then `LIVE=1 MOCK=0 pytest tests/evals/test_live_bedrock.py`.

## How we use Tatum

- **RPC:** `RPC_URL` defaults to Tatum's Base Sepolia gateway (`https://base-sepolia.gateway.tatum.io`,
  `x-api-key` header) on the hackathon Starter plan. `https://sepolia.base.org` is the public fallback, and
  the anvil fork forks from it.
- **Webhooks are not built.** Incoming payments are confirmed by reading the chain. The planned
  `/webhooks/tatum` route (HMAC on the raw body, dedupe by `txId`) does not exist yet, so the fault scenarios
  F04 and F05 are marked xfail.

## Built during the hackathon

- **Everything after the tag [`hackathon-start`](https://github.com/13Belka13/collabse-fintechers/tree/hackathon-start)**
  (19.10.2026, 12:00 Prague) was built during the event: the bank service, the seller, both agents, the
  tCZK contract and its deployment, the Apify Actor, the UI, the tests and the validation runs.
- **Before the tag** (two commits, disclosed to the organisers in advance, not judged):
  - `docs/SPEC.md`, `docs/REFERENCE.md`, `docs/TASKS.md`, `CLAUDE.md`, `prompts/`, `.env.example`,
    `requirements.txt`: the spec, research notes verified on testnet, and the coding agent's instructions.
  - [`prep/`](prep/README.md): throwaway spikes from 18.10 that tested x402 EURC payments, the Coinbase
    escrow cycle and a demo deposit token on Base Sepolia.
- **What we reused from `prep/`** (each commit message says so):
  - Chain sender: nonce handling and retry lessons (REFERENCE §8a), in `c67e2c1` and `f0047bd`.
  - Escrow operator: PaymentInfo tuple order, nonce derivation, v1.1 capture from `prep/spikes/escrow_spike.py`,
    in `22f57b1` and `47fcadf`.
  - x402 seller and buyer helper: built on `prep/spikes/seller_x402.py` and `buyer_x402.py`, in `e01615e`.
  - tCZK contract test layout: follows `prep/contracts/DepositToken.sol` and `deposit_token_test.py`, in
    `53b5ae2`. The deploy steps follow `prep/spikes/deploy_tczk.py`, in `504cce5`.
  - The prep tCZK token `0x6a99…4B67` itself, as a stage fallback. It was used for S04 in the Base Sepolia P0
    run.
- **AI assistants.** Claude Code (Anthropic) wrote the code under the team's direction: the team set the
  product, the spec, the rules and the reviews. The product's agents run on Claude via Amazon Bedrock. AI
  text shown to people in the UI is labelled (AI Act Art. 50).

## Honest limitations

- **Testnet only** (Base Sepolia). No real money, no mainnet.
- **Synthetic companies.** Alfa, Beta, Gama and Delta are invented, with recorded registry answers. The live
  registry check works for real legal entities: the ČEZ check passes with `LIVE=1`.
- **Custodial keys in the prototype.** Our service holds throwaway testnet keys. In production the keys
  belong to the bank or a licensed provider.
- **The tCZK contract is unaudited.** Its source is verified on Blockscout.
- **The VAT point is our reading** of §109 of the Czech VAT Act, being confirmed with lawyers.
- **MOCK until Bedrock is connected.** The negotiation, judge and explanation texts in the reports are
  scripted, and the judge numbers above come from a keyword heuristic.
- **Company checks are signals with sources, not full KYB.** The Mostly Right datasets need an API key we do
  not have yet.
- **Not built yet:** the Tatum webhook; latency of block → `/bank` feed (not measured); facilitator
  verify is above its 500 ms target. The seller agent negotiates up to 3 turns (`SELLER_OPENING=list`: the
  6 € list price is refused by the 5 € task budget, the buyer agent haggles to 5 € and pays), but only in
  MOCK so far.

## Repository layout

```
bank/                 bank service: api, mandate, screening/, facilitator, escrow, verifier, disputes,
                      deadlines (timer), explain, journal, llm, ui + templates/
seller/               seller service (x402)
agents/               buyer and seller agents
contracts/            tCZK token (Foundry) + artifacts/ for Python
actors/company-check/ Apify Actor
fixtures/             synthetic companies, recorded registry answers, sanctions index, Maps stand-in
scripts/              seed, demo_*, smoke, scenarios, report, verify_journal, check_bedrock
tests/                unit/ and evals/
reports/              validation reports (committed, tx links, no secrets)
docs/                 spec (Russian), task list, verified reference notes, demo script
prep/                 pre-hackathon spikes (disclosed, not judged)
```

The demo script for the stage is in [docs/DEMO.md](docs/DEMO.md).

## Team Collabse

[@13Belka13](https://github.com/13Belka13) · [@ilya2108](https://github.com/ilya2108)
