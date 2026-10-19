# Kickoff prompts

## Main prompt (one agent, paste at 12:00)

```
Read CLAUDE.md, then docs/SPEC.md (the implementation spec and the master
document), docs/REFERENCE.md (§8a lessons) and docs/TASKS.md. The hackathon has started and the
`hackathon-start` tag is set. Work through docs/TASKS.md: layer L1 first, then L2, L3, L5, then the
rest; if you are the only agent, interleave stream T (tCZK) after L1. For every task implement the
smallest thing that meets its done-criteria in SPEC §15, run it, commit with a short
imperative message and tick the box. prep/ is disclosed pre-hackathon work: reuse its techniques,
keep its origin in comments. Bedrock: no messages.parse — JSON + pydantic + one retry; MOCK mode
until AWS credentials exist. If a credential is missing, use the documented fallback and continue.
Report after each layer: what works, how to run it, what is next.
```

## Parallel prompts (several agents/people; each owns its folders — TASKS "Streams")

**T — tCZK + bank facilitator (start at 12:00, independent)**
```
Read CLAUDE.md, docs/SPEC.md §3, §4.3, §5.6, §18 and docs/REFERENCE.md §4, §8a.
You own contracts/ and bank/facilitator.py. Do TASKS T1–T4. Use prep/contracts/DepositToken.sol
and prep/spikes/deploy_tczk.py as references only; the new TCZK follows §3.2 (OZ v5.7 ERC3009,
roles, strict mode). Keep the prep token as the stage fallback.
```

**P — payments & escrow**
```
Read CLAUDE.md, SPEC §2, §4.1–4.2, §5, §18 and REFERENCE §3, §8a. Route keys use
`:offer_id`, never `{offer_id}` (else the route is free). You own
bank/escrow.py and seller/app.py. Do L1, then the escrow part of L3. Persist PaymentInfo before
authorize; pending nonces + local counter; reuse prep/spikes/escrow_spike.py techniques.
```

**A — agents & mandate**
```
Read CLAUDE.md, SPEC §6.1–6.3, §6.7, §7. You own agents/, bank/mandate.py,
bank/explain.py. Do L2 incl. minimal negotiation (MOCK mode first, Bedrock after the Toolbox), then
negotiation polish in L7. No messages.parse on Bedrock; manual tool loop; models from .env (fallbacks
in SPEC §7; Haiku 4.5 takes no effort); log tokens and cost per call; the LLM never moves money.
```

**S — screening & verifier**
```
Read CLAUDE.md, SPEC §6.4–6.6, §8, §10. You own bank/screening/, bank/verifier.py,
actors/company-check/. Do L4 and the verifier part of L3. Synthetic companies + recorded fixtures for
the stage; live mode only on a large public company. No personal data.
```

**Q — validation on real testnet transactions (start at hour 3–4, when L1 pays)**
```
Read CLAUDE.md, SPEC §19 (whole), §6.3, §6.7, §12, §18 and REFERENCE §8a. You own tests/,
scripts/smoke.py, scripts/scenarios.py, scripts/report.py, reports/, .github/workflows/. Do TASKS
Q1–Q6 in order. Scenarios run on Base Sepolia with real transactions and assert against the chain,
not our DB; negatives must prove no money moved. Never invent numbers: the report is the only source.
Keep it plain pytest + small scripts; no test frameworks beyond pytest/pytest-cov.
```

**U — UI & demo**
```
Read CLAUDE.md, SPEC §9, §13, §14. You own
bank/templates/ and scripts/demo_*.py. Do L5, then scenario buttons and L8 demo runs.
```
