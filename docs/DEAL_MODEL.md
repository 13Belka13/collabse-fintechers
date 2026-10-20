# Deal model v2.6

> English translation of [MODEL_V2.md](MODEL_V2.md) (Russian original, the version the code was built from). The two say the same; section numbers match.

> **Design document**: the deal model the prototype implements (v2.6). The project is described in the [README](../README.md#how-it-works); where each part lives in the code: §15.

Model v2.6, 20.10.2026. The bank executes the terms of the agents' deal; it does not judge the goods. This model replaces the scheme of SPEC §8 ("the bank checks the content of a delivery with rules and an AI judge"): acceptance is decided by predicates in code, and there is no AI on the money path.
We take the mechanics from proven analogues and write "as at X". The precedents are of two kinds: **deployed** (in use on the market: ICC trade finance, Visa, Upwork, Escrow.com, AAA, UDRP) and **proposed** (drafts and prototypes: ADRP, AP2, ASP, x402r, Circle Refund Protocol). What is our own is marked "ours". Sources are in the appendix.
Numbers are the team's proposal unless a source is given. [L] marks the team's legal position; in a pilot it is confirmed by a lawyer's opinion.

## 0. Principles

1. **The bank deals in documents, not goods**, as in ICC trade finance: delivery against secured payment (documentary collection, URC 522 Art. 7), examination of documents for compliance with the terms (letter of credit, UCP 600 Art. 14), partial payments (UCP 600 Art. 31). The bank checks only what is objective: time, signatures, quantity and the predicates from the buyer's task, fixed by a hash in the offer. A quality dispute (rule C) is decided by an **external decider**; the bank executes the decision, does not decide cases and does not assess quality.
2. **The buyer's agent has no money keys.** It can only ask the bank to pay an agreed deal (`pay(deal_id)`); money goes only to the wallet of a checked seller. The agent calls the bank with its own access token; it holds no money keys.
3. **The buyer receives only what it pays for.** Items that fail never reach it (§5.2).
4. **Disputed money is not paid out until the dispute is decided, and it is not returned to the buyer by default.** It stays in escrow on-chain for the whole dispute (except for a few blocks during the split-off of D and during a rollover, when it is under the bank's key and the mandate reservation, §8.2, §8.4): the escrow term is long (§8.3), and the bank closes the deal itself, with a payout and a final return. An undisputed refund does not wait for the dispute (split-off of D, §8.2).
5. **Where a person is needed** (and how often):
   - a person at the buyer: signs the mandate with SCA (once every few months); confirms the task terms (once per task); confirms payments above the mandate's threshold (rarely); "accept anyway" (optional);
   - an external decider: only a C dispute at T2 (a fraction of a percent of deals);
   - the bank's compliance: AML cases;
   - at the request of a seller who is a natural person: human review of the autocode result (GDPR Art. 22).
   Everything else is automatic.
6. **The bank does not carry unlimited risk.** Its costs are its own errors (by law) and no more than one deal amount in a failure; in a mass failure, new authorizations are suspended.

Answer to the track's brief: "spending limits" is the mandate (§2); "disputes and refunds between non-humans" are the autocodes and the quality rules (§6–§7); "dispute path a human can follow" is the deal page (§11) and the decider's page `/deciders/{id}`; rule C is tested by unit tests on a simulated chain.

**What code checks, and what the quality rules cover.** The autocodes check what is objective: signature, quantity, schema, predicates and public registries. Data that formally meets the terms is covered by the B guarantee (priced in), by a C dispute with evidence dated as of the delivery, and by the seller's open statistics (§9).

**The role of the chain.** The bank holds the payer's key and is the escrow operator. The on-chain lock is verifiable proof for the seller that the money is there; it fixes the payee and the fee cap; both sides see every movement. The seller's relationship with the bank is governed by the bank's licence and the contract. The chain is a shared, verifiable journal for the agents of two companies.

## 1. Parties and keys

| Who | What it holds | Note |
|---|---|---|
| Person at the buyer company | The mandate and the task terms | Signs the mandate with **SCA** (PSD2 Art. 97). Only a person can change the mandate, confirm the task terms and "accept anyway" |
| Buyer's agent | A non-money API key | Tools: `find_seller`, `check_seller`, `negotiate`, `pay(deal_id)`, `fetch_delivery`, `file_claim`, `get_statement` |
| Bank | The buyer's wallet (for tCZK, the account), the escrow operator key (in an HSM, with a backup), the tCZK relayer, the clearing address for fees | T1/T2 are available only to buyers with a wallet at the bank |
| Seller | **Its own manifest signing key** and its own wallet | The bank stores the public key with its registration and revocation times |
| Deciders | A decision signing key | Accredited dispute resolution providers under contract with the bank (as Upwork hands arbitration to an outside organisation and Escrow.com to an outside arbitrator; UDRP providers decide domain disputes for registrars) |

## 2. Mandate

As now: a limit per deal and per month, a task budget, a threshold for human confirmation, versions with a hash.

**SCA:** the mandate is signed with SCA. The agent's payments within the mandate fall under the exemption for secure corporate processes (RTS 2018/389 Art. 17: legal persons only, with the supervisor's consent). If ČNB does not agree, the fallback grounds are Art. 13 (a person with SCA adds the seller to the trusted beneficiaries once) and Art. 16 for T0 (low-value amounts: up to 30 €, cumulatively 100 € or 5 transactions) [L].

New:

```json
"unprotected_budget_eur_per_day": 50,
"unprotected_per_seller_eur_per_day": 20,
"session_budget_eur_per_day_per_seller": 20,
"dispute_floor": [
  {"tier_sum_eur": "<= 20",     "min_tier": "T0"},
  {"tier_sum_eur": "(20; 500]", "min_tier": "T1", "min_window_min": 30, "allowed_quality": ["A", "B"]},
  {"tier_sum_eur": "> 500",     "min_tier": "T2", "min_window_h": 72, "allowed_quality": ["A", "B", "C"]}
],
"require_b_for": ["data_purchase"],
"require_c_above_eur": 2000,
"claim_fee_budget_eur_per_month": 1000
```

- **`tier_sum`** = the largest of (the deal amount; the sum of deals and sessions with this seller in 24 h, including this one; **the sum of this task's deals with this seller and with the sellers linked to it through a beneficial owner** (EvSM), including this one). `dispute_floor` and `require_c_above_eur` are compared against this same sum: "lots of 1 999 €", or splitting across days and linked sellers, do not get around the C requirement, because each next deal gets its tier and rule from the accumulated sum. Deals already concluded do not change retroactively, so at most one amount under the threshold stays without C. Half-open intervals: ≤ 20 / (20; 500] / > 500. The task's total **across all sellers** affects only the mandate limits. Sessions count in `tier_sum` for **deals** (against splitting), but sessions themselves do not fall under `dispute_floor`: only the limits on unprotected payments below cap them.
- **`unprotected_budget_eur_per_day`** is **one shared** daily cap on everything unprotected: T0 and sessions, across all sellers together. Within it, T0 with one seller is capped at `unprotected_per_seller_eur_per_day`, and sessions with one seller at `session_budget_eur_per_day_per_seller`. A compromised agent loses no more than the shared cap in a day, however many sellers there are.
- **`require_b_for`** and **`require_c_above_eur`** say where the mandate requires the B guarantee or a C dispute. The quality rules are ordered **A < B < C**: rule C includes the B guarantee (§7C), so both B and C meet a B requirement. The deal's category (`data_purchase` and others) is set by the task terms a person confirmed, not by the seller. A finite delivery in a `require_b_for` category does not go through T0: at `tier_sum` ≤ 20 € it is lifted to T1. Sessions (streaming data) do not fall under `require_b_for`: they have no delivery the guarantee could apply to, and their risk is capped by the shared daily cap on unprotected payments.
- If **rule C is unavailable** for a deal for which the mandate requires it (no published `source` template for this data, no field mapping for the task's fields, no pool and no reserve provider), the deal can go ahead under rule B with confirmation by a person at the buyer; the confirmation page says why C is unavailable and which fields only B protects. The C requirement is a protection, not a ban on buying.
- If the B guarantee is unavailable to the buyer because of its own claim limits (§7B), a deal under `require_b_for` is still possible: rule B with no claims left, confirmed by a person at the buyer. The buyer keeps the ability to buy data, but it gets no refunds without evidence beyond the limit.
- **`claim_fee_budget_eur_per_month`** is the budget for C dispute fees, separate from the per-deal limit. **It is reserved at `pay`** of each C deal (like the B slots, §7B) for the largest possible fee `F_max = min(1000; max(50 + 2·count; 10 % of the deal amount))`, doubled for a buyer with the ×2 fee (§9); the reservation is released when the window closes with no C claim, and reduced to the actual F when a claim is filed. Not enough budget for the reservation → a C deal only with human confirmation. So the fee for any C claim the mandate required is always paid, and "claim withdrawn for non-payment" (§7C.3) happens only by the buyer's decision, never because the budget ran out.
- The **deal** budget (the per-deal and monthly limits) is reserved at `pay` and stays reserved until the final settlement; the fee budget reservation follows the rule above.

### 2.1 Task terms
The agent extracts the acceptance predicates (§6) from the person's task. The person confirms them once per task, seeing them **in plain language with 3 examples of passing and failing items**; for frequent tasks there are templates in the mandate. The hash of the terms is written into the task. The seller's offer must carry the confirmed predicates **verbatim** (it may add its own but may not remove any); otherwise the bank refuses.
- Predicates are joined **by AND only**: an item is accepted if it passes all of them. The dictionary has no OR, no negation and no priorities, so a seller's predicate can only narrow acceptance, never loosen it. Predicates the seller added are shown on the deal page.
- "Verbatim" is checked by code: each confirmed predicate in canonical form (JCS, §5.1) appears in the offer's list byte for byte.
- If the quality rule is C, the task terms must contain a `source` predicate (§6): a **source template** the buyer picks from the list the bank publishes (for example, ARES by `{ico}`; for sources that need an API key, the bank takes the snapshot through the API with its own key), and the item's key field. Without it the bank does not accept an offer with C. Rule B needs no source: a B refund goes through without evidence.
- Each template publishes a **field mapping**: "item field → path in the snapshot" (a JSON path in the API answer or a selector in HTML) and a **comparison type** (string, number, date, code, enumeration). Under C the task terms automatically require the item field to be of the same type (through `json_schema`). A C dispute is possible only on fields that have such a mapping; on the rest, only B.

## 3. Checks before payment

| Check | Source | On failure |
|---|---|---|
| Mandate limits | Mandate journal | Refusal / human confirmation |
| The seller is active, not in insolvency, not under sanctions | ARES, ISIR, EU sanctions | Refusal with a reason |
| The VAT payer is reliable, the account is published | VAT register | Refusal or `VAT_LIABILITY_RISK` |
| Account published recently, recent change of director, company younger than N months | VAT register, ARES | Signal: lower limits, a human confirmation threshold |
| Related parties (common beneficial owner) | EvSM | AML signal; not shown in the open statistics |
| **The seller's open statistics** (§9) | Bank data | Shown to the buyer's agent before the deal |
| **Availability of the B guarantee** for this pair (§7B) | Bank data | Shown before the deal; if unavailable, the deal at the A price or a refusal |
| **Availability of rule C** for this pair (a pool without a common beneficial owner, or a reserve provider, §7C.6) | Bank data | Shown before the deal; if unavailable, the deal without C; if the mandate requires C, rule B with human confirmation (§2) |
| Payee = the seller's bound wallet | The bank's register | Refusal |
| Repeat request | Idempotency by `deal_id` | Refusal |
| The task predicates are verbatim in the offer; the tier, rule and window are not below the mandate's requirements | §2 | The agent cannot accept / the bank refuses |

## 4. Tiers

| | **T0 "Instant"** | **Session** | **T1 "Documentary"** | **T2 "With a decider"** |
|---|---|---|---|---|
| `tier_sum` | ≤ 20 € | Micro-payments beyond T0, within `session_budget` | (20; 500] € | > 500 € or by the seller's choice |
| Rail | x402 exact through the bank's facilitator | One authorization per session, capture by vouchers | Escrow, delivery through the bank | Escrow, delivery through the bank |
| Claim window | None | None | 30 min – 24 h | 72 h – 14 days |
| Autocodes (§6) | Bank errors only | Bank errors only | All | All |
| Quality rules (§7) | — | — | A or B | A, B, C |
| People | None | None | None | Only the decider in C |
| Bank fee (proposal) | 0,2 %, debited from the seller's account once a month | 0,2 % at capture | A: 0,8 %, B: 1,2 % | 1,5 % |

- **Session for micro-payments:** one authorization per session. For each request the buyer's agent signs, with its non-money key, a **cumulative voucher** "N requests paid, total S" (as in payment channels). The bank captures by the last voucher; the remainder goes back in a final void. Usage-based escrow as an idea comes from Fortytwo x402Escrow (there the facilitator reports the amount, and there are no vouchers).
- **C under splitting:** the tier and the C requirement are computed from `tier_sum` (§2) at the moment of `pay`; when the accumulated sum with the seller (and the sellers linked to it) crosses the threshold, C becomes available and required for the group's next deals. Deals already concluded do not change.

## 5. Delivery through the bank

Delivery and receipt are **one event**: the seller hands the set to the bank, the bank recomputes it and hands the buyer **only the accepted items**. The principle of delivery against secured payment comes from documentary collection (URC 522 Art. 7). The difference: in a collection the bank does not examine the content of the documents, while here it checks that they comply with the terms, as in a letter of credit (UCP 600 Art. 14). Deadlines run only from submission through the bank (like Submit Work at Upwork).

### 5.1 Format
- Scope: finite sets of JSON items, up to 10 MB per delivery. Streaming data goes through a session (§4).
- JSON follows I-JSON (RFC 7493): numbers above 2^53 as strings; strings in NFC; no external `$ref` in `json_schema`. An item is canonicalised by RFC 8785 (JCS).
- Leaf: `H(0x00 ‖ uint64_be(index) ‖ JCS(item))`, node: `H(0x01 ‖ left ‖ right)`, `H` = SHA-256; an odd node is promoted without duplication. The bank publishes a reference implementation and test vectors.
- **Manifest** (EIP-712, signed with the seller's key): `{deal_id, offer_hash, version, count, merkle_root, nonce}`.
- **Source snapshot.** Under rule C, a neutral bank service saves at receipt a dated snapshot of each item's source (the evidence for C, §7C.2). **The bank builds the URL itself** from the `source` template (§2.1) and the item's key field (IČO, `placeId`); a `source_url`, if the seller sent one, is not used for the snapshot. The template's host is compared exactly, with no subdomains, so pages with user content (`sites.google.com` and the like) cannot become the source. The bank publishes templates only for sources where the page for the key belongs to the source itself.
- **The snapshot service** is a separate, isolated process with no access to the bank's network: `https` only, only the hosts of published templates, the address after DNS is checked against private and reserved ranges (protection against SSRF and DNS rebinding), no cookies and no authorization, limits of 2 MB and 10 s per snapshot, at most 3 redirects within the same host. For each template the bank publishes the snapshot method (HTML, API or rendered page). A snapshot stores the response, its hash and the time by the bank's journal.
- **Quotas:** snapshots are taken only for items that passed the other predicates, and for no more than the ordered number of items per version; per deal, no more than 3 versions and 3 resubmissions after `ROOT_MISMATCH`. Beyond the quota the bank does not accept the delivery (`QUOTA_EXCEEDED`; this is not a bank error and does not count toward the circuit breaker of §8.4).
- The source answered unambiguously "no such key" (a 404 or an explicit API answer), or the response is over the size limit → the item gets `SOURCE_UNAVAILABLE` (§6) and can be replaced before the deadline. A timeout, an overrun of the time limit, a redirect to another host, 5xx, 429 and a failure of the snapshot service itself are **not the seller's fault**: the item waits for its snapshot (it is neither accepted nor released), the bank retries with pauses, and the delivery deadline is extended by the waiting time (§5.2 p. 9).
- **Fair snapshot queue:** the request limit for each source is shared equally among sellers, so one seller's deliveries (including through sham buyers) do not slow down the snapshots for other sellers' deals.
- If there is no snapshot for 5 business days, the item is **not accepted automatically**: a person at the buyer decides whether to accept it without the right to C (B remains) or to reject it. Silence means rejection. Such items do not count toward the seller's rejection rate (§9).
- Only a snapshot by a `source` from the terms a person confirmed serves as evidence for C; a `source` the seller added is not evidence.

### 5.2 Flow
1. The bank locks the amount in escrow; the seller checks the lock on-chain (`verify_escrow`): payee, `feeReceiver` = the bank's clearing address, `minFeeBps = maxFeeBps` = tariff, deadlines, payer. The deal's PaymentInfo has the salt `keccak(deal_id ‖ n)`, so the seller links it to the deal itself.
2. The seller sends the bank the set + the manifest.
3. The bank records the **time of receipt by its own journal** (this is the delivery time) and checks: the seller's key is valid at the moment of receipt; `deal_id`, `offer_hash`, `nonce`; the delivery deadline; it **recomputes the root and the item count itself**. On a mismatch the bank returns its own root at once, and the seller may resend the same version before the deadline.
4. **The bank** runs the autocodes of §6 and computes the **amount payable P** (for the accepted items).
5. **Release:** `fetch_delivery` gives the buyer **only the accepted items** of the current version. The bank tells the seller which items failed and why.
6. **Replacement:** before the delivery deadline, before the final void and before a C claim is filed (§7C.1), the seller may send `version + 1` replacing the failed items. Accepted items do not change, and **P does not decrease**. The bank rejects a repeat of a version it has already accepted. The B claim window for the items of the new version runs from its receipt. `unique` and `max_count` are counted **over all accepted items of the deal in all versions**: a replacement whose `unique_key` matches an already accepted item gets `DUPLICATE`; in total, no more than the ordered number of items is paid. `unique` also applies **across all deals of one task** (by the hash of the task terms, §2.1): the bank keeps the hashes of the `unique_key` of accepted and paid items, so buying the missing items later does not pay again for what was already received. Items returned under B or C **stay blocked for the same seller** in this task (and in all versions of the deal): the same bad record cannot be sold to the buyer a second time; it can be bought from another seller. A cross-deal `DUPLICATE` does not count toward the seller's rejection rate (§9): the seller cannot filter it out in advance.
7. **Accept anyway:** only a person at the buyer, before the final void and within the locked amount. For each item the person sees the code, the predicate it broke and the masked field value (like a notice of discrepancies under UCP 600 Art. 16); accepting the discrepancies works like a waiver under UCP 600 Art. 16(b). The items are released after payment.
8. Data passed to the buyer around the bank is the seller's risk: payment is only for delivery through the bank.
9. **If the bank cannot receive deliveries, that is a bank error:** the delivery deadline is extended by the outage, and outages are published.

### 5.3 Data
- For delivery in transit the bank is the buyer's **processor** (GDPR Art. 28); for keeping evidence, AML and statistics it is an **independent controller** [L].
- The set is kept until the end of the window and the resolution of disputes, then deleted; failed items are kept until the end of the replacement period. The journal keeps hashes, manifests, autocode results, source snapshots and the items that claims refer to.
- The seller warrants that the data source is lawful and that it has the right to pass the data on.
- In production the set is stored by a subsidiary of the bank under an outsourcing agreement (EBA Guidelines on outsourcing); the bank computes the autocodes and P [L].
- A seller who is a sole trader (a natural person) may request human review of the autocode result (GDPR Art. 22).

## 6. Autocodes (no people, T1 and T2)

Deployed precedent: examination of documents for compliance with the terms (UCP 600 Art. 14). Proposed: the cryptographic dispute class of ADRP §6.1 and the acceptance criteria of the cart mandate in ADRP §11.

| Code | How the bank checks | Result |
|---|---|---|
| `NO_DELIVERY` | No accepted delivery by the deadline | Final void, everything to the buyer |
| `SHORT` | `count` < ordered | Payment for what was delivered; no penalty, shown in the fulfilment rate (§9) |
| `BAD_SIGNATURE`, `WRONG_DEAL`, `ROOT_MISMATCH` | Signature, binding to the deal, root recomputation | Delivery not accepted; it may be resent before the deadline (resubmissions within the quota of §5.1) |
| `QUOTA_EXCEEDED` | More versions, resubmissions or items than the quota of §5.1 | Delivery not accepted; not a bank error |
| `SCHEMA_FAIL` | The item fails the JSON schema from the terms | Not paid and not released |
| `DUPLICATE` | The `unique_key` matches another item of the deal (in this or an already accepted version) or an accepted item of another deal of the same task | Not paid and not released |
| `CRITERIA_FAIL` | The item fails the predicates from the terms | Not paid and not released |
| `REGISTRY_CONTRADICTION` | The item contradicts a public register on the delivery date: the IČO is not found, terminated in ARES, the company is in ISIR, or **the IČO belongs to another company** (the item's name and address do not match the ARES record). Applies if the task terms contain such a predicate | Not paid and not released |
| `SOURCE_UNAVAILABLE` | Under C: the item has no source key field; the source at the URL the bank built answered unambiguously "no such key"; the response is over the size limit; or there has been no snapshot for 5 business days and a person at the buyer did not accept the item without C (§5.1). Temporary source failures (timeout, 5xx, 429) mean waiting, not this code | Not paid and not released; can be replaced before the deadline |
| `OUT_OF_MANDATE`, `DOUBLE_PAYMENT` | Mandate and deal journal | **Bank error:** refund from the bank's own money no later than the end of the next business day (PSD2 Art. 73 / zákon 370/2017 Sb.). Does not affect the seller |

**Predicates** are a fixed dictionary: `required`, `eq`, `in`, `range`, `regex` (RE2 only), `json_schema` (depth and time limits, no external `$ref`), `unique` (over all items of the deal, §5.2 p. 6), `max_count`, `registry_active`, `source`. There is no arbitrary code and no AI. Code checks **what the seller itself stated in the item** and what is visible in public registers, not quality.
- **A missing field fails:** any predicate on a field (`eq`, `in`, `range`, `regex`, `registry_active`, `source` and the constraints inside `json_schema` for fields named in the terms) requires the field to be present in the item and not `null`; otherwise `CRITERIA_FAIL`. In other words, `required` is added automatically to every field in the terms.
- `registry_active(ico_field, name_field, address_field?)`: the IČO is active in ARES and not in ISIR on the date of receipt **and belongs to this item**: the name (after normalising case, diacritics and legal form) matches `obchodniJmeno` in ARES, and the address, if the predicate names it, matches `sidlo` down to the street and PSČ. Without this binding the seller could substitute any live IČO. For data without an IČO (for example, places from Google Maps) the predicate does not apply: registry facts are not checked there, and only B and C protect the buyer.
- `source(template, key_field)`: the key field is present and fits a source template from the bank's list; the bank builds the snapshot URL from it (§5.1).

There is no AI on the money path: the model proposes predicates from the task, the person's confirmation is what counts, code decides acceptance, and there is no AI judge.

## 7. Quality rules (what code does not check)

The seller picks **one** rule in the offer, within the mandate's requirements. The rules are ordered **A < B < C**: rule C always includes the B guarantee (a refund of up to X % without investigation) and adds a dispute with a decider for what is proven beyond it. So both B and C meet a mandate requirement of "at least B". All amounts are computed from **P**.

### A "No quality claims"
Autocodes only. The payout of P and the final void happen at the delivery deadline (no later than N business days), or earlier if a person at the buyer confirmed acceptance. Before automatic acceptance the buyer gets a notice.

### B "Quality guarantee X %" (X ∈ {5, 10, 20})
- The buyer takes back up to X % of P **without investigation**, naming the items and a reason from the list (`closed`, `wrong_data`, `irrelevant`, `other`).
- This is **the seller's refund rule, announced in advance** and priced in, like Visa Rapid Dispute Resolution, where the merchant sets rules for automatic refunds in advance. It is not fraud protection.
- The buyer's limits (shown **before the deal**, §3):
  - no more than 2 B claims per buyer–seller pair in 30 days;
  - a new buyer (fewer than 20 deals): no more than 2 B claims in 30 days in total;
  - share of deals with a B claim > 30 % in 30 days: B is unavailable.
  - **Slots are reserved at `pay`:** each deal with B (or C) and an open window takes a claim slot. B is available in a new deal only if "claims used + B deals with an open window" is below the limit. So 15 parallel deals will not give a new buyer 15 refunds: only the first 2 get B, the rest go at the A price or as "B with no claims" (§2). A slot is freed if the window closes without a claim.
  - If B is unavailable: the deal at the A price or a refusal; if the mandate requires B: rule B with no claims left, with human confirmation (§2). A guarantee that was paid for is not taken away retroactively. The same limits apply to the B part inside rule C.
- B claims do not count toward sanctions against the seller (at Visa, non-fraud refunds through RDR do not count toward the VAMP dispute ratio; fraud ones count from 01.04.2025). The open statistics show **the B claim frequency by the number of independent buyers**, without reasons (§9). On B claims the bank checks only for signs of fraud and collusion.

### C "B guarantee + dispute with an external decider" (T2)
0. **The B guarantee inside C:** the buyer may take back up to X % of P without investigation under the rules of §7B. Items returned under B are not part of the C claim; items of the C claim are not part of the B refund.
1. **One C claim per deal.** Withdrawal = giving up C for this deal. **D is the disputed sum** (the amount payable for the disputed items), not the deal amount. The deal's C window ends with the window of the **last** accepted version (§5.2 p. 6). **After a C claim is filed, no new versions of the deal are accepted**: failed items can be replaced only before the claim. So "bait" in an early version does not let the seller send junk after the claim or after the decision. The delivery deadline in the offer cannot exceed the deadline in the task terms.
2. **The only evidence is the source snapshot the bank's service saved when it received the delivery** (by the URL the bank built from the `source` template, §5.1). It shows the source exactly at the moment of delivery. A snapshot taken after delivery does not qualify (the data may have gone stale during the window); older copies (a web archive) do not qualify either: the data may have changed before delivery, and a correct item would look "wrong". The burden of proof is on the buyer; an item without such evidence is decided in the seller's favour; what cannot be proven is covered only by B. Registry facts are checked automatically (`REGISTRY_CONTRADICTION`, §6). Precedent: a chargeback by reason code needs documents (Visa).
   - **The bank checks admissibility when the claim is filed, before any fees** (form only, not substance): for each disputed item the claim names only the **field**; the evidence is the bank's snapshot of that item. **The bank itself extracts the field's value from the snapshot** by the template's published field mapping (§2.1); it accepts no quotes from the buyer. An item is admissible if the extracted value exists, has the same comparison type as the item's field, and **differs** from it after the same normalisation as in the predicates. Items and fields that fail the check (no mapping, no value extracted, the values match) are dropped from the claim at once; D and the fee are computed only on what remains. If nothing remains, there is no C claim and no fees, and the right to C on the deal stays until the end of the window. This way only items where the source on the delivery date says something different in **the same field** reach the decider; the decider decides whether the difference matters in substance ("Praha 1" and "Praha-Staré Město", the line of business by NACE).
   - The buyer can make this comparison an autocode in advance: an `eq` predicate between the item field and the snapshot field in the task terms. A mismatch is then rejected at receipt (`CRITERIA_FAIL`) with no dispute.
3. **Negotiation first**, 2 business days: the seller may agree, and the refund then goes without fees (D is marked for refund; the money follows the schedule of §8.2, as in p. 4). No agreement → the buyer **pays F within 3 business days** after the negotiation ends; if it does not, the claim counts as withdrawn and the bank captures D to the seller at once. D cannot be frozen without paying the fee. Precedents: the negotiation stage at Escrow.com, free mediation at Upwork, correspondence before turning to the administration at plati.market.
4. **Both sides' fees are paid in, not locked** (like the arbitration fee at AAA and Upwork; Upwork allows 7 days to agree and pay): F = a base of 50 € + 2 € per disputed item, at least 10 % of D, at most 1000 €. The buyer pays on moving to the decider (p. 3); the seller pays **within 3 business days after the buyer's payment**. Both sides **pay at once** (capture to the bank's clearing address; an authorization with `maxFeeBps = 0`). The decider starts work only when **both** fees are in, as at Upwork, where arbitration starts after both parties have paid. The seller does not pay in time → the decision goes to the buyer **without a decider**: D is marked for refund to the buyer, and the buyer's fee F is returned in full; the money follows the schedule of §8.2: at window end a capture of the undisputed remainder, then the final void (void returns the whole uncaptured remainder, so it is not done before the window ends). So the seller cannot wait for the decision and only then decide whether to pay.
5. **The fee does not depend on the outcome** (as at AAA and Upwork): the bank pays the decider from the loser's fee; the bank returns the winner's fee in a separate transfer after the period for a re-review (p. 6). With a split outcome (for example, 60/40), proportionally.
6. **Appointment of the decider that nobody can steer** (commit → reveal order):
   - the pool is all accredited deciders minus the objective challenges (a common beneficial owner with a party, cases with the same party in the last 90 days);
   - **the schedule is fixed when the decider stage opens** (when both fees are in, p. 4, and the case's turn in the queue has come, p. 10) and is written on-chain together with the hash of the **bank's secret salt** `s`: the deadline for recording challenge hashes T1 = +24 h, the reveal deadline T2 = T1 + 24 h, the **selection round number** = the first drand round after T2, the **sample round number** = the first round after T2 + 48 h. Round numbers are computed from time in advance, so neither the parties nor the bank choose which round counts;
   - each party may challenge up to 2 deciders: before T1 it writes the **hashes** of its challenges on-chain, and before T2 it reveals them;
   - the decider is chosen by `H(drand[selection round] ‖ s)`. The salt and the round number are fixed before the round comes out, so the bank cannot pick the decider, and the parties cannot compute it from public data before the decision; after the decision the bank reveals `s`, and anyone can recheck the choice afterwards. The sample (p. 7) is drawn the same way, by the sample round;
   - **challenges not revealed by T2** (or a reveal that does not match the hash) → that party's challenges do not apply, and the appointment goes ahead without them;
   - **the minimum pool is 3 deciders**, and no challenges take it below that. Challenges apply in order: first "common beneficial owner" (always), then "cases with the same party in 90 days", from the oldest case to the most recent, as long as the pool stays at 3 or more, then the parties' challenges in turn (each party's first challenge, then the second). The "common beneficial owner" challenge is mandatory: if the pool has fewer than 3 deciders once these are removed, the case goes to a **reserve provider** of dispute resolution under contract with the bank (outside the pool); rule C is not cancelled in that case. So a party cannot "empty" the pool with its own sham disputes: the 90-day challenge gives way to the pool minimum. Before the deal (§3) only one thing is shown: whether the pair has an objective pool without a common beneficial owner, or a reserve provider;
   - **the decider's identity is not disclosed to the parties before the decision**.
   - **Against "reverse" bribery** (the decider approaches a party): the decider receives the case with the parties under pseudonyms and without company names. The decision contains a finding on each sampled item with a reference to the evidence. The bank sends a random 5 % of decisions to another decider for re-review; a discrepancy means a penalty, a repeat means removal from the pool.
   - **Contact about the case.** A party loses the case over a contact only if the contact with the decider is proven to come from **that party's authenticated channel** (a signature with its key, its mail domain with DKIM, its account); in that case the decider is also removed if it did not report the contact. An anonymous or unconfirmed contact **is recorded in the case but does not lead to replacing the decider**, unless the decider itself declares that it has lost objectivity; there is at most one replacement on this ground per case, after which the case continues with the current decider. So nobody can frame the other side or drag the dispute out endlessly with anonymous letters. The decider must report any contact within 24 h.
   - **Re-review at the loser's request** (against a marker in the data and collusion with the decider): within 5 business days the loser may pay the re-review fee F3 (2 × the base of F); the case goes to **two new** deciders from the pool without the first one (if the pool is small, the reserve provider). The result is the majority of the three decisions on each sampled item. The period of §7C.9 runs from the final decision.
   - **Every new selection** (re-review, replacement for lateness, replacement at the decider's own declaration) follows the same scheme: a new bank salt `s'`, its hash on-chain and a round number computed in advance are fixed when that step opens, and `s'` is revealed after the final decision. The earlier salt is not used for a new selection.
   - **Payment in a re-review:** F3 pays the two new deciders and is not refunded to the requester out of the other side's money. If the result changed, the first decider pays a penalty under its contract with the bank, and from it the bank refunds F3 to the requester. The first decider's fee comes from the fee of the side that loses under the **final** decision. The bank returns the winner's fee (p. 5) only after the 5 business days for a re-review have passed or the re-review has finished.
7. **Sample:** if there are more than k disputed items, the decider checks a random sample of k drawn by the sample round fixed in advance (p. 6, `H(drand ‖ s)`), which comes out after the appointment, and extends the result to the whole as a share (like statistical sampling with extrapolation in Medicare overpayment audits, CMS Program Integrity Manual ch. 8) [L].
8. The decider decides within 48 h **of the sample round** (p. 6), when the sampled items are known. **Late** → hold and replacement of the decider (replacing an arbitrator is provided for in the AAA/ICC rules [L]; proposed precedent: ADRP §10.2 `hold_and_reassign`); a penalty under the contract with the bank, and repeated lateness means removal from the pool. The money stays in escrow (the term is long, §8.3). Nobody wins by default.
9. **Execution of the decision**, as in UDRP para. 4(k): the bank executes the decision after 10 business days unless a party has confirmed filing a lawsuit; if one has, the money stays in escrow until the court's decision or an agreement between the parties.
10. **Dispute limits:** no more than 2 C claims of one buyer against one seller **under review** at the same time; per seller, no more than `max(3, 5 % of its open rule-C deals)` under review. The limits cap review, **not filing**: a C claim can be filed in the window of any deal, and D is held in escrow at once. **Negotiation and the buyer's fee deadline (pp. 3–4) run from filing**, with no queue; only the decider stage waits in the queue, once both fees are in. So splitting a delivery into many C deals does not strip the buyer of protection on the later deals, and D cannot be frozen through the queue without a fee. Queued claims appear in the seller's open statistics as open C claims. Only claims of **independent** buyers count toward the seller's limit (§9): the seller cannot fill its own queue with sham buyers to delay honest ones.
    - If the B part of a C deal is unavailable (the §7B slots are taken), the deal does not by itself count as meeting `require_b_for`: it needs human confirmation, like "B with no claims" (§2).
11. **Legal nature:** the decider's decision is a condition for **executing the instruction to the bank** under the escrow agreement (OZ §548), as in UDRP: the registrar executes the panel's decision, and the way to court stays open. The sale contract does not change by this (§1749–1750 do not apply to it). It is not arbitration under zákon 216/1994 Sb. and not a decision of the bank [L].

## 8. Payouts and escrow terms

### 8.1 AuthCaptureEscrow constraints (checked against the source code)
- capture: only the operator, and only before `authorizationExpiry`; void: only the operator, at any time, **once, the whole remainder**; reclaim: only the payer, after expiry; refund: the operator, before `refundExpiry`, through a collector (OperatorRefundCollector, with the operator's money); capture: no more than `maxAmount`.
- The payee is fixed in PaymentInfo. The fee is taken at capture, within `[minFeeBps, maxFeeBps] · amount`, and **is deducted from the captured amount**; with `feeReceiver = 0` the operator chooses the fee receiver, so we set the bank's clearing address.
- One PaymentInfo cannot be authorized twice (`PaymentAlreadyCollected`).
- Authorization through ERC3009PaymentCollector needs the payer's signature (`receiveWithAuthorization`, `validBefore = preApprovalExpiry`); for the buyer the bank makes it as the key holder, for the seller only the seller itself.
- `paymentState` gives `capturableAmount` and `refundableAmount` (= captured − refunded), but not "paid out".
- If the payer is on the EURC blacklist or frozen in tCZK, void and reclaim will revert, so the payer must not be frozen while the escrow is open.
- Result: all captures are cumulative, **all refunds to the buyer go in one final void** per PaymentInfo (with an open C, the split-off of D, §8.2); item replacement and "accept anyway" happen before the final void.

### 8.2 Schedule

| Moment | A | B | C (T2) |
|---|---|---|---|
| Delivery accepted, autocodes | — (wait for the delivery deadline or human confirmation) | capture `(1 − X) · P` | nothing |
| Delivery deadline / confirmation | capture P, final void | — | — |
| Window end | — | capture of the undisputed remainder (P minus B refunds), final void | capture P minus B refunds minus D; if there is no C claim, or D is already decided for refund (§7C pp. 3–4): final void; if C is open: **split-off of D** (below) |
| C dispute decision (after 10 business days under §7C.9) | — | — | capture of the seller's share of D, final void |

C fees are separate authorizations, captured at once to the bank's clearing address; the refund to the winner goes by transfer (§7C.4–5). The buyer gets the refund for failed items at the end. This is stated in the terms.

**Split-off of D.** So that the undisputed refund (failed items, B refunds) does not wait for the dispute, the queue or a court, at window end with an open C the bank, after capturing the undisputed part, makes a void and **at once a new authorization for D only** (a new PaymentInfo, salt `keccak(deal_id ‖ n+1)`, the same terms of §8.3 from the current moment); the seller repeats `verify_escrow`. The new authorization comes after the void is confirmed in a finalized block, as with a rollover (§8.4): for those blocks D returns to the buyer's wallet under the bank's key and the mandate reservation. The dispute, the queue and a court keep only D in escrow.

### 8.3 Terms
- **The escrow term is long, and the bank closes the deal itself** (capture and the final void). Since the operator can void at any time, a long term does not weaken the buyer's protection, and the money does not pass through the buyer's wallet in the middle of a dispute.
  - T1: `authorizationExpiry = delivery deadline + window + 30 days`.
  - T2: `authorizationExpiry = delivery deadline + window + 120 days` (negotiation, fees, deciders, the queue, the 10 business days of UDRP, an AML suspension under §20 with extensions).
- `refundExpiry = authorizationExpiry + 30 days` (for bank errors only).
- Deal deadlines are set in business days by the Czech holiday calendar and converted to timestamps at authorization.

### 8.4 Payout accounting, failures and exceptions
- **Accounting at the deal level:** "paid out" = Σ `PaymentCaptured.amount` (gross amounts) **over all of the deal's PaymentInfos whose payee is the seller** (C fee authorizations, whose payee is the bank's clearing address, are not included), read in finalized blocks. Each next capture and any new authorization are computed from this sum.
- **Rollover is an exception only** (if a dispute or a court case runs past 120 days): for the current `capturableAmount`; a new PaymentInfo with the salt `keccak(deal_id ‖ n+1)`; the seller is notified and repeats `verify_escrow`. Today the operator is the bank's key (an EOA), and only `paymentInfo.operator` can call `void`, so a rollover is **not atomic**: the new authorization comes only after the void is confirmed in a finalized block. An atomic variant (void and authorize in one transaction) is possible only if an operator contract is set as the operator in PaymentInfo from the start; that is on the roadmap. While a rollover is not atomic, the money returns to the buyer's wallet for a few blocks; this is the exception to §0 p. 4: the bank holds the wallet key and the mandate reservation stays, so neither the buyer nor its agent can spend that money.
- One operator key in an HSM with a backup, one transaction queue, a second RPC node, a standby node without a key. Idempotency key `deal:hash:capture:n`.
- **Capture failure:** retries; the term is long, so expiry is an edge case. If it does expire: a rollover for (amount due − paid out on the deal) and a capture. If the buyer has no funds, the bank pays the seller the shortfall from its own funds and gets a claim against the buyer. There is no double payout.
- **Circuit breaker:** if **infrastructure** failures affect more than N deals or an amount above M, the bank suspends new authorizations. Failures caused by sanctions and freezes do not count.
- **Sanctions against the seller in the middle of an escrow:** no capture; final void; the buyer's obligation to pay is suspended; the FAÚ is notified; then the procedure of Reg. 269/2014 applies [L].
- **AML suspension** (zákon 253/2008 Sb. §20: 24 h, an extension by the FAÚ, a further extension after a criminal complaint): the money stays in escrow and nothing happens on-chain (the term is long); after the decision, capture or void. For all deals the page shows deadlines as "up to N business days", so a suspension looks no different from an ordinary deal (§38, the ban on tipping-off).

## 9. Monitoring and open statistics

Shares are smoothed: `(events + 1) / (observations + 20)` over 30 days. Thresholds are absolute, for each metric separately. **Independent** and **established** buyers share no common attributes with the seller or with each other (EvSM, beneficial owner, IP, keys).
- **Both the numerator and the denominator** of each metric are counted only over deals of **independent** buyers, and the "lost C" and "B claims" thresholds over deals of **established** buyers, weighted by amount. Hundreds of the seller's own claim-free deals through a sham company do not dilute the quality signals: either they do not enter the denominator (the link is found), or they weigh as much as one non-established buyer.

**The seller's open statistics** (visible to the buyer's agent before the deal):

| Metric | How it is computed | What it means |
|---|---|---|
| Fulfilment rate | Delivered / ordered | Delivery reliability |
| Item rejection rate | Share of items with `CRITERIA_FAIL`, `SCHEMA_FAIL`, `DUPLICATE`, `REGISTRY_CONTRADICTION` | Care, **not quality**: the predicates are public, so the seller can filter in advance |
| Share of deals under rules B and C | — | The seller's readiness to answer for quality |
| B claim frequency | Number of independent buyers who filed B, to the number of deals with B; without reasons | **Quality signal** |
| Lost C disputes | To the number of deals under rule C | **Quality signal** |
| Established buyers | Number of buyers older than 90 days with deals at ≥ 3 different sellers for a sum ≥ a threshold | Protection against inflated statistics |

| Who | What | Threshold | Consequence |
|---|---|---|---|
| Seller | Item rejection rate | > 5 % | Rules B/C only |
| Seller | Lost C | > 10 % of deals with C | T2 only |
| Seller | Fulfilment rate | < 80 % | Warning in the statistics |
| Seller | B claims from ≥ 3 independent buyers | Internal threshold | Check for fraud and collusion, not for quality |
| Buyer | B claims | §7B | B unavailable (visible before the deal) |
| Buyer | Lost C | ≥ 3 in 30 days | Fee F ×2 within the mandate budget |
| Pairs | Reciprocal deals, common beneficial owner, price out of line with history | AML rules | To compliance, without notifying the client |

## 10. Economics

| Item | Who pays | How much (proposal) |
|---|---|---|
| Deal fee | The seller, at capture (`feeReceiver` = the bank's clearing address) | T0 0,2 % (monthly), session 0,2 %, T1 0,8–1,2 %, T2 1,5 % |
| C dispute fee | The loser, to the decider, through the bank | 50 € + 2 € per item, ≥ 10 % D, ≤ 1000 € |
| Penalty for a late decider | The decider, under its contract with the bank | Per contract |
| Bank errors | The bank | As incurred, D+1 |
| Infrastructure failure | The bank | The shortfall on the deal, with a claim against the buyer |
| Gas | The bank (tCZK: the relayer) | Base: fractions of a cent; the calculation at the current gas price is in the economics panel on `/story` |

- **The bank** earns the fee at capture and pays back through volume; it already has KYB and the registries.
- **The decider** always gets a fee; the fee grows with the number of items, and the check uses a sample. Benchmark: at Upwork the arbitration fee is hundreds of dollars per party.
- **The seller**: access to buyers whose mandate requires protection; the money is locked before delivery; under A, payout at the delivery deadline. Comparison: the card chargeback and Escrow.com.
- **The buyer**: protection against non-delivery, short delivery, mismatch with what was stated and with the registries, deception of the agent and payment to the wrong party, with no people involved; against bad data with dated evidence, C; against the rest, B and the statistics.

## 11. A path a person can follow

- Every event (delivery, autocodes, claim, negotiation, fees, challenges, appointment of the decider, decision, the period for a lawsuit, capture/void) is an entry in the hash-chained journal.
- The deal page: the terms in plain language, what arrived, which items failed and why, where the money went, links to transactions, what happens on silence and by when ("up to N business days").
- For the client, neutral texts in AML cases; the full texts only for compliance.

## 12. Law

| Position | Basis |
|---|---|
| Escrow is a contractual úschova / agency role of the bank; the terms are accepted by the seller in the offer and in the service terms, and by the buyer in the mandate | Contract |
| Release of accepted items only, against secured payment; checking compliance with the terms; partial payments; the buyer's consent to discrepancies | URC 522 Art. 7; UCP 600 Art. 14, 16, 31 (analogies) |
| The C dispute decision is a condition for executing the instruction to the bank, executed after 10 business days if there is no lawsuit; the sale contract does not change; not arbitration; the way to court stays open | OZ §548 [L]; UDRP para. 4(k) (analogy); zákon 216/1994 Sb. |
| The mandate is signed with SCA; the agent's payments within the mandate fall under RTS Art. 17 with ČNB's consent, the fallback grounds are Art. 13 and Art. 16 | PSD2 Art. 97; RTS 2018/389 Art. 13, 16, 17 [L] |
| A payment outside the mandate is an unauthorised transaction, refunded no later than the end of the next business day. In B2B, Art. 72 and 74 can be opted out of, but not Art. 73 | PSD2 Art. 61(1), 73; zákon 370/2017 Sb. |
| Refunds only to the payer | AuthCaptureEscrow: void/reclaim/refund to the payer |
| tCZK is a deposit (MiCA Art. 2(4)); the qualification follows the features in the EBA report and is not automatic | MiCA; EBA, report on tokenised deposits (12.2024) |
| EURC is an EMT: a transfer is a payment service under PSD2 (the transition period under the EBA letter on the PSD2–MiCA interplay ended on 1.03.2026); holding and transferring it for clients are crypto-asset services, and the bank notifies ČNB | MiCA Art. 60 [L] |
| AML suspension and the ban on tipping-off | zákon 253/2008 Sb. §20, §38 |
| DORA: the ICT providers under contract are the RPC providers; the Base network and the escrow contract are a dependency without a contract, kept in the risk register with an exit plan | Reg. 2022/2554 |
| GDPR roles: processor for delivery, controller for evidence and AML; human review for sellers who are natural persons | GDPR Art. 4, 22, 28 [L] |
| Data storage by a subsidiary under an outsourcing agreement | DSA; EBA Guidelines on outsourcing [L] |

**Pilot with a bank (legal part):** the Travel Rule (Reg. 2023/1113): for the bank's clients the parties' data is already there, for external sellers it is the next stage; the prudential treatment of tokenized deposits on a public chain (the Basel Committee's crypto-asset standard); tax documents (the seller issues the daňový doklad; reverse charge for foreign sellers); adhesion terms with the rule "silence = accepted" (OZ §1798–1801) [L]; hosting for a banking group under zákon 21/1992 Sb. [L]; the status of escrowed funds if the buyer goes bankrupt (zákon 182/2006 Sb.) [L]; deposit insurance for tCZK while it is locked in escrow [L].

**Next stages:** sellers at other banks (EURC for external ones, an interbank rulebook); non-custodial payers (T0 and sessions); a qualified eIDAS seal over the manifest hash.

## 13. Novelty

Not ours:
- **Deployed:** delivery against payment (documentary collection, URC 522); examination of documents for compliance, partial payments and consent to discrepancies (letter of credit, UCP 600); data matching against a bank's payment obligation (BPO, ICC URBPO); an inspection window and negotiation before arbitration (Escrow.com); automatic payout on silence, fees from both sides, external arbitration (Upwork, AAA); execution of a panel's decision with a window for a lawsuit (UDRP); the merchant's rules for automatic refunds and documents by reason code (Visa).
- **Proposed:** intent and cart mandates signed by a person (Google AP2); acceptance criteria of the cart mandate, deterministic checks, tiers, fees, hold and reassignment (ADRP, draft); Commerce Payments escrow (Coinbase); a pluggable arbiter (x402r); lockup and the recipient's debt (Circle Refund Protocol, unaudited); partial refunds and per-seller risk control (Agentic Settlement Protocol, arXiv 2609.02208); usage-based escrow (Fortytwo); random selection (Kleros, drand).

Ours (we did not find it in the analogues):
1. **A regulated bank that holds the payer's account executes the deal:** the mandate, the checks, the release and the payment sit in one regulated perimeter; the agent has no money keys.
2. **Item by item and automatically at the moment of delivery:** the bank itself checks each item against the terms a person confirmed and against the public registries, releases only the accepted ones and pays for them, with no people involved.
3. **KYB against Czech registries** (ARES, ISIR, the VAT register, EvSM) before payment and in the predicates.
4. **tCZK** working with the escrow: we found no public analogue in the Czech Republic (tokenized deposits as a class are known).

## 14. Demo configuration

**Demo scale.** On the testnet every threshold and fee in euro is divided by 100, and deadlines are accelerated (`DEMO_MONEY_DIVISOR`, `DEMO_TIME_DIVISOR` in `bank/config.py`); the deal page says so, and so do we on stage. The mechanics do not change, only the numbers:

| | Production | Demo (÷100) |
|---|---|---|
| T0 / T1 / T2 by `tier_sum` | ≤ 20 / (20; 500] / > 500 € | ≤ 0,20 / (0,20; 5] / > 5 € |
| C fee | 50 € + 2 € per item, ≥ 10 % D, ≤ 1000 € | 0,50 € + 0,02 € per item, ≥ 10 % D, ≤ 10 € |
| Mandate | — | 10 € per deal, confirmation above 6 €, 30 € a month |
| B window / C negotiation / 10 business days | 30 min – 14 days | seconds (30 min → 20 s) |

Example: 20 items for 5 € is T1, 10 items for 8 € is T2 (above 6 €, with human confirmation).

**Repeatable runs** (rehearsal → show):
- each episode has its own seller, so `tier_sum` over 24 h does not mix episodes; before a run the mandate and the counters are reset (`scripts/seed.py`, `scripts/demo_seed.py`);
- the pool is 3 demo deciders; the drand draw is real (quicknet rounds), and the decision on the sample is entered on the decider's page;
- the 90-day objective challenge is switched off in the demo, so that a repeat run goes with the same pool.

The show script: [DEMO.md](DEMO.md).

## 15. Where it is in the code

| Module | What it implements |
|---|---|
| `bank/mandate.py` | Mandate limits and versions; `dispute_floor`, `tier_sum`, the rule order A < B < C, `require_b_for`, `require_c_above_eur`, the shared daily cap on unprotected payments, the C fee budget |
| `bank/deals.py` | Task terms with a hash (`task_terms`); `pay(deal_id)`: matching against the offer and the seller's wallet, the task predicates verbatim, the mandate's requirements, budget reservation, the lock in escrow |
| `bank/receipts.py` | I-JSON/NFC, JCS, the hash tree with test vectors (`fixtures/merkle_vectors.json`), the EIP-712 manifest signed with the seller's key, the key registry, delivery versions, release of accepted items only, "accept anyway" |
| `bank/snapshots.py` | Source snapshots by `source` templates: the bank builds the URL, exact host, address check after DNS, limits, the fair queue; the "field → path in the snapshot" mapping |
| `bank/verifier.py` | The predicate engine of §6, AND only (RE2, schema limits, `registry_active` bound to ARES, `source`), autocodes. No AI judge |
| `bank/disputes.py` | Rules A < B < C and the payout schedule of §8.2: cumulative captures, split-off of D, one final void |
| `bank/claims_c.py` | The C claim: admissibility at filing, negotiation, fees to the clearing address, execution of the decision |
| `bank/deciders.py` | The decider pool (minimum 3, challenge order), challenges through hash commitments, the draw `H(drand ‖ bank salt)`, the sample, the parties' pseudonyms |
| `bank/escrow.py` | AuthCaptureEscrow: `feeReceiver` = the bank's clearing address, `minFeeBps = maxFeeBps` = the tariff of the tier and rule, salt `keccak(deal_id ‖ n)`, the terms of §8.3 |
| `bank/config.py` | Tariffs, windows, the demo scale of §14 |
| `seller/app.py` | Manifest signing key, delivery through the bank, `verify_escrow` (payee, `feeReceiver`, tariff, deadlines, salt) |
| `agents/buyer.py` | `fetch_delivery` (accepted items only), `file_claim` by ids only |

**Pilot with a bank:** micro-payment sessions with vouchers (§4); the seller's open statistics and monitoring (§9); linked sellers from an EvSM extract; rollover under §8.4; signing the mandate with SCA, and keys in the bank's HSM.

## Appendix. Sources

**Deployed practice**
- ICC: URC 522 (documentary collection), UCP 600 (letter of credit), URBPO: https://library.iccwbo.org/content/tfb/RULES/tfb-urc522-rules.htm, https://library.iccwbo.org/content/tfb/RULES/tfb-ucp600-rules.htm
- UDRP para. 4(k): https://www.wipo.int/export/sites/www/amc/en/docs/UDRPflowchart.pdf
- Upwork (7 days to agree to and pay for arbitration, fees, automatic payout on silence): https://support.upwork.com/hc/en-us/articles/14044146250259, https://gigradar.io/blog/upwork-payment-protection-fixed-price
- Escrow.com (inspection period, negotiation and arbitration): https://www.escrow.com/learn-more/how-escrow-works/escrow-dispute, https://escrow.com/inspection-period
- Visa RDR, CE 3.0, VAMP: https://usa.visa.com/solutions/post-purchase-solutions/merchants.html, https://verifi.review.visa.com/compelling-evidence-ce30.html, https://www.chargebackgurus.com/blog/visa-announces-new-change-to-vamp-rules
- EBA Q&A on RTS Art. 17: https://www.eba.europa.eu/single-rule-book-qa/qna/view/publicId/2018_4400
- plati.market: https://plati.market/refundpolicy/
- drand: https://drand.love

**Proposed precedents**
- ADRP: https://www.ietf.org/archive/id/draft-stone-adrp-01.html
- AuthCaptureEscrow, ERC3009PaymentCollector: https://raw.githubusercontent.com/base/commerce-payments/main/src/AuthCaptureEscrow.sol, https://raw.githubusercontent.com/base/commerce-payments/main/src/collectors/ERC3009PaymentCollector.sol
- Circle Refund Protocol: https://github.com/circlefin/refund-protocol
- Fortytwo x402Escrow: https://github.com/Fortytwo-Network/fortytwo-x402Escrow
- Agentic Settlement Protocol: https://arxiv.org/abs/2609.02208
- Kleros: https://kleros.io

**Law**
- OZ 89/2012 Sb.: https://www.zakonyprolidi.cz/cs/2012-89
- zákon 253/2008 Sb.: https://www.zakonyprolidi.cz/cs/2008-253
- EBA letter on the PSD2–MiCA interplay: https://eba.europa.eu/sites/default/files/2025-07/bde8c438-bb3f-4343-a158-f42b7afc9650/EBA%20Op%202025%2008%20%28No-Action%20letter%20on%20PSD2-MICA%20interplay%29.pdf
