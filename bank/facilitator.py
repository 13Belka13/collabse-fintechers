# Bank x402 facilitator (SPEC §4.3): /x402/verify, /x402/settle, /x402/supported for EURC and tCZK on
# Base Sepolia — one facilitator for both, because a resource server takes one per (network, scheme).
# The bank screens every payment in BOTH on_before_verify and on_before_settle (settle re-verifies
# internally and skips the verify hooks). A refusal is HTTP 200 {"isValid": false, "invalidReason": code}:
# any non-200 makes the resource server error out instead of answering a clean 402.
# Endpoints are sync `def` (web3 is blocking; FastAPI runs them in its thread pool); settle runs under
# one lock, and its tx goes through bank.chain's sender with an idempotency key per EIP-3009
# authorization, so a retry (nonce clash, crash after broadcast, FAULTS=settle_fail_once) never sends
# a second transfer. Retry-on-nonce-error and "pending" nonces follow the lessons from the disclosed
# prep/ spikes (REFERENCE §8a).
import logging
import threading
import time

from fastapi import APIRouter, Body
from web3 import Web3
from x402 import x402FacilitatorSync
from x402.mechanisms.evm import FacilitatorWeb3Signer
from x402.mechanisms.evm.constants import ERR_SETTLEMENT_PENDING
from x402.mechanisms.evm.data_suffix import append_data_suffix
from x402.mechanisms.evm.exact import register_exact_evm_facilitator
from x402.schemas import AbortResult, PaymentAbortedError, parse_payment_payload, parse_payment_requirements

from bank import chain, config, core, journal, money, tczk, timing
from bank.codes import Code

log = logging.getLogger(__name__)
router = APIRouter(prefix="/x402", tags=["x402"])

MAX_PAYMENT_EUR_CENTS = 1000  # sanity cap = the demo mandate's per-deal limit (§6.1); mandates are checked before
GAS_LIMIT = 500_000           # legacy tx with fixed gas, like the SDK signer
CONFIRMATION_TIMEOUT = 60     # below the resource server's 90 s facilitator timeout
SETTLE_TRIES = 3

_settle_lock = threading.Lock()
_fac_lock = threading.Lock()
_fac = None
_fault_fired = False


# ---- screening ----

def _asset(address: str) -> str | None:
    for symbol, meta in config.ASSETS.items():
        if meta["address"] and meta["address"].lower() == address.lower():
            return symbol
    return None


def _screen(payload, requirements) -> tuple[str, str] | None:
    """(code, fine-grained reason for the journal) or None. Cheap: SQLite + for tCZK two eth_calls."""
    if getattr(payload, "x402_version", None) != 2 or requirements.network != config.NETWORK \
            or requirements.scheme != "exact":
        return Code.QUOTE_MISMATCH, "unsupported_network_or_scheme"
    asset = _asset(requirements.asset)
    if asset is None:
        return Code.QUOTE_MISMATCH, "asset_not_handled_by_bank"
    meta, extra = config.ASSETS[asset], requirements.extra or {}
    if extra.get("name") != meta["name"] or extra.get("version") != meta["version"]:
        return Code.QUOTE_MISMATCH, "eip712_domain_mismatch"
    try:
        amount = int(requirements.amount)
    except (TypeError, ValueError):
        return Code.QUOTE_MISMATCH, "amount_not_atomic"
    if amount <= 0:
        return Code.QUOTE_MISMATCH, "amount_not_positive"
    if money.to_eur_cents(asset, amount) > MAX_PAYMENT_EUR_CENTS:
        return Code.AMOUNT_ABOVE_LIMIT, "above_bank_payment_cap"
    auth = (payload.payload or {}).get("authorization") or {}  # EIP-3009 only (no Permit2)
    pay_to, payer = requirements.pay_to, auth.get("from") or ""
    if (auth.get("to") or "").lower() != pay_to.lower():
        return Code.COUNTERPARTY_NOT_IN_SCOPE, "authorization_to_differs_from_pay_to"
    if str(auth.get("value")) != str(amount):
        return Code.QUOTE_MISMATCH, "authorization_value_differs_from_amount"
    if core.company_by_wallet(pay_to) is None:
        return Code.COUNTERPARTY_NOT_IN_SCOPE, "payee_not_a_bank_client"
    if not payer or core.company_by_wallet(payer) is None:  # the relayer pays gas only for the bank's own clients
        return Code.COUNTERPARTY_NOT_IN_SCOPE, "payer_not_a_bank_client"
    if tczk.bank_frozen(pay_to) or (payer and tczk.bank_frozen(payer)):
        return Code.UNDER_REVIEW, "wallet_frozen_at_bank"  # non-public: the client sees only the neutral code
    if asset == "tCZK":
        if not tczk.is_allowed(pay_to):
            return Code.COUNTERPARTY_NOT_IN_SCOPE, "payee_not_on_tczk_allowlist"
        if tczk.is_frozen(pay_to) or (payer and tczk.is_frozen(payer)):
            return Code.UNDER_REVIEW, "wallet_frozen_on_chain"
    return None


def screen(payload, requirements) -> str | None:
    """Bank screening for one x402 payment: a closed-enum code (§6.3) to refuse, or None to let it through."""
    hit = _screen(payload, requirements)
    return str(hit[0]) if hit else None


def _hook(stage: str):
    def hook(ctx):
        hit = _screen(ctx.payment_payload, ctx.requirements)
        if hit is None:
            return None
        auth = (ctx.payment_payload.payload or {}).get("authorization") or {}
        journal.append("bank", "x402_refused", {
            "stage": stage, "code": str(hit[0]), "reason": hit[1], "payer": auth.get("from"),
            "pay_to": ctx.requirements.pay_to, "asset": ctx.requirements.asset, "amount": ctx.requirements.amount})
        return AbortResult(reason=str(hit[0]))
    return hook


# ---- signer: SDK verify logic, bank's sender for transactions ----

class BankSigner(FacilitatorWeb3Signer):
    """FacilitatorWeb3Signer (x402 2.25.0) whose reads use bank.chain.w3() and whose transactions go
    through chain.sender(BANK_RELAYER_PK): one nonce counter per key in this process and an idempotency
    key per EIP-3009 authorization (token, from, nonce) — the same payload settled twice is one tx.
    __init__ is not called on purpose: it would build a second web3 client with its own nonce counter."""

    def __init__(self, private_key: str):  # noqa: super-init-not-called
        from eth_account import Account
        self._pk = private_key
        self._account = Account.from_key(private_key)
        self._confirmation_timeout_seconds = CONFIRMATION_TIMEOUT
        self._gas_limit = GAS_LIMIT
        self._chain_id = None
        self._nonce_lock = threading.Lock()
        self._next_nonce = None

    @property
    def _w3(self):
        return chain.w3()

    def get_chain_id(self) -> int:
        return chain.chain_id()

    def write_contract(self, address, abi, function_name, *args, data_suffix=None) -> str:
        global _fault_fired
        fn = getattr(chain.w3().eth.contract(address=Web3.to_checksum_address(address), abi=abi).functions,
                     function_name)(*args)
        call = fn
        if data_suffix:
            call = {"to": Web3.to_checksum_address(address),
                    "data": append_data_suffix(fn._encode_transaction_data(), data_suffix)}
        # transferWithAuthorization(from, to, value, validAfter, validBefore, nonce, ...)
        nonce = args[5].hex() if isinstance(args[5], bytes) else str(args[5])
        idem_key = f"x402:{address.lower()}:{str(args[0]).lower()}:{nonce}"
        try:
            tx = chain.sender(self._pk).send(call, kind="x402_settle", idem_key=idem_key, gas=self._gas_limit,
                                             timeout=self._confirmation_timeout_seconds)
        except chain.TxReverted as e:
            return e.tx_hash  # the SDK reads the failed receipt and answers transaction_failed with the hash
        if "settle_fail_once" in config.FAULTS and not _fault_fired:
            _fault_fired = True  # §19.2: crash after broadcast; the retry must find this tx, not send another
            raise RuntimeError("FAULT settle_fail_once: facilitator failed after broadcast")
        return tx["tx_hash"]

    def send_transaction(self, to: str, data: bytes) -> str:  # ERC-6492 wallet deploys (no factory allowed)
        return chain.sender(self._pk).send({"to": Web3.to_checksum_address(to), "data": data},
                                           kind="x402_wallet_deploy", gas=self._gas_limit)["tx_hash"]


def facilitator() -> x402FacilitatorSync:
    global _fac
    with _fac_lock:
        if _fac is None:
            if not config.BANK_RELAYER_PK:
                raise RuntimeError("BANK_RELAYER_PK missing (TODO(credential)); EURC fallback: x402.org facilitator")
            fac = x402FacilitatorSync()
            register_exact_evm_facilitator(fac, BankSigner(config.BANK_RELAYER_PK), networks=config.NETWORK)
            fac.on_before_verify(_hook("verify"))
            fac.on_before_settle(_hook("settle"))
            _fac = fac
        return _fac


def reset():
    """Drop the facilitator (tests swap chains and keys) and re-arm the settle_fail_once fault."""
    global _fac, _fault_fired
    with _fac_lock:
        _fac, _fault_fired = None, False


# ---- HTTP ----

def _parse(body: dict):
    version = int(body.get("x402Version") or body.get("paymentPayload", {}).get("x402Version") or 1)
    return (parse_payment_payload(body["paymentPayload"]),
            parse_payment_requirements(version, body["paymentRequirements"]))


def _payer(body: dict) -> str | None:
    return (((body.get("paymentPayload") or {}).get("payload") or {}).get("authorization") or {}).get("from")


def _invalid(reason: str, body: dict, message: str | None = None) -> dict:
    out = {"isValid": False, "invalidReason": reason, "payer": _payer(body)}
    if message:
        out["invalidMessage"] = message
    return out


def _settle_refused(reason: str, body: dict, message: str | None = None) -> dict:
    # Readable both as a SettleResponse and as the {"isValid": false, "invalidReason"} refusal shape.
    return {**_invalid(reason, body, message), "success": False, "errorReason": reason,
            "transaction": "", "network": config.NETWORK}


@router.get("/supported")
def supported():
    sup = facilitator().get_supported()
    kinds = [k.model_dump(by_alias=True, exclude_none=True) for k in sup.kinds
             if k.x402_version == 2 and k.network == config.NETWORK]
    assets = [{"asset": symbol, "address": m["address"], "decimals": m["decimals"],
               "extra": {"name": m["name"], "version": m["version"]}}
              for symbol, m in config.ASSETS.items() if m["address"]]
    return {"kinds": kinds, "extensions": sup.extensions, "signers": sup.signers, "assets": assets}


@router.post("/verify")
def verify(body: dict = Body(...)):
    try:
        payload, requirements = _parse(body)
    except Exception as e:  # noqa: BLE001 — malformed body is a refusal, not a 500
        return _invalid("invalid_payload", body, str(e)[:200])
    with timing.timed("facilitator_verify"):
        try:
            result = facilitator().verify(payload, requirements)
        except PaymentAbortedError as e:
            return _invalid(e.reason, body)
        except Exception as e:  # noqa: BLE001 — RPC hiccup: refuse cleanly, the client may retry
            log.exception("x402 verify failed")
            return _invalid("facilitator_error", body, str(e)[:200])
    return result.model_dump(by_alias=True, exclude_none=True)


def _retryable(result) -> bool:
    msg = (result.error_message or "").lower()
    return (result.error_reason == ERR_SETTLEMENT_PENDING or "fault settle_fail_once" in msg
            or any(m in msg for m in chain.NONCE_ERRORS))


@router.post("/settle")
def settle(body: dict = Body(...)):
    try:
        payload, requirements = _parse(body)
    except Exception as e:  # noqa: BLE001
        return _settle_refused("invalid_payload", body, str(e)[:200])
    with _settle_lock, timing.timed("facilitator_settle"):
        result = None
        for attempt in range(SETTLE_TRIES):
            try:
                result = facilitator().settle(payload, requirements)
            except PaymentAbortedError as e:
                return _settle_refused(e.reason, body)
            except Exception as e:  # noqa: BLE001 — RPC / signer error: a clean failure, never a 500 (the resource
                # server then answers no delivery); a retry is safe — one idempotency key per EIP-3009 authorization
                log.exception("x402 settle failed")
                journal.append("bank", "x402_settle_failed", {"payer": _payer(body), "pay_to": requirements.pay_to,
                                                              "asset": requirements.asset, "amount": requirements.amount,
                                                              "reason": "facilitator_error",
                                                              "error": f"{type(e).__name__}: {str(e)[:200]}"})
                return _settle_refused("facilitator_error", body, "the bank could not settle now; retry later")
            if result.success or not _retryable(result):
                break
            journal.append("bank", "x402_settle_retry", {"attempt": attempt + 1, "reason": result.error_reason,
                                                         "message": (result.error_message or "")[:200],
                                                         "transaction": result.transaction})
            time.sleep(0.5 * attempt)
    out = result.model_dump(by_alias=True, exclude_none=True)
    journal.append("bank", "x402_settled" if result.success else "x402_settle_failed", {
        "payer": result.payer, "pay_to": requirements.pay_to, "asset": requirements.asset,
        "amount": requirements.amount, "transaction": result.transaction,
        "link": chain.tx_link(result.transaction) if result.transaction else None,
        "reason": result.error_reason})
    return out
