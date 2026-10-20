# docs/ — design and working documents

> The project is described in the root [README](../README.md). [MODEL_V2.md](MODEL_V2.md) is the deal model the
> code implements; the rest are the team's and its coding agent's working files from the build.

| File | What it is | Language |
| --- | --- | --- |
| [MODEL_V2.md](MODEL_V2.md) | The deal model v2.6 the code implements: delivery through the bank, acceptance predicates, rules A < B < C, deciders, and where each part lives in the code | Russian |
| [SPEC.md](SPEC.md) | The implementation spec the build started from (addresses, interfaces, API formats, done-criteria). Written before the hackathon start, disclosed; its deal model is superseded by MODEL_V2 | Russian |
| [REFERENCE.md](REFERENCE.md) | Facts verified live on testnet (addresses, tx links, SDK snippets) and lessons learned | English |
| [TASKS.md](TASKS.md) | The build tick-list by stream | English |
| [INTERFACES.md](INTERFACES.md) | Module contracts between the parallel build streams | English |
| [DEMO.md](DEMO.md) | Stage demo script (4 minutes) with the keyless fallback | English |
| [DEPLOY.md](DEPLOY.md) | AWS deployment runbook | English |
| [assets/](assets/README.md) | Screenshots and demo video poster shown in the README, and how to replace them | English |

Other internal material in the repository:

| Path | What it is |
| --- | --- |
| [../CLAUDE.md](../CLAUDE.md) | Instructions for the coding agent (Claude Code) |
| [../prompts/](../prompts/) | Kickoff prompts for the coding agent and the disclosure email template |
| [../prep/](../prep/README.md) | Pre-hackathon spikes, disclosed to the organisers, not part of the submission |
| [../contracts/README.md](../contracts/README.md) | Developer reference for the tCZK contract |
| [../actors/company-check/README.md](../actors/company-check/README.md) | Developer reference for the Apify Actor (also its page on Apify) |

Not documentation: [`agents/prompts/`](../agents/prompts/) holds the runtime system prompts of the product's
buyer and seller agents (loaded by code), and [`reports/`](../reports/) holds the generated validation
reports that the README quotes.
