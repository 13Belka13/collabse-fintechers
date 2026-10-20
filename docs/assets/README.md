# docs/assets — screenshots and demo videos

> **Internal**: how to fill the screenshot and video slots of the root [README](../../README.md). Every file
> here is a placeholder until it is replaced by a real capture **with the same name**; the README then shows it
> without any edit.

## Screenshots

PNG, 1280 × 800 (or any 16:10 size), browser zoom 100 %, light theme, under about 500 KB each. Capture the UI
of the running bank: live stack, or keyless (`python -m scripts.demo_seed`, then
`DEMO_SIM=1 uvicorn bank.app:app --port 8000`). On the simulated chain the **SIMULATED CHAIN** banner must stay
visible in the shot.

| File | Page | What should be on screen |
| --- | --- | --- |
| `screenshots/01-story.png` | `/story` | The honest deal after the "Honest deal" button: the steps, the bank's decision time, where the money is now |
| `screenshots/02-bank-feed.png` | `/bank` (client view) | The feed after all five buttons: decisions, transfers, decision time p95, model cost |
| `screenshots/03-bank-compliance.png` | `/bank` (compliance view) | The "Suspicious company" refusal with its full reason and sources |
| `screenshots/04-mandate.png` | `/mandate` | Limits, version list with `policy_hash`, the VAT acknowledgement for Beta and EURC |
| `screenshots/05-deal.png` | `/deals/{id}` | The honest deal: offer, accepted items, escrow state read from the chain, transaction links |
| `screenshots/06-dispute.png` | `/deals/{id}/dispute` | The "Fake data" deal: the two items, the violated criterion, sources, deadlines, accept / reject |
| `screenshots/07-statement.png` | `/statement` | The statement with the reconciliation result |
| `screenshots/08-explorer-revert.png` | [Basescan](https://sepolia.basescan.org/tx/0xbe5adf590d439625c92fafca266fbe8e94b5eeb04800d141c00ba6b0b94765fd) | The reverted direct tCZK transfer with `TransferViaBankOnly` |

## Video

Record the stage demo from [docs/DEMO.md](../DEMO.md) (about four minutes) and, optionally, the three short clips
listed in the README (attack, dispute, tCZK — about 40 s each). Say on the recording which mode is on: live
Base Sepolia or the simulated chain, MOCK or Bedrock.

Publishing, either way:

- **Inline player on GitHub:** open README.md in the GitHub web editor and drag the `.mp4` into it (up to
  100 MB). GitHub uploads it and inserts a `https://github.com/user-attachments/assets/…` link; a link alone on
  its own line plays inline. Put it right under the `## Demo video` heading.
- **YouTube (unlisted) or Loom:** replace the link target `docs/assets/README.md#video` of the thumbnail in the
  README with the video URL.

Then replace `video/demo-full.png` with a frame from the video (1280 × 720) and link each clip name in the
README's clip table. Do not commit `.mp4` files to the repository.
