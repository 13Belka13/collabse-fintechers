# REFERENCE — verified facts and snippets (checked 2026-10-18)

[V] = verified live or in code; [S] = from docs; [U] = unverified.

> **`docs/SPEC.md` is the master document.** This file only holds what we verified ourselves
> (addresses, live tx links, working snippets, lessons). Where they differ, SPEC wins — except
> for facts marked [V] here with a date later than SPEC's statement.

## 1. Chain, tokens, contracts (Base Sepolia, `eip155:84532`)

| What | Value |
|---|---|
| RPC (public) | `https://sepolia.base.org` · Tatum: `https://base-sepolia.gateway.tatum.io` (header `x-api-key`) |
| USDC | `0x036CbD53842c5426634e7929541eC2318f3dCF7e` · name `USDC` · version `2` · 6 decimals [V] |
| EURC | `0x808456652fdb597867f38412077A9182bf77359F` · name `EURC` · version `2` · 6 decimals [V] |
| AuthCaptureEscrow v1.1 | `0xf96815976523E00e65Be8f34cA5e64b4f41EB19c` [V] |
| ERC-3009 collector | `0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88` (its `authCaptureEscrow()` = escrow above) [V] |
| Refund collector | `0x7a03443724d14798c4AB4622F1DAAcA761Fea486` [S] |
| Token store of our operator | `0xd2b0C305F62427414e9624a2beD72a3792e15cf8` = `escrow.getTokenStore(OPERATOR_ADDR)` [V] |
| Public facilitator | `https://x402.org/facilitator` — `exact`, `upto`, `batch-settlement` on 84532; **no `auth-capture`** [V] |
| Explorer | `https://sepolia.basescan.org/tx/<hash>` |
| Faucets | Circle `faucet.circle.com` (USDC/EURC, 20 per 2 h, no account) [V]; Base Sepolia ETH: Coinbase CDP faucet (account needed) |

Test wallets (keys in `.env` only): buyer `0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140` (20 USDC,
≈20 EURC), seller `0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1`, operator
`0x0469ceC178789072bc61435835D0BB38214D4e53` (≈0.0002 ETH), bank
`0xD23DB197CC1294b049AeDf0d9ea1BA216691de48` (≈0.0003 ETH). Buyer also holds 845.00 tCZK.

## 2. x402 Python SDK 2.25 [V]

Seller (FastAPI middleware) — price in EURC as an explicit `AssetAmount`:
```python
from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact import ExactEvmServerScheme

server = x402ResourceServer(HTTPFacilitatorClient({"url": "https://x402.org/facilitator"}))
server.register("eip155:84532", ExactEvmServerScheme())
routes = {"GET /orders/*": {"accepts": {"scheme": "exact", "payTo": SELLER_ADDR, "network": "eip155:84532",
          "price": {"amount": "10000", "asset": EURC, "extra": {"name": "EURC", "version": "2"}}}}}
mw = payment_middleware(routes, server)
@app.middleware("http")
async def x402_mw(request, call_next): return await mw(request, call_next)
```
Middleware order: verify → run handler → if handler status < 400: settle → release body; settle
failure returns 402 (no charge if the handler fails) [V by reading code].

Buyer client — **EURC/tCZK must be allowed explicitly** (default allows only USDC-like defaults):
```python
from x402 import x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

client = x402Client()
client.set_spend_controls({"allowed_assets": [
    {"network": "eip155:84532", "asset": EURC, "max_amount_per_payment": "1000000"}]})  # atomic units
client.register("eip155:*", ExactEvmScheme(signer=EthAccountSigner(Account.from_key(BUYER_KEY))))
async with x402HttpxClient(client, timeout=60) as http:
    r = await http.get(url)   # header "payment-response" = base64 JSON with "transaction"
```
Known flake [V]: public facilitator sometimes returns `invalid_exact_evm_transaction_failed` with
`replacement transaction underpriced` (its own nonce clash). Funds are not moved. Retry after a
few seconds — 3/3 retries succeeded. Build retry + idempotency in from the start.

**One facilitator per (network, scheme)** [V, `x402/server_base.py` `initialize()`:
`_facilitator_clients_map[network][scheme] = client`, first registered wins]. EURC and tCZK are
both `exact` on `eip155:84532`, so one seller cannot send EURC to x402.org and tCZK to the bank →
**the bank facilitator serves both**; x402.org is only an EURC fallback.

Own facilitator (bank, both tokens) [V by reading code; not run end to end]:
```python
from x402 import x402Facilitator
from x402.mechanisms.evm import FacilitatorWeb3Signer
from x402.mechanisms.evm.exact import register_exact_evm_facilitator
from x402.schemas.hooks import AbortResult

fac = x402Facilitator()
register_exact_evm_facilitator(fac, FacilitatorWeb3Signer(private_key=BANK_KEY, rpc_url=RPC_URL), networks="eip155:84532")
fac.on_before_verify(lambda ctx: AbortResult(reason="SANCTIONED_PARTY") if blocked(ctx) else None)
# ctx.payment_payload / ctx.requirements carry from/to/value/asset; expose verify/settle/supported over FastAPI
```
Hook type [V]: `BeforeVerifyHook = Callable[[VerifyContext], Awaitable[AbortResult | None] | AbortResult | None]`
(sync or async both fine); returning `AbortResult(reason=...)` raises `PaymentAbortedError` → verify fails.
`register_exact_evm_facilitator(facilitator, signer, networks, eip6492_allowed_factories=None, simulate_in_settle=False)`.
**Settle does not run the verify hooks** [V, `facilitator_base.py` `_settle_core`: only
`_before_settle_hooks`, then `_settle_v2`] → register the same screening on `on_before_settle` too.

## 3. AuthCaptureEscrow from Python [V — full cycle run live with EURC and tCZK, see §8a]

Working code: `prep/spikes/escrow_spike.py` (`check`, `simulate`, `run`). Key facts:
- `PaymentInfo(operator, payer, receiver, token, maxAmount:uint120, preApprovalExpiry:uint48,
  authorizationExpiry:uint48, refundExpiry:uint48, minFeeBps:uint16, maxFeeBps:uint16, feeReceiver, salt:uint256)`.
- Buyer signs EIP-712 **`ReceiveWithAuthorization`** on the token domain (name/version from §1),
  `to = ERC-3009 collector`, `value = maxAmount`, `validAfter = 0`, `validBefore = preApprovalExpiry`,
  **`nonce = escrow.getHash(paymentInfo with payer = 0x0)`**.
- Operator calls `authorize(pi, amount, COLLECTOR, signature)`; then `capture(pi, amount, 0, 0x0)`
  (partial allowed: amount ≤ capturable); `void(pi)` returns the remaining capturable to payer;
  `reclaim(pi)` by payer after `authorizationExpiry`; state via `paymentState(getHash(pi))` →
  `(hasCollectedPayment, capturableAmount, refundableAmount)`.
- Only `paymentInfo.operator` may authorize/capture/void (non-operator authorize reverts) [V].

## 4. DepositToken tCZK (route B) [V on in-memory chain and on Base Sepolia, see §8a]

Deployed instance: `0x6a99C9e95DD220172d07D5f730f2e82B466D4B67` (bank = `BANK_ADDR`). Reuse it;
`prep/spikes/deploy_tczk.py` is idempotent (uses `TCZK` from `.env` if set).

`prep/contracts/DepositToken.sol` + `prep/spikes/deposit_token_test.py`. ERC-20 + bank-only
mint/burn + allowlist on every transfer + EIP-3009 `transferWithAuthorization` /
`receiveWithAuthorization` with both `(v,r,s)` and `bytes signature` overloads (like USDC v2).
EIP-712 domain: name = constructor arg (e.g. `Demo Bank tCZK`), version `1`, chainId, contract.
Verified: mint, signed transfer, replay rejected, non-client rejected, non-bank mint rejected,
tampered amount rejected, **and a payload created by the official x402 SDK settles on it**.
Compile: `solcx.install_solc('0.8.26')`, `solcx.compile_source(src, output_values=["abi","bin"], solc_version="0.8.26")`.
Local chain for tests: `Web3(EthereumTesterProvider())` (install `py-solc-x eth-tester py-evm`;
on Windows avoid `web3[tester]` — it needs a C build of `safe-pysha3`).

## 5. Data sources [V]

- ARES by IČO: `GET https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}` →
  `ico, obchodniJmeno, datumVzniku, sidlo.textovaAdresa, ...` (e.g. 45317054 = Komerční banka).
- ARES search: `POST .../ekonomicke-subjekty/vyhledat` body `{"obchodniJmeno": "logistika", "pocet": 3}`
  → `pocetCelkem`, `ekonomickeSubjekty[]`. Filter by city via `sidlo.kodObce` (Brno = 582786) [U on filter].
- EU consolidated sanctions CSV (≈25 MB, `;`-separated, regenerated regularly — 2026-10-15):
  `https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw`.
  Download once at start, match on `NameAlias_WholeName` (normalized).
- Apify: use credits (API token; hackathon coupon applied → 105 USD monthly limit) [V]. Apify's
  hackathon guide asks teams to keep the repo public, highlight Apify in the demo and tag @apify
  on LinkedIn/X; it recommends `npx skills add apify/agent-skills`, the MCP server (mcp.apify.com)
  and `docs.apify.com/llms.txt`; any Store actor page + `.md` gives an agent-friendly description.
  Apify x402 is **mainnet only** (Base, Solana; min 1 USD) — not usable
  under the testnet rule [V]. Pitch line: "Apify already accepts x402 on mainnet".

## 6. Tatum [V — key works; base-sepolia INCOMING_FUNGIBLE_TX subscription created and deleted on 2026-10-18]

Subscription: `POST https://api.tatum.io/v4/subscription`
`{"type":"INCOMING_FUNGIBLE_TX","attr":{"address":SELLER_ADDR,"chain":"base-sepolia","url":WEBHOOK_URL}}`.
HMAC: set secret via `PUT /v4/subscription` `{"hmacSecret":...}`; verify header `x-payload-hash` =
base64(HMAC-SHA512(body without spaces)). Dedupe by event id. Fallback: poll balances via Tatum RPC.
Webhook needs a public URL (tunnel or webhook relay) — decide at the venue.

## 7. AWS [S]

AgentCore Payments exists (x402, per-session caps) — we position as complementary: they cap *how
much*, we decide *whether the seller earned it* and *who may be paid*.

Claude on Bedrock — checked against Anthropic docs on 2026-10-18 [S]:
- `pip install "anthropic[bedrock]"`; client `AnthropicBedrockMantle(aws_region=...)` (recommended;
  `AnthropicBedrock` = legacy InvokeModel path). Model IDs `anthropic.claude-opus-5-5`,
  `anthropic.claude-haiku-5-5`. Region/availability: check in the team account at the Toolbox.
- [V 2026-10-19, docs] Opus 5.5 / Haiku 5.5 / Sonnet 5.5 are "See Access" (not open to every
  account). Open to all: Fable 5.1, Fable 5, Opus 4.8, Opus 4.7, Sonnet 5, Haiku 4.5. Fallbacks:
  `anthropic.claude-opus-4-8` (default effort `high`) and `anthropic.claude-haiku-4-5` — Haiku 4.5 is
  not in the effort docs' supported models, so send no `output_config.effort` to it.

x402 route keys — [V 2026-10-19, SDK 2.25 `_parse_route_pattern`]: `GET /data/:offer_id` and
`GET /data/[offer_id]` match `/data/abc123`; `GET /data/{offer_id}` does not (escaped literally) →
the request passes through unpaid. `PaymentOption.price` accepts `DynamicPrice` =
`Callable[[HTTPRequestContext], Price]`; context fields `path`, `method`, `route_pattern`.

Prep tCZK EIP-712 name is `"Demo Bank tCZK"` (version "1", decimals 2), not `"tCZK Deposit Token"`.
ČNB daily rate file works without a key [V 2026-10-19]: `EMU|euro|1|EUR|24,325` on 16.10.2026.
ARES legal form of ČEZ (IČO 45274649) = `121` [V 2026-10-19].
- **Structured outputs (`messages.parse`, `output_format`) are NOT available on Bedrock** → ask for
  JSON in the prompt, validate with pydantic, one retry with the error, then a safe default.
- Opus 5.5: do not send `thinking` disabled/`budget_tokens` (400); effort via
  `output_config={"effort": ...}`; forced `tool_choice` → 400; no assistant prefill.
- Tools: `additionalProperties: false`, optional fields allowed; don't rely on `strict` on Bedrock.
  `beta.messages.tool_runner` exists but is unverified on Bedrock → manual `stop_reason` loop.
- `stop_reason == "refusal"` exists (HTTP 200). Top-level `cache_control` works on the Mantle path.
  No server-side MCP connector on Bedrock → call Apify via `apify-client` inside tools.
- SDK is built on `httpx2`; don't pass `httpx` objects into the Anthropic client.

## 8a. Live testnet runs on 2026-10-18 [V] — and the lessons

| Run | Result |
|---|---|
| Escrow, EURC: lock 0.02 → capture 0.015 → void 0.005 | [authorize](https://sepolia.basescan.org/tx/0xbfe16c01138fcde5d2182a6324536e79fb403579f1d36d7b4f9b3f37cd74caf7) · [capture](https://sepolia.basescan.org/tx/0x997c9efd924878d4201f44d4b8d0284a3da0518d994bca65a69b74f8577c0532) · [void](https://sepolia.basescan.org/tx/0xb91119334be9570dcdfdb7c700185e3e1a12f7ef8bb6e4332fc8edf122cec42b) |
| tCZK deployed by BANK | [`0x6a99C9e95DD220172d07D5f730f2e82B466D4B67`](https://sepolia.basescan.org/address/0x6a99C9e95DD220172d07D5f730f2e82B466D4B67); allowlisted buyer, seller, collector, operator token store; [minted 1000.00 to buyer](https://sepolia.basescan.org/tx/0xde808c56a16dc7a224864e4ae9a472638ed93d5f3658db22233fe2072b73445a) |
| Escrow, tCZK: lock 200 → capture 150 → void 50 | [authorize](https://sepolia.basescan.org/tx/0x462fc8182743aa1f35106f624fc4bb7309024ac9656b44d566ada11ec49e3e16) · [capture](https://sepolia.basescan.org/tx/0x996dc31c120dbd87508c97509f499692ed679492c36f18cfcf74a3f164ecafc3) · [void](https://sepolia.basescan.org/tx/0xf0aab7a48e57e11e3c6cedac047a02a57765825ea7789b5edff15aa72b07c40a) |
| x402 SDK-signed 5.00 tCZK, settled by the bank | [tx](https://sepolia.basescan.org/tx/0x1f947cf94b2657c717c71517d6bcab1a20fbf71816d31f3d5e3c345caaaa87d2). Balances after: buyer 845.00, seller 155.00 tCZK (consistent) |

Wallet ETH after the runs: operator ≈0.000198, bank ≈0.0003; gas ≈0.006 gwei → hundreds of txs left.
CDP faucet moved: `portal.cdp.coinbase.com/<entity>/onchain-tools/faucet` (0.0001 ETH per claim, 0.1 ETH/day).

Lessons — build these in from the first commit:
1. **Nonces:** the public RPC is load-balanced; `get_transaction_count(addr)` can be stale and the
   next tx fails with `replacement transaction underpriced`. Use `"pending"` and keep a local nonce
   counter per signer (one sender per key, serialised).
2. **Persist PaymentInfo before `authorize`** (`deals.escrow_info_json`). Without it the locked
   funds cannot be captured or voided. Recovery is possible from `PaymentAuthorized` logs
   (`prep/spikes/escrow_recover.py`) but slow.
3. **Reads lag writes:** right after a tx, balances/state/code may read stale for a few seconds
   (we saw state `[False,0,0]` after a confirmed authorize, empty code after a deploy). Read from
   the receipt/events, or retry reads with a short delay; never branch on a stale read.
4. **`eth_getLogs` on `sepolia.base.org`** returns 413 for big ranges: query ≤ 50 blocks per call
   or use the Tatum RPC.

## 8. Rules that touch the repo [V]

One track per team. Tag `hackathon-start` at kickoff; README section "Built during the hackathon";
pre-existing work disclosed in advance by email and listed in README; AI assistants allowed and
disclosed; sandbox/synthetic data only; submit repo link before Tue 13:30; private repo OK if
judges have access; tech judge reviews commit history of top teams.
