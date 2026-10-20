# Regulatory position

> For a bank's legal and compliance team. This is the team's reading of the rules that apply
> when a Czech bank runs Collabse, written so a pilot can start from it. A bank pilot begins with its counsel
> confirming it. It is not legal advice. Status as of 20 October 2026.

## Summary

1. **Collabse is software that a licensed bank runs.** In production the bank holds every key, all client money
   and every client relationship. The AI agents hold no keys and have no tool that moves money.
2. **Collabse needs no licence or registration from ČNB.** It does not take deposits, execute payments, issue
   e-money or tokens, or hold or transfer crypto-assets for anyone. Those activities belong to the bank, which is
   already licensed for them.
3. **Collabse is not an obliged person under the Czech AML Act,** so it does not report to FAÚ and FAÚ does not
   supervise it. The bank stays the obliged person; the software is built to help it meet those duties.
4. **tCZK is outside MiCA.** It is a bank deposit in token form, and MiCA does not apply to deposits
   (Art. 2(4)(b)). **EURC is inside MiCA** as an e-money token; its issuer is Circle, and the bank may hold and
   transfer it for clients after notifying ČNB (Art. 60).
5. **Supervision reaches Collabse through the bank.** Collabse is the bank's ICT third-party service provider
   under DORA. The bank's contract gives the bank and ČNB access, inspection and audit rights over the service.

## 1. Who does what in production

| Party | Role in the flow | Licence or status | Supervised by |
| --- | --- | --- | --- |
| **The bank** | Holds the client accounts; issues and redeems tCZK; runs the x402 facilitator and the escrow operator; holds all signing keys in its HSM; pays the seller out in crowns | Banking licence (Act 21/1992 Sb. on banks). For EURC services: MiCA Art. 60 notification to ČNB | ČNB (prudential, conduct, MiCA); AML duties under the AML Act, with reports to FAÚ |
| **Collabse** | Supplies the rules engine, the agents' API, the screening, the delivery checks and the UI; operates nothing on its own account | No licence needed. ICT third-party service provider to the bank (DORA) | Indirectly, through the bank's outsourcing contract |
| **Circle** | Issues and redeems EURC | E-money institution authorised in France; EURC is a MiCA e-money token | ACPR (France) |
| **Coinbase AuthCaptureEscrow** | Open-source smart contract on Base. Coinbase has no role in a deal: the bank is the operator | Software, not a service provider in this flow | — |
| **Buyer and seller companies** | The bank's clients. They set the mandate, acknowledge risks, approve payments above the threshold and receive the statement | Ordinary business clients | — |
| **AI agents** | Negotiate and explain. They call the bank with deal ids only | Software used by the buyer and the seller | AI Act duties sit with whoever deploys them (§2.6) |
| **External decider** (rule C) | Decides a disputed claim by the terms agreed in the offer; drawn through the public drand beacon | Engaged by the bank, paid from the dispute fees | — |

## 2. Regime by regime

### 2.1 MiCA (Regulation (EU) 2023/1114)

| Question | Answer | Why |
| --- | --- | --- |
| Is tCZK a crypto-asset under MiCA? | **No** | MiCA does not apply to deposits, structured deposits included (Art. 2(4)(b)). tCZK is a claim on the client's account at the bank: only bank clients can hold it, each wallet is bound to an account (`accountRef`), it is minted and burned against the account, and in strict mode it moves only through the bank's router. The EBA report on tokenised deposits (December 2024) says the qualification follows such features, not the label. That is why the contract enforces them in code rather than leaving them to policy |
| Does anyone in the flow issue a crypto-asset? | **Only Circle,** for EURC | Authorisation, the reserve of assets and redemption at par are the issuer's duties (MiCA Title IV). Collabse and the bank neither issue nor redeem EURC and hold no reserves |
| Who provides crypto-asset services for EURC? | **The bank** | Custody and transfer of crypto-assets for clients are crypto-asset services (Art. 3(1)(16)(a) and (j)). A credit institution may provide them after notifying ČNB (Art. 60(1)), with no separate CASP authorisation |
| Is Collabse a CASP? | **No** | It holds no keys, has no custody of client assets and executes no transfers. It sells software to the licensed party, not a service to the public |
| Why does this matter now? | The Czech transitional period ended | Since 1 July 2026 only authorised CASPs and notified institutions such as banks may provide crypto-asset services in Czechia ([ČNB notice of 15 June 2026](https://www.cnb.cz/cs/cnb-news/aktuality/Upozorneni-Ceske-narodni-banky-Konec-prechodneho-obdobi-podle-narizeni-MiCA-k1.7.2026)). Putting every service inside the bank is what keeps the model workable |

The project never calls itself "MiCA-licensed" or "MiCA-compliant". The licence belongs to the bank and to the
issuer.

The track asked about issuers, reserves and AML:

- **Issuer.** The x402 client accepts only allowlisted assets, with a cap per asset in atomic units. EURC is on the
  list because a MiCA-authorised issuer stands behind it.
- **Reserves.** EURC reserves are Circle's. tCZK needs no reserve of its own: each token is a deposit on a client's
  account, minted and burned against a bank transaction reference. The bank's books and the token's
  `totalSupply` reconcile on `/statement` to 0,00 Kč.
- **AML.** See §2.4.

### 2.2 Payment services (PSD2, Czech Payments Act 370/2017 Sb.)

- **The bank executes every payment** from its client's account or wallet. Collabse never comes into possession
  of the funds. Technical service providers that support payment services in this way are outside PSD2
  (Art. 3(j)), which the Czech Payments Act transposes.
- **The agent's instruction is the client's own instruction to its own bank,** given within a mandate the client
  set. The bank checks consent in code before it signs (PSD2 Art. 64): per-deal, monthly and task limits, the
  confirmation threshold, the allowed assets and counterparties.
- **Strong customer authentication.** Above the threshold a person approves by scanning a one-time QR code bound
  to the deal and its amount. In a pilot the bank's own SCA with dynamic linking to amount and payee (PSD2
  Art. 97) takes its place.
- **E-money tokens are also funds under PSD2.** Transferring EURC for clients is therefore a payment service as
  well as a crypto-asset service (EBA no-action letter on the PSD2–MiCA interplay; its transition ended on
  1 March 2026). The bank is licensed for both.

### 2.3 Banking law and the tokenized deposit

- **One liability, not two.** Minting debits the client's ordinary account under one bank transaction reference;
  burning reverses it. Debit comes before mint and burn before credit, and one reference moves money once.
- **The bank keeps its powers over the account:** freeze, forced transfer under the account terms with a case
  reference, and the allowlist. They are enforced by the contract
  ([`contracts/src/TCZK.sol`](../contracts/src/TCZK.sol)).
- **In escrow, tCZK is meant to stay the buyer's deposit,** encumbered like a blocked amount on an account, until
  the bank captures it for the seller. Only the captured part changes hands.
- **The seller receives crowns** on its ordinary account after each capture and never holds a token.
- **Open for the pilot opinion:** whether escrowed tCZK keeps its deposit status in every state, how deposit
  insurance applies to tCZK balances, and the accounting and prudential treatment of a deposit token on a public
  chain (the Basel crypto-asset standard).

### 2.4 AML: Act 253/2008 Sb. and FAÚ

**Who is the obliged person.** The bank, as a credit institution listed in §2 of the AML Act. Collabse provides
no payment, crypto-asset or other financial service, is not listed in §2, and so has no reporting duty to FAÚ and
no AML supervisor of its own.

**What the software does for the bank's duties:**

| Bank's duty | Source | What Collabse does |
| --- | --- | --- |
| Screen the counterparty before paying | AML Act; EU restrictive measures | ARES, VAT payer register, ISIR, EU consolidated sanctions list and the seller's web footprint, before the first payment. In parallel with a 2 s timeout. A missing answer fails closed to `UNDER_REVIEW` |
| Do not tip off the client | AML Act §38 | Every reason carries a `public` flag. If any reason is non-public, the client and its agent see only "under review", the same text as a registry timeout. Compliance sees the reason and its source on the deal page |
| Report without undue delay and be able to suspend | AML Act §18 and §20 (24 h suspension, which FAÚ can extend) | Escrow terms leave room for both: the lock runs 30–120 days past the claim window, and deal pages state deadlines as "up to N business days". The hold step for an FAÚ suspension is pilot work |
| Keep records | AML Act, record keeping | Hash-chained journal of every decision and transfer, checked by `python -m scripts.verify_journal`; statement reconciled with the chain |
| Travel Rule | Regulation (EU) 2023/1113 | Between two clients of the same bank the originator and beneficiary data is already in the bank. Transfers to other institutions are on the roadmap |

Persons behind a company (owners, directors) stay with the bank's own screening. The prototype screens companies
only, so it processes no personal data.

### 2.5 ČNB supervision and DORA (Regulation (EU) 2022/2554)

- **Collabse is an ICT third-party service provider** to a financial entity. The bank records the arrangement in
  its register of information (Art. 28) and signs a contract with the terms DORA requires (Art. 30): service
  levels, data location, incident assistance, exit strategy, and access, inspection and audit rights for the bank
  and for ČNB.
- **ČNB therefore supervises the service through the bank,** not Collabse as an entity. At pilot scale Collabse
  is far from the designation of critical ICT third-party providers by the European Supervisory Authorities
  (Art. 31).
- **What already exists for the bank's ICT risk assessment:** idempotency keys on every money transaction, one
  nonce queue per key, fail-closed registries, a configurable RPC endpoint, signed webhooks whose payload is
  re-read from the chain, model timeouts with code fallbacks, a deadline timer that carries on past a failing
  deal, CI on every push and infrastructure as code. In a pilot the keys move to the bank's HSM, split by role.
- **Talking to ČNB.** ČNB does not run a regulatory sandbox with a special regime. It offers consultations through
  its [contact point for financial innovation](https://www.cnb.cz/cs/cnb-news/tiskove-zpravy/CNB-spousti-kontaktni-misto-pro-financni-inovace).
  The pilot runs under the bank's existing licence, in the bank's own test environment, on testnet first.

### 2.6 AI Act (Regulation (EU) 2024/1689)

- **Not a high-risk use.** The agents negotiate business-to-business deals and draft explanations. That use is not
  listed in Annex III; the closest entry, credit scoring of natural persons (point 5(b)), does not apply.
- **Transparency (Art. 50).** Every AI-generated text shown to a person is labelled. The evals check it (29/29
  labelled in the live run of 19 October 2026).
- **Human oversight by design.** Models never move money, never choose a payee and never accept or reject a
  delivered item. Code decides against the mandate and the agreed predicates; a person approves above the
  threshold; an external decider resolves rule-C disputes.

### 2.7 GDPR

- **No personal data in the prototype.** Public business data and synthetic companies only. Google Maps data is
  taken without phone numbers or reviews (`scrapeReviewsPersonalData: false`, capped). The live IČO check runs
  only for legal entities (s.r.o., a.s.), because a sole trader's register entry is a person's name.
- **In a pilot** the bank is the controller. If the service ever processes personal data on the bank's behalf,
  Collabse acts as processor under an Art. 28 agreement.

### 2.8 VAT: Act 235/2004 Sb. §109

A Czech buyer guarantees the supplier's unpaid VAT if, among other cases, it pays to an account not published in
the VAT payer register or pays in a virtual asset. The AML Act §4(8) defines a virtual asset as a MiCA
crypto-asset, except what MiCA Art. 2(4) excludes (deposits included).

- **EURC to a VAT payer** raises `VAT_LIABILITY_RISK`. A person acknowledges it once per (seller IČO, asset); the
  acknowledgement becomes a new mandate version.
- **An unreliable VAT payer** is refused.
- **tCZK to an account published in the VAT register** raises no risk, because tCZK is a deposit.

### 2.9 Verification of Payee: Regulation (EU) 2024/886

Verification of Payee applies in Czechia from 9 July 2027, to euro transfers. Collabse already pays only to the
seller's registered wallet and, for tCZK, checks the seller's account against the accounts published in the VAT
register.

## 3. What remains for the pilot

| Item | Who |
| --- | --- |
| Legal opinion on the qualification of tCZK as a deposit, incl. escrowed tCZK, deposit insurance and accounting | The bank's counsel |
| Legal opinion on the §109 VAT reading for tCZK and EURC | The bank's tax counsel |
| MiCA Art. 60 notification to ČNB, if EURC is offered | The bank |
| DORA contract and entry in the register of information | The bank with Collabse |
| Keys in the bank's HSM, split by role: a relayer that can only route, a timelocked multisig as token admin, issuer and enforcer under four-eyes | The bank with Collabse |
| Mandate and confirmations signed with the bank's SCA (dynamic linking) | The bank with Collabse |
| Hold step for an FAÚ suspension | Collabse |
| Independent audit of the tCZK contract | An external auditor |
| Live EvSM (beneficial owners register) instead of the synthetic extract | The bank |
| Prudential treatment of a deposit token on a public chain | The bank's risk function |

## 4. Sources

- MiCA: [Regulation (EU) 2023/1114](https://eur-lex.europa.eu/eli/reg/2023/1114/oj)
- PSD2: [Directive (EU) 2015/2366](https://eur-lex.europa.eu/eli/dir/2015/2366/oj)
- DORA: [Regulation (EU) 2022/2554](https://eur-lex.europa.eu/eli/reg/2022/2554/oj)
- AI Act: [Regulation (EU) 2024/1689](https://eur-lex.europa.eu/eli/reg/2024/1689/oj)
- GDPR: [Regulation (EU) 2016/679](https://eur-lex.europa.eu/eli/reg/2016/679/oj)
- Transfer of funds (Travel Rule): [Regulation (EU) 2023/1113](https://eur-lex.europa.eu/eli/reg/2023/1113/oj)
- Instant payments and Verification of Payee: [Regulation (EU) 2024/886](https://eur-lex.europa.eu/eli/reg/2024/886/oj)
- Czech acts: [253/2008 Sb. (AML)](https://www.zakonyprolidi.cz/cs/2008-253),
  [235/2004 Sb. (VAT)](https://www.zakonyprolidi.cz/cs/2004-235),
  [370/2017 Sb. (payments)](https://www.zakonyprolidi.cz/cs/2017-370),
  [21/1992 Sb. (banks)](https://www.zakonyprolidi.cz/cs/1992-21)
- ČNB: [end of the MiCA transitional period, 15 June 2026](https://www.cnb.cz/cs/cnb-news/aktuality/Upozorneni-Ceske-narodni-banky-Konec-prechodneho-obdobi-podle-narizeni-MiCA-k1.7.2026);
  [contact point for financial innovation](https://www.cnb.cz/cs/cnb-news/tiskove-zpravy/CNB-spousti-kontaktni-misto-pro-financni-inovace)
- EBA: report on tokenised deposits (December 2024); no-action letter on the interplay between PSD2 and MiCA
