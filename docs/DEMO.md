# Demo script (4 minutes)

> **Internal runbook**: the team's stage demo script. The project is described in the [README](../README.md).

Commands are in the [README](../README.md#run-it-locally). The demo lives on `/story` (the start page `/`): the
latest deal step by step in plain words, where the money is now, the five scenario buttons, the **Run your own
deal** console and the **economics** panel. The same buttons are on `/bank` for the detailed feed.

## Before the demo

- [ ] Choose the stack:
  - **Live (Base Sepolia):** the stand https://63-182-160-195.sslip.io, or locally
    `uvicorn bank.app:app --port 8000`, then `SELLER_OPENING=list uvicorn seller.app:app --port 8001` (the
    seller opens at the 6 € list price and haggles), with the testnet keys in `.env`, `RPC_URL` on the Tatum
    gateway and `TCZK=0x18245366edA63Ca583c11B27d1134F3d233A115F`.
  - **Keyless fallback:** `python -m scripts.demo_seed`, then `DEMO_SIM=1 uvicorn bank.app:app --port 8000`.
    Same bank code, simulated chain, MOCK model replies; the page shows a **SIMULATED CHAIN** banner. Say so.
- [ ] `python -m scripts.smoke` is green right before the demo (live stack); for the fork,
      `python -m scripts.smoke --target fork`.
- [ ] Balances: ETH for the relayer, the operator and Alfa; EURC and tCZK for Alfa (the smoke test checks them).
- [ ] Real values for `AGENT_TOKEN_ALFA`, `SELLER_TOKEN`, `BANK_TO_SELLER_TOKEN`, `HUMAN_TOKEN`,
      `DECIDER_TOKEN` (the bank warns while development defaults are in use).
- [ ] Tabs: `/story` on the big screen, `/mandate`, `/deals`, `/statement`, `/bank?view=compliance`, and the
      explorer for [tCZK](https://sepolia.basescan.org/address/0x18245366edA63Ca583c11B27d1134F3d233A115F).
- [ ] A phone on the stage table for the QR approval; the QR opens `/approve` (one-time token, 10 minutes).
- [ ] Note the model mode: `MOCK=1` means scripted replies at $0. Say which mode is on.

## Script

| Time | Click | What happens | What to say |
| --- | --- | --- | --- |
| 0:00–0:20 | `/mandate` | Limits per deal (10 €), confirmation above 6 €, 30 € a month, task budget 5 €, versions with `policy_hash` | "A person sets the limits. The agent has no keys and no 'send to address' tool." |
| 0:20–1:20 | `/story` → **Run your own deal** | Give the agents a task (e.g. "15 warehouses in Brno with rating above 4.0"), a budget, EURC, seller Beta, behaviour **Prompt injection** — or type your own seller message. The agents haggle live (≤ 3 turns); the seller's words are marked untrusted; the bank refuses the hidden payee with `COUNTERPARTY_NOT_IN_SCOPE` | "The model talks, the code decides. The agent may be fooled; the money is not." |
| 1:20–2:30 | **Same deal in tCZK** | The bank debits Alfa's account and mints 125,00 Kč (= 5 € at the demo rate). Beta is checked (ARES, VAT, ISIR, sanctions, web footprint); its account is published, so no VAT risk. The money is locked in escrow; Beta delivers through the bank with a signed manifest; the predicates accept the items; rule B: 90 % paid at once (the bank's fee to its clearing address), the rest after the window, one final void. Then the payout: tCZK burned, **crowns on Beta's ordinary bank account**. Reconciliation: bank books = chain, **0,00 Kč** | "The seller received crowns on an ordinary account — it never touched crypto. In strict mode only the bank can move tCZK." Show the [`TransferViaBankOnly`](https://sepolia.basescan.org/tx/0xbe5adf590d439625c92fafca266fbe8e94b5eeb04800d141c00ba6b0b94765fd) revert |
| 2:30–3:00 | `/story` → economics panel | What this deal would cost on-chain at today's Base mainnet gas price, next to the bank's fee and margin | "Fractions of a cent on-chain; the bank earns its fee at capture, paid by the seller." |
| 3:00–3:40 | Console: a deal above 6 € | The bank stops at `CONFIRMATION_REQUIRED` and shows a QR code. A juror scans it with a phone and approves on `/approve`; the deal continues | "Above the threshold a person decides — from any phone, with a one-time 10-minute token." |
| 3:40–4:00 | `/statement` → reconcile | The statement matches the chain; `python -m scripts.verify_journal` shows the hash chain is intact | "Numbers come from the chain, not from our database." Quote [the test results](../README.md#proof-test-results) |

**If time allows or for questions:** **Fake data** (3 of 20 items break the terms, one company closed in ARES:
17 delivered and paid, 3 returned in one final void, no person needed); **Suspicious company** (Delta's website
shows another IČO than ARES: refused before any payment; switch `/bank` to the compliance view — the client
sees a neutral status, compliance sees the reason); **Honest deal** in EURC (`VAT_LIABILITY_RISK`, acknowledged
once on `/mandate`).

"Run the timer now" on `/bank` applies the deadlines at once, so you need not wait for the 5-second tick.

## Keyless run of the same script (DEMO_SIM)

Everything above runs on the simulated chain: `python -m scripts.demo_seed`, then
`DEMO_SIM=1 uvicorn bank.app:app --port 8000`, open `/story`. Transactions are simulated (no explorer links),
model replies are scripted, and the banner says so. From the command line:
`python -m scripts.demo_scenarios tczk --sim` (or `honest`, `fake_data`, `attack`, `suspicious`).

## If something fails on stage

- The seller cannot reach the bank facilitator: it falls back to `x402.org` for EURC only. Restart the bank,
  then the seller.
- No funds or no RPC: switch to the simulated chain (above) and say so.
- A slow model: the bank's model calls time out and fall back, so the deal goes on; if Bedrock is down, set
  `MOCK=1` (scripted replies) and say so.
