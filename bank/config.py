# Settings from .env (SPEC §16). Read once at import; tests override via monkeypatch + reload().
import os
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name, default=""):
    # python-dotenv already drops inline " # comments"; don't cut values (an HMAC secret may contain '#')
    return os.getenv(name, default).strip().strip('"')


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
    g["MODEL_MAIN"] = _env("MODEL_MAIN", "anthropic.claude-opus-5-5")
    g["MODEL_FAST"] = _env("MODEL_FAST", "anthropic.claude-haiku-5-5")

    g["CZK_PER_EUR_DEMO"] = int(_env("CZK_PER_EUR_DEMO", "25"))
    g["SCENARIO_PRICE_EUR"] = _env("SCENARIO_PRICE_EUR", "0.10")

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

    g["APIFY_TOKEN"] = _env("APIFY_TOKEN")
    g["APIFY_COMPANY_CHECK_ACTOR"] = _env("APIFY_COMPANY_CHECK_ACTOR", "collabse/company-check")
    g["MOSTLYRIGHT_API_KEY"] = _env("MOSTLYRIGHT_API_KEY")

    # Shared-secret auth between the parts (prototype; a real bank would use client certificates / OAuth).
    # Agent token -> the one company that agent acts for: the API never takes buyer_id from the caller.
    g["AGENT_TOKENS"] = {_env("AGENT_TOKEN_ALFA", "dev-agent-alfa"): "alfa"}
    g["SELLER_TOKEN"] = _env("SELLER_TOKEN", "dev-seller")     # seller service -> bank (deliveries)
    g["BANK_TO_SELLER_TOKEN"] = _env("BANK_TO_SELLER_TOKEN", "dev-bank")  # bank -> seller (escrow notices)
    g["HUMAN_TOKEN"] = _env("HUMAN_TOKEN", "dev-human")        # bank UI: confirmations, VAT acknowledgements
    # the escrow operator the seller accepts; derived from the bank's key when this process holds it
    derived = Account.from_key(g["BANK_OPERATOR_PK"]).address if g["BANK_OPERATOR_PK"] else ""
    g["BANK_OPERATOR_ADDR"] = _env("BANK_OPERATOR_ADDR", derived or "0x0469ceC178789072bc61435835D0BB38214D4e53")

    # Assets the bank handles. Amounts are always integers in atomic units.
    g["ASSETS"] = {
        "EURC": {"address": g["EURC"], "decimals": 6, "name": "EURC", "version": "2"},
        "tCZK": {"address": g["TCZK"], "decimals": 2, "name": g["TCZK_NAME"], "version": "1"},
    }


reload()
