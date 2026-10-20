# Settings from .env (SPEC §16). Read once at import; tests override via monkeypatch + reload().
import os
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account

ROOT = Path(__file__).resolve().parent.parent
if os.getenv("COLLABSE_NO_DOTENV") != "1":  # tests run hermetic: never the real keys or the live RPC from .env
    load_dotenv(ROOT / ".env")


def _env(name, default=""):
    # python-dotenv already drops inline " # comments"; don't cut values (an HMAC secret may contain '#').
    # An empty value ("TOKEN=" copied from .env.example) means "not set": the default applies.
    return os.getenv(name, "").strip().strip('"') or default


def reload():
    """Re-read the environment into module globals (used by tests)."""
    g = globals()
    g["MOCK"] = _env("MOCK", "1") == "1"
    g["FAULTS"] = {f for f in _env("FAULTS").split(",") if f}
    g["DB_PATH"] = _env("DB_PATH", str(ROOT / "data" / "bank.db"))
    g["NETWORK"] = "eip155:84532"
    g["CHAIN_ID"] = 84532
    g["RPC_URL"] = _env("RPC_URL", "https://sepolia.base.org")
    g["TATUM_API_KEY"] = _env("TATUM_API_KEY")
    g["EXPLORER"] = _env("EXPLORER", "https://sepolia.basescan.org")

    g["AWS_REGION"] = _env("AWS_REGION")
    g["AWS_PROFILE"] = _env("AWS_PROFILE")  # SSO profile (`aws configure sso` + `aws sso login`); not used with an API key
    g["BEDROCK_API_KEY"] = _env("AWS_BEARER_TOKEN_BEDROCK")  # short-term Bedrock API key from the console
    # the hackathon account (AWS Participant Guide): Claude 4.5 via EU inference profiles only, eu-central-1
    g["BEDROCK_CLIENT"] = _env("BEDROCK_CLIENT", "invoke")  # invoke = AnthropicBedrock (InvokeModel), or mantle
    g["MODEL_MAIN"] = _env("MODEL_MAIN", "eu.anthropic.claude-opus-4-5-20251101-v1:0")
    g["MODEL_FAST"] = _env("MODEL_FAST", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    # Bedrock timeouts (bank/llm.py). Each HTTP attempt: connect LLM_CONNECT_S, read per role; retries with backoff
    # on throttling / 5xx / timeouts; the whole call (all attempts, json_call's validation retry included) has a
    # wall-clock budget per stage. They nest inside the HTTP timeouts below: seller model 20 s < bank -> seller
    # /negotiate 30 s < agent -> bank 45 s. A model that does not answer in time = a code-written fallback.
    g["LLM_CONNECT_S"] = float(_env("LLM_CONNECT_S", "5"))
    g["LLM_READ_S"] = {"main": float(_env("LLM_READ_S_MAIN", "60")), "fast": float(_env("LLM_READ_S_FAST", "30"))}
    g["LLM_RETRIES"] = int(_env("LLM_RETRIES", "2"))  # extra attempts after the first
    g["LLM_BACKOFF_S"] = float(_env("LLM_BACKOFF_S", "0.5"))  # 0.5, 1, 2 s ... plus jitter, within the budget
    g["LLM_BUDGET_S"] = {"seller": float(_env("LLM_BUDGET_S_SELLER", "20")), "buyer": 40.0, "criteria": 20.0,
                         "explain": 10.0, "*": float(_env("LLM_BUDGET_S", "60"))}
    g["LLM_FAULT_SLOW_S"] = float(_env("LLM_FAULT_SLOW_S", "8"))  # FAULTS=llm_slow: the model answers this late
    g["NEGOTIATE_TIMEOUT_S"] = float(_env("NEGOTIATE_TIMEOUT_S", "30"))  # bank -> seller /negotiate (model inside)
    g["BANK_HTTP_TIMEOUT_S"] = float(_env("BANK_HTTP_TIMEOUT_S", "45"))  # agent -> bank API (negotiate inside)

    g["CZK_PER_EUR_DEMO"] = int(_env("CZK_PER_EUR_DEMO", "25"))
    g["SCENARIO_PRICE_EUR"] = _env("SCENARIO_PRICE_EUR", "0.10")
    # deal economics panel (bank/economics.py): gas priced at today's Base mainnet eth_gasPrice, read off the money
    # path (10-min cache, 2 s timeout); hermetic tests never call it. Fallback price and the ETH rate are constants.
    g["BASE_MAINNET_RPC"] = _env("BASE_MAINNET_RPC", "" if os.getenv("COLLABSE_NO_DOTENV") == "1"
                                 else "https://mainnet.base.org")
    g["BASE_MAINNET_GAS_PRICE_WEI"] = int(_env("BASE_MAINNET_GAS_PRICE_WEI", "6000000"))
    g["ETH_EUR_DEMO"] = int(_env("ETH_EUR_DEMO", "3000"))

    g["BANK_RELAYER_PK"] = _env("BANK_RELAYER_PK")
    g["BANK_ADMIN_PK"] = _env("BANK_ADMIN_PK")
    g["BANK_OPERATOR_PK"] = _env("BANK_OPERATOR_PK")
    g["ALFA_PK"] = _env("ALFA_PK")

    g["EURC"] = _env("EURC", "0x808456652fdb597867f38412077A9182bf77359F")
    g["TCZK"] = _env("TCZK")
    g["TCZK_NAME"] = _env("TCZK_NAME", "tCZK Deposit Token")
    g["ESCROW"] = _env("ESCROW", "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c")
    g["COLLECTOR"] = _env("COLLECTOR", "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88")
    g["REFUND_COLLECTOR"] = _env("REFUND_COLLECTOR", "0x7a03443724d14798c4AB4622F1DAAcA761Fea486")
    g["OPERATOR_TOKEN_STORE"] = _env("OPERATOR_TOKEN_STORE", "0xd2b0C305F62427414e9624a2beD72a3792e15cf8")
    g["ACCOUNT_REF_SALT"] = _env("ACCOUNT_REF_SALT", "collabse-demo")  # tCZK accountRef = keccak(salt ‖ account)

    g["BANK_URL"] = _env("BANK_URL", "http://localhost:8000")
    g["BANK_FACILITATOR_URL"] = _env("BANK_FACILITATOR_URL", "http://localhost:8000/x402")
    g["PUBLIC_FACILITATOR_URL"] = _env("PUBLIC_FACILITATOR_URL", "https://x402.org/facilitator")
    g["SELLER_URL"] = _env("SELLER_URL", "http://localhost:8001")
    g["PUBLIC_BASE_URL"] = _env("PUBLIC_BASE_URL")
    g["TATUM_HMAC_SECRET"] = _env("TATUM_HMAC_SECRET")
    # incoming-transfer poll (bank/webhooks.py) when Tatum cannot reach us: 3 RPC calls per tick (block number + one
    # eth_getLogs per asset). 1 s meets block -> feed <= 5 s (p95 3.6 s on the fork); raise it to save RPC credits
    # when nobody watches the feed, or switch the poller off with BANK_POLLER=0
    g["BANK_POLL_S"] = max(0.5, float(_env("BANK_POLL_S", "1.0")))

    g["APIFY_TOKEN"] = _env("APIFY_TOKEN")
    g["APIFY_COMPANY_CHECK_ACTOR"] = _env("APIFY_COMPANY_CHECK_ACTOR", "savvy_sauger/company-check")
    g["MOSTLYRIGHT_API_KEY"] = _env("MOSTLYRIGHT_API_KEY")

    # Shared-secret auth between the parts (prototype; a real bank would use client certificates / OAuth).
    # Agent token -> the one company that agent acts for: the API never takes buyer_id from the caller.
    g["AGENT_TOKENS"] = {_env("AGENT_TOKEN_ALFA", "dev-agent-alfa"): "alfa"}
    g["SELLER_TOKEN"] = _env("SELLER_TOKEN", "dev-seller")     # seller service -> bank (deliveries)
    g["BANK_TO_SELLER_TOKEN"] = _env("BANK_TO_SELLER_TOKEN", "dev-bank")  # bank -> seller (escrow notices)
    g["HUMAN_TOKEN"] = _env("HUMAN_TOKEN", "dev-human")        # bank UI: confirmations, VAT acknowledgements
    g["DECIDER_TOKEN"] = _env("DECIDER_TOKEN", "dev-decider")  # rule C deciders submit decisions (demo: on stage)
    # the decider page takes a per-decider token: DECIDER_TOKENS "dec-1:tokA,dec-2:tokB", or, unset, tokens derived
    # from DECIDER_TOKEN (else HUMAN_TOKEN) per decider id (bank/deciders.py tokens; print: python -m bank.deciders tokens)
    g["DECIDER_TOKENS"] = _env("DECIDER_TOKENS")
    g["DECIDER_SECRET"] = _env("DECIDER_TOKEN") or g["HUMAN_TOKEN"]
    # the escrow operator the seller accepts; derived from the bank's key when this process holds it
    derived = Account.from_key(g["BANK_OPERATOR_PK"]).address if g["BANK_OPERATOR_PK"] else ""
    g["BANK_OPERATOR_ADDR"] = _env("BANK_OPERATOR_ADDR", derived or "0x0469ceC178789072bc61435835D0BB38214D4e53")

    # MODEL_V2 §14 demo scale: on the testnet every euro threshold and fee is divided by DEMO_MONEY_DIVISOR and
    # every window / business day by DEMO_TIME_DIVISOR (30 min -> 20 s). Said on the deal page and on stage.
    g["MONEY_DIVISOR"] = int(_env("DEMO_MONEY_DIVISOR", "100"))
    g["TIME_DIVISOR"] = float(_env("DEMO_TIME_DIVISOR", "90"))
    g["DEMO_SCALE"] = g["MONEY_DIVISOR"] != 1 or g["TIME_DIVISOR"] != 1
    # tiers by tier_sum in euro cents (production: <= 20 €, (20; 500], > 500), §4
    g["TIER_T0_MAX"], g["TIER_T1_MAX"] = 2000 // g["MONEY_DIVISOR"], 50_000 // g["MONEY_DIVISOR"]
    # bank fee per (tier, rule), basis points, taken at capture (feeReceiver = clearing address, §10); T0 is
    # billed monthly from the seller's account, so nothing at capture
    g["TARIFF_BPS"] = {("T0", "A"): 0, ("T1", "A"): 80, ("T1", "B"): 120,
                       ("T2", "A"): 150, ("T2", "B"): 150, ("T2", "C"): 150}
    # claim windows per tier in seconds (production: T1 30 min – 24 h, T2 72 h – 14 days), scaled
    g["WINDOW_S"] = {"T1": (1800 / g["TIME_DIVISOR"], 86400 / g["TIME_DIVISOR"]),
                     "T2": (72 * 3600 / g["TIME_DIVISOR"], 14 * 86400 / g["TIME_DIVISOR"])}
    g["BUSINESS_DAY_S"] = 86400 / g["TIME_DIVISOR"]  # demo: a business day is this many seconds, no calendar
    # escrow terms §8.3 (not scaled: a long lock is safe, the bank closes the deal itself)
    g["ESCROW_EXTRA_DAYS"] = {"T0": 30, "T1": 30, "T2": 120}
    g["REFUND_EXTRA_DAYS"] = 30
    # the bank's clearing address: fee receiver at capture and receiver of C dispute fees (§8.2, §10)
    g["BANK_CLEARING_PK"] = _env("BANK_CLEARING_PK")
    derived_clearing = Account.from_key(g["BANK_CLEARING_PK"]).address if g["BANK_CLEARING_PK"] else ""
    g["BANK_CLEARING_ADDR"] = _env("BANK_CLEARING_ADDR", derived_clearing or "0xC1ea41a6C0000000000000000000000000000bA4")

    # Assets the bank handles. Amounts are always integers in atomic units.
    g["ASSETS"] = {
        "EURC": {"address": g["EURC"], "decimals": 6, "name": "EURC", "version": "2"},
        "tCZK": {"address": g["TCZK"], "decimals": 2, "name": g["TCZK_NAME"], "version": "1"},
    }


reload()
