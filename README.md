# Collabse — Agents that pay each other

**The FinTechers Hackathon 2026 · Prague · Track 2: Stablecoins and new financial infrastructure**

> 🚧 Built during the hackathon (19–20 October 2026). This README grows with the code.

Two AI agents from two companies negotiate a B2B deal and settle it on-chain — and the bank decides
whether the money may move. AWS decides how much an agent may spend. Coinbase gives a safe for the
money. Mastercard proves a human allowed it. **We check whether the seller earned the money and
whether the seller can be trusted.**

## What it does

1. **Mandate.** A human sets the agent's limits: per deal, per month, a confirmation threshold and a
   validity period. The mandate is versioned and hashed.
2. **Seller check.** Before any payment the bank screens the seller in public Czech registries
   (ARES, VAT payers, insolvency), the EU sanctions list and regulator datasets. An Apify Actor
   checks the seller's web footprint. Every signal comes with its source.
3. **Negotiation.** The buyer's agent negotiates with the seller's agent. The result is an offer with
   price, deadline, **acceptance criteria** taken from the human's task, and dispute terms.
4. **Escrow.** The bank locks the money in Coinbase's AuthCaptureEscrow on Base. The seller verifies the
   lock on-chain before delivering.
5. **Verification.** Code rules check every delivered item. An LLM judge only flags doubtful items;
   it never moves money.
6. **Settlement.** We pay per accepted item and return the rest: 18 of 20 accepted means 90% to the
   seller and 10% back to the buyer.
7. **Dispute.** The disputed part stays in escrow until a human decides. Every step has a deadline,
   and a timer applies the agreed default when someone stays silent.
8. **Journal and statement.** A hash-chained journal and a statement that reconciles with the chain.

**The agent never holds a key and has no "send to address" tool** — only `pay(deal_id)` for an agreed
deal. A prompt-injected agent cannot redirect money: the bank's code refuses.

## The bank's answer: a tokenized deposit

The same deal runs in two currencies:

- **EURC** (Circle's euro stablecoin). Paying a Czech VAT payer in a virtual asset raises a VAT
  liability risk for the buyer (§109 of the Czech VAT Act, in our reading).
- **tCZK**, the bank's tokenized deposit. It is a bank deposit in token form: only bank clients can
  hold it, every wallet is tied to an account, and the bank can freeze it or force a transfer.
  In strict mode only the bank and the escrow can move it, so its screening cannot be bypassed.

## Architecture

```
 Human (bank UI): mandate, confirmations, disputes
      │
      ▼
┌────────────────────────── bank service (FastAPI :8000) ──────────────────────────┐
│ mandate      versions, budget reservations, closed list of refusal codes          │
│ screening    ARES, VAT register, ISIR, EU sanctions, datasets, Apify Actor        │
│ facilitator  x402 /verify /settle /supported for EURC and tCZK + bank screening   │
│ escrow       operator of AuthCaptureEscrow: authorize / capture / void            │
│ verifier     rules + AI flags · disputes + deadline timer · hash-chained journal  │
│ explain      two-level explanations (client / compliance), AI-labelled            │
└───────────────────────────────────────────────────────────────────────────────────┘
      ▲ agent tools: find_seller, check_seller, negotiate, pay(deal_id), get_statement
      │
 buyer agent (Claude on Amazon Bedrock) ── negotiate ──► seller service (FastAPI :8001, x402)
                                                          └─► Apify: Google Maps Actor
 Base Sepolia: EURC, tCZK, AuthCaptureEscrow · Tatum: RPC + incoming-payment webhooks
```

## Tech stack

| Area | What we use |
| --- | --- |
| Language, API | Python 3.12, FastAPI |
| Payments | x402 (Python SDK 2.25) with the bank's own facilitator for EURC and tCZK |
| Escrow | Coinbase Commerce Payments AuthCaptureEscrow v1.1 on Base Sepolia |
| Bank token | Solidity, OpenZeppelin 5.7 `ERC3009` + `AccessControl` |
| AI | Claude on Amazon Bedrock: a larger model for negotiation, a smaller one for frequent steps |
| Web data | Apify: Google Maps Actor + our own `company-check` Actor with evals |
| Chain data | Tatum RPC and webhooks |
| Storage, UI | SQLite, Jinja2 + HTMX |

## How we use Apify

- The **seller's agent** fulfils the order with the Google Maps Actor (`compass/crawler-google-places`).
  No phone numbers, no reviews, and a capped number of places.
- Our own **`company-check` Actor** checks a seller's web footprint: the same IČO on the website,
  a Google Maps listing, and whether the business is permanently closed. It returns a score with
  sourced signals and has eight eval cases, including a website carrying a prompt injection.

## Repository layout

```
contracts/            tCZK token (Foundry)
bank/                 bank service
seller/               seller service
agents/               buyer and seller agents
actors/company-check/ Apify Actor
fixtures/             synthetic companies, recorded registry answers
scripts/              seed, demo, smoke test, scenario runner, report
tests/                unit tests and evals
docs/                 technical spec, task list, verified reference notes
prep/                 pre-hackathon spikes (disclosed, not judged)
```

## Run it

Testnet only (Base Sepolia). Full instructions will be added as the layers land.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; on macOS/Linux: .venv/bin/pip
cp .env.example .env                           # fill in testnet keys; never commit .env
```

Start order: the bank service first (its facilitator must be up), then the seller, then the agents.

## Built during the hackathon

- **Everything after the tag `hackathon-start` (19.10.2026, 12:00 Prague)** was built during the event.
- **Existed before:** `prep/` holds throwaway spikes that tested x402 payments, the Coinbase escrow cycle
  and a demo deposit token on testnet. `docs/` holds the technical spec and research notes. Both were disclosed
  to the organisers in advance.
- **AI assistants:** the code is written with Claude Code (Anthropic). The product's agents run on
  Claude via Amazon Bedrock.

## Honest limitations

- Testnet only. Demo companies are synthetic.
- Company checks are signals with sources, not full KYB.
- In this prototype our service holds testnet keys; in production keys belong to a bank or a
  licensed provider.
- The tCZK contract is an unaudited prototype.
- The VAT point is our reading of the law and is being confirmed with lawyers.

## Team Collabse

[@13Belka13](https://github.com/13Belka13) · [@ilya2108](https://github.com/ilya2108)
