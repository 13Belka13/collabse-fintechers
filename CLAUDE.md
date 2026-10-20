# CLAUDE.md — Agents that pay each other + bank token tCZK (The FinTechers Hackathon 2026, Track 2)

> **Internal — instructions for the coding agent** (Claude Code), not project documentation. The project is described in the [README](README.md).

You are the coding agent for a 24-hour hackathon build. Before writing code read, in this order:

1. `docs/SPEC.md` — **the master document** (Russian): the implementation spec (addresses,
   interfaces, API formats, order, done-criteria, pitfalls). If anything else disagrees with it, it wins —
   except the deal model (delivery, acceptance, quality rules, disputes), where `docs/MODEL_V2.md` (v2.6) wins.
2. `docs/REFERENCE.md` — facts verified live on 2026-10-18 (tx links) and the **lessons** in §8a.
3. `docs/TASKS.md` — the tick-list; one line per task, details live in SPEC.

## What we build (one paragraph)

Two AI agents (Claude on Amazon Bedrock) of two companies close a B2B deal on Base Sepolia. A human
sets a **mandate**; the agent holds **no keys** — the **bank service** (one FastAPI process: core
accounts, mandate, screening, x402 facilitator, escrow operator, verifier, disputes, journal) checks
everything and signs. The seller is checked against public registries (ARES, VAT payers, ISIR, EU
sanctions, hackathon datasets) and an **Apify actor**. Money is locked in **Coinbase
AuthCaptureEscrow**; the seller delivers **through the bank** with a manifest signed by its own key (Merkle
root), code predicates accept or reject each item (no LLM judge), the bank pays per accepted item by the
deal's quality rule **A < B < C** (fee to the bank's clearing address at capture) and returns the rest in
**one final void**; a rule-C dispute goes to an external decider drawn by drand. Paying a Czech VAT payer in EURC
raises `VAT_LIABILITY_RISK` (Czech VAT Act §109); the human acknowledges it once per (seller, asset) in
the first scene. Final scene: the same deal in EURC carries the "VAT risk acknowledged" mark, in the
bank's **tokenized deposit tCZK** (125,00 Kč = 5 € at the demo rate) it passes with no risk.

## Hard rules

- **Testnet only** (Base Sepolia, `eip155:84532`). Never mainnet, never real funds.
- **No secrets in code or git.** Keys only in `.env` (gitignored). Never print keys.
- **The LLM never moves money and never chooses a payee.** Agent tools take ids only
  (`pay(deal_id)`); money moves by code rules, human confirmations and executed rule-C decisions.
  No LLM judges deliveries.
- **Refusals are results, not exceptions:** closed enum (SPEC §6.3), each with a
  public/non-public flag; AML-suspicion reasons are never shown to the client (two-level explanations, §6.7).
- **AI-generated text shown to people is labelled** (AI Act Art. 50).
- **Data:** public business data and synthetic companies only. No personal data: never take
  `phone`/`phoneUnformatted` or reviews, disable `scrapeReviewsPersonalData`, cap
  `maxCrawledPlacesPerSearch`. Live IČO check only for legal entities (ARES `pravniForma` 112 s.r.o.,
  121 a.s.); anything else → "check only for legal entities" (a sole trader's ARES entry is a person's name).
- **Log tokens and cost of every model call** and show the total in the UI and README — AWS credit
  spend is part of the technical score.
- Python 3.12.
- **Lean code.** Plain functions, one SQLite, no abstractions "for later". Only what the demo
  shows or what protects money.
- **Every layer ends runnable**: check its done-line in SPEC §15, then commit (small,
  imperative messages). **No `Co-Authored-By` or other
  AI attribution trailers in commit messages or PR descriptions** — the team's decision; AI use
  is disclosed in the README instead.
- **`prep/` is pre-hackathon, disclosed** work: reuse proven techniques, never present it as new;
  say so in the commit message whenever something comes from `prep/`.

## Validation (SPEC §19)

- A money-moving or money-refusing function ships with its unit test in the same commit.
- Scenarios run on **real Base Sepolia transactions** and assert against the chain (balances, escrow
  state, `Transfer` events), not our DB. Negative scenarios prove "no money moved".
- No LLM and no slow external call on the money path: decisions are code + cache; counterparty checks
  are prefetched during negotiation; registries in parallel with 2 s timeout, fail-closed.
- Wrap each stage in `timed(stage, deal_id)`; targets (p95): decision ≤ 50 ms, facilitator verify
  ≤ 500 ms, tx in block ≤ 6 s, block → `/bank` feed ≤ 5 s.
- Numbers in the demo and in README come only from `reports/validation.md`. Never call it
  "production-ready": it is a prototype validated on testnet, ready for a bank sandbox pilot.

## Must-know pitfalls (details: SPEC §18 and REFERENCE §8a)

1. x402 client rejects EURC/tCZK by default → `allowed_assets` with atomic-unit caps.
2. One facilitator per (network, scheme) in a resource server → the **bank facilitator serves both
   EURC and tCZK**; `x402.org/facilitator` is only the EURC fallback.
3. Bank screening in **both** `on_before_verify` and `on_before_settle`; a refusal is HTTP 200
   `{"isValid": false, "invalidReason": ...}`.
4. Signer nonces: use `"pending"` + a local counter per key, one process, lock around settle;
   retry on `replacement transaction underpriced` / `nonce too low` with an idempotency key.
5. **Persist PaymentInfo before `authorize`.** Never branch on a read right after a write (RPC lag).
6. `eth_getLogs` on the public RPC: ≤ 50 blocks per call (or Tatum RPC).
   A tx that must revert (scenarios N10/N11) needs manual `gas=300_000`: `estimate_gas` raises on revert.
7. **Claude on Bedrock:** client `AnthropicBedrock` (InvokeModel; `BEDROCK_CLIENT=mantle` for Mantle). The
   hackathon account has only Claude 4.5 via EU profiles in eu-central-1 (`eu.anthropic.claude-…-4-5-…`,
   no `output_config.effort`; see docs/REFERENCE.md). Models from `.env` (`MODEL_MAIN` /
   `MODEL_FAST`), never hard-coded. **No `messages.parse`/structured outputs on Bedrock** → JSON in the
   answer + pydantic validation + one retry. Manual tool loop (`while stop_reason == "tool_use"`);
   check `stop_reason == "refusal"`; no prefill.
   - Opus 5.5 / Haiku 5.5: no forced `tool_choice`, no `thinking` param, set `output_config.effort`
     explicitly. They are not open to every AWS account — make one test call each in the first hour.
   - Fallbacks (open to all): `anthropic.claude-opus-4-8` (default effort `high` → set `low` for
     negotiation turns) and `anthropic.claude-haiku-4-5` (**does not accept `output_config.effort`** —
     omit it).
8. tCZK escrow: collector and `getTokenStore(operator)` must be allowlisted (and have ROUTER role in
   strict mode) **before** the first `authorize`.
9. **x402 route keys use `:param`, `[param]` or `*` — never `{param}`.** A `{param}` key matches
   nothing, so the route silently becomes free (no 402). FastAPI decorators still use `{param}`.
   Check: an unpaid request must get 402. Deal price comes from the agreed offer via `DynamicPrice`
   (`price=lambda ctx: AssetAmount(...)`, `ctx.path`).
10. **Mandate limits are in euro cents:** 10 € per deal, confirm above 6 € (threshold must be below the
    limit), 30 €/month; the task budget (5 €) is a separate, lower cap. tCZK amounts → EUR at
    `CZK_PER_EUR_DEMO` (25). VAT risk is acknowledged once per (seller IČO, asset) pair (§6.5).
11. **tCZK EIP-712 name comes from `.env` (`TCZK_NAME`)** together with `TCZK`: the new token is
    `"tCZK Deposit Token"`, the prep fallback token is `"Demo Bank tCZK"`. Wrong name = every signature fails.

## Layout (SPEC §17) and ports

```
contracts/            Foundry: src/TCZK.sol, test/, script/Deploy.s.sol  (fallback: py-solc-x, see prep/)
bank/                 app.py (:8000), db.py, mandate.py, screening/{ares,vat,isir,sanctions,datasets}.py,
                      facilitator.py (/x402/verify|settle|supported), escrow.py, verifier.py, disputes.py,
                      explain.py, journal.py, templates/
seller/               app.py (:8001, x402 middleware, /negotiate, /order, /order/{id}/escrow)
agents/               buyer.py, seller_agent.py, prompts/
actors/company-check/ Apify actor (python-empty template)
fixtures/             synthetic companies, recorded registry answers, Google Maps dataset
scripts/              seed.py, demo_*.py, verify_journal.py, smoke.py, scenarios.py, report.py
tests/                unit/, evals/
reports/              validation.md|json (committed; tx links, no secrets)
.github/workflows/    ci.yml (pytest + coverage, MOCK=1, no secrets)
prep/                 pre-hackathon spikes (disclosed)
```

Start order: bank (its facilitator must be up — the seller middleware calls `/supported` on start),
then seller, then agents.

## When blocked

Missing credential (AWS, Mostly Right, tunnel) → documented fallback (MOCK LLM mode with scripted
replies, recorded fixtures, RPC polling instead of webhooks, `x402.org` facilitator for EURC,
`prep/` token for tCZK) + a `TODO(credential)` note; keep building. Ask a human only if nothing can proceed.
