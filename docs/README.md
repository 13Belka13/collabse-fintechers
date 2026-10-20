# docs/ — internal working documents

> **Internal.** These are the team's and its coding agent's working files from the build, kept for history.
> The project is described in full in the root [README](../README.md); nothing here is needed to understand,
> run or check it.

| File | What it is | Language |
| --- | --- | --- |
| [SPEC.md](SPEC.md) | The implementation spec the build followed (addresses, interfaces, API formats, done-criteria). Written before the hackathon start, disclosed | Russian |
| [REFERENCE.md](REFERENCE.md) | Facts verified live on testnet (addresses, tx links, SDK snippets) and lessons learned | English |
| [TASKS.md](TASKS.md) | The build tick-list by stream | English |
| [INTERFACES.md](INTERFACES.md) | Module contracts between the parallel build streams | English |
| [MODEL_V2.md](MODEL_V2.md) | Design draft of the next model, under review; summarised in the README [roadmap](../README.md#roadmap) | Russian |
| [DEMO.md](DEMO.md) | Stage demo runbook | English |
| [DEPLOY.md](DEPLOY.md) | AWS deployment runbook | English |
| [assets/](assets/README.md) | Screenshots and demo video thumbnail shown in the README, and how to replace the placeholders | English |

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
