# docs/

> The project is described in the root [README](../README.md). This folder holds two kinds of documents: the
> project documentation, and the team's internal working files from the build.

## Project documentation

| File | What it is |
| --- | --- |
| [REGULATORY.md](REGULATORY.md) | Regulatory position: MiCA, licensing by ČNB, AML duties and FAÚ, PSD2, DORA, AI Act, GDPR, VAT §109, and what remains for a pilot |
| [DEAL_MODEL.md](DEAL_MODEL.md) | The deal model v2.6 the code implements: delivery through the bank, acceptance predicates, rules A < B < C, deciders, and where each part lives in the code. Russian original: [MODEL_V2.md](MODEL_V2.md) |
| [../reports/validation-sepolia.md](../reports/validation-sepolia.md) | Validation on Base Sepolia: every scenario with its transaction links, speed against targets, coverage, evals, model cost |
| [../reports/validation.md](../reports/validation.md) | Validation on an anvil fork of Base Sepolia, 3 runs of the full matrix |
| [../reports/numbers.md](../reports/numbers.md) | Every measurable number of the project, with the source of each |
| [../contracts/README.md](../contracts/README.md) | The tCZK contract: roles, strict mode, deployment, tests |
| [../actors/company-check/README.md](../actors/company-check/README.md) | The Apify Actor: input, output, scoring, live runs and their cost (also its page on Apify) |

## Internal working documents

Written by the team and for its coding agent during the build. They are kept for history and for the
disclosures, and each says so at the top. The README and the documents above describe the project; where an
internal file disagrees with them, the internal file is out of date.

| File | What it is | Language |
| --- | --- | --- |
| [SPEC.md](SPEC.md) | The implementation spec the build started from (addresses, interfaces, API formats, done-criteria). Written before the hackathon start and disclosed; its deal model is superseded by the deal model v2.6 | Russian |
| [REFERENCE.md](REFERENCE.md) | Facts verified live on testnet before the start (addresses, transaction links, SDK snippets) and lessons learned | English |
| [TASKS.md](TASKS.md) | The build tick-list by stream | English |
| [INTERFACES.md](INTERFACES.md) | Module contracts between the parallel build streams | English |
| [DEMO.md](DEMO.md) | The presenter's 4-minute stage script, with the keyless fallback | English |
| [DEPLOY.md](DEPLOY.md) | AWS deployment runbook | English |
| [assets/](assets/README.md) | How the screenshots and the video poster in the README are made and replaced | English |
| [../CLAUDE.md](../CLAUDE.md) | Instructions for the coding agent (Claude Code) | English |
| [../prompts/](../prompts/) | Kickoff prompts for the coding agent and the disclosure email template | English |
| [../prep/](../prep/README.md) | Pre-hackathon spikes, disclosed to the organisers, not part of the submission | English |

Not documentation: [`agents/prompts/`](../agents/prompts/) holds the runtime system prompts of the buyer and seller
agents (the code loads them), and the `.json` files in [`reports/`](../reports/) are the raw results the reports
are built from.
