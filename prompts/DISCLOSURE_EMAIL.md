# Disclosure email to the organisers (reply to their instruction email)

Fill the [brackets], then send it yourself as a reply to the organisers' instruction email.

---

Subject: Re: [original subject] — pre-hackathon work disclosure, team [team name]

Hi FinTechers team,

Following the rules on pre-prepared work, here is what our team [team name] prepared before the
hackathon. We plan to work in Track 2 (Stablecoins and new financial infrastructure), challenge
"Agents that pay each other", with a tokenized-deposit payment route as a second option.

Prepared before 19 October (it will sit in a separate `prep/` folder of our repo, committed before
the `hackathon-start` tag and listed in the README):

1. Planning documents: a specification, a task list and a demo script.
2. Small learning spikes (scripts) on public testnets only, to check feasibility:
   - an x402 payment in EURC on Base Sepolia via the public x402 facilitator;
   - the Coinbase AuthCaptureEscrow contract called from Python (lock, partial capture, refund);
   - a small demo "tokenized deposit" ERC-20 contract (allowlist and EIP-3009 transfers), deployed
     to Base Sepolia for testing.

No product code (agents, clearing service, verification, dispute flow, UI, Apify actor) exists yet;
all of it will be built during the event, after the `hackathon-start` tag. We use AI coding
assistants and will disclose that in the README. We use testnets and public/synthetic data only.

Please let us know if any of this should be handled differently.

Best regards,
[name], team [team name]
