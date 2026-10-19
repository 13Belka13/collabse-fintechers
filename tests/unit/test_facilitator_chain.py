# Bank facilitator end to end on a real EVM: x402 SDK-signed payments verified and settled by our
# endpoints, read back through the SDK's own HTTP facilitator client (the resource server's view).
#   tCZK: plain anvil with chain id 84532 (EIP-712 domains need it), our token deployed fresh.
#   EURC: anvil fork of Base Sepolia (FORK=1): the real EURC contract, funds taken from the buyer wallet.
# Asserts read balances and Transfer counts from the chain, not from our DB.
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3 import Web3
from x402 import x402ClientSync
from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.schemas import PaymentRequired, PaymentRequirements

from bank import chain, config, db, facilitator, journal, tczk

ANVIL = shutil.which("anvil") or str(Path.home() / ".foundry" / "bin" / "anvil")
pytestmark = [pytest.mark.anvil, pytest.mark.skipif(not Path(ANVIL).exists(), reason="anvil (Foundry) not installed")]

BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"
GAMA = "0x19d12f1e3869e75D819aD9190065D3B77b4c7Ac1"
ALFA_REAL = "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140"  # buyer test wallet on Base Sepolia (holds EURC)
STORE = "0x000000000000000000000000000000000000510e"   # stands in for the escrow token store (no escrow here)
# Fresh deterministic test keys, funded with anvil_setBalance — never real funds.
BANK_KEY = Web3.keccak(text="collabse-test-bank").to_0x_hex()
BUYER_KEY = Web3.keccak(text="collabse-test-buyer").to_0x_hex()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_anvil(*args):
    port = _free_port()
    proc = subprocess.Popen([ANVIL, "--port", str(port), "--silent", *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}"))
    for _ in range(150):
        try:
            if w3.eth.chain_id == 84532:
                return proc, port
        except Exception:  # noqa: BLE001 — still starting
            time.sleep(0.1)
    proc.kill()
    pytest.skip("anvil did not start")


def _fund(w3, *addrs):
    for a in addrs:
        w3.provider.make_request("anvil_setBalance", [a, hex(10**18)])


@pytest.fixture(scope="module")
def anvil_local():
    proc, port = _start_anvil("--chain-id", "84532")
    yield port
    proc.kill()


def _use(port, monkeypatch, **env):
    for k, v in {"RPC_URL": f"http://127.0.0.1:{port}", "BANK_RELAYER_PK": BANK_KEY, "BANK_ADMIN_PK": BANK_KEY,
                 **env}.items():
        monkeypatch.setenv(k, v)
    config.reload()
    chain.set_w3(None)
    facilitator.reset()
    w3 = chain.w3()
    _fund(w3, Account.from_key(BANK_KEY).address, Account.from_key(BUYER_KEY).address)
    return w3


@pytest.fixture
def tczk_bank(anvil_local, seeded, monkeypatch):
    w3 = _use(anvil_local, monkeypatch)
    addr = tczk.deploy(store=STORE, mint_alfa=0)  # relayer = bank key: ROUTER (strict mode, needed by verify's eth_call)
    monkeypatch.setenv("TCZK", addr)
    monkeypatch.setenv("TCZK_NAME", "tCZK Deposit Token")
    config.reload()
    buyer = Account.from_key(BUYER_KEY).address
    tczk.allow(buyer, "9999999999/9999")
    tczk.mint(buyer, 50_000, f"dep-{addr}")  # 500.00 Kč
    yield w3
    chain.set_w3(None)
    facilitator.reset()


def _requirements(asset: str, amount: int, pay_to: str = BETA) -> PaymentRequirements:
    m = config.ASSETS[asset]
    return PaymentRequirements(scheme="exact", network=config.NETWORK, asset=m["address"], amount=str(amount),
                               pay_to=pay_to, max_timeout_seconds=60, extra={"name": m["name"], "version": m["version"]})


def _sdk_pay(req: PaymentRequirements):
    """What the buyer's x402 client sends: EIP-3009 authorization signed by the SDK (SPEC §4.2 spend controls)."""
    client = x402ClientSync().set_spend_controls({"max_amount_per_payment": False, "allowed_assets": [
        {"network": config.NETWORK, "asset": req.asset, "max_amount_per_payment": "100000000"}]})
    client.register(config.NETWORK, ExactEvmScheme(EthAccountSigner(Account.from_key(BUYER_KEY))))
    return client.create_payment_payload(PaymentRequired(accepts=[req]))


@pytest.fixture
def http():
    """The SDK's facilitator client talking to our router — exactly how the seller middleware calls the bank."""
    app = FastAPI()
    app.include_router(facilitator.router)
    return HTTPFacilitatorClientSync(FacilitatorConfig(url="http://testserver/x402", http_client=TestClient(app)))


def _balance(asset, who):
    return chain.balance(asset, who)


def _transfers(asset, frm, to):
    return chain.transfers(config.ASSETS[asset]["address"], 0, frm=frm, to=to)


# ---- tCZK on local anvil ----

def test_tczk_payment_verified_and_settled(tczk_bank, http):
    buyer = Account.from_key(BUYER_KEY).address
    req = _requirements("tCZK", 12_500)  # 125,00 Kč = 5 € at the demo rate
    payload = _sdk_pay(req)
    assert http.get_supported().kinds[0].network == config.NETWORK
    v = http.verify(payload, req)
    assert v.is_valid, v
    s = http.settle(payload, req)
    assert s.success and s.transaction, s
    assert _balance("tCZK", BETA) == 12_500 and _balance("tCZK", buyer) == 50_000 - 12_500
    assert journal.entries()[0]["event"] == "x402_settled"  # newest first
    assert db.one("SELECT COUNT(*) n FROM timings WHERE stage='facilitator_verify'")["n"] == 1


def test_same_payload_settled_twice_is_one_transfer(tczk_bank, http):
    buyer = Account.from_key(BUYER_KEY).address
    req = _requirements("tCZK", 2_500)
    payload = _sdk_pay(req)
    first, again = http.settle(payload, req), http.settle(payload, req)
    assert first.success and again.success and again.transaction == first.transaction
    assert len(_transfers("tCZK", buyer, BETA)) == 1 and _balance("tCZK", BETA) == 2_500


def test_settle_fail_once_retries_into_exactly_one_tx(tczk_bank, http, monkeypatch):
    """§19.2 fault: the facilitator fails right after broadcast; the retry finds the same tx (idempotency key)."""
    monkeypatch.setenv("FAULTS", "settle_fail_once")
    config.reload()
    buyer = Account.from_key(BUYER_KEY).address
    req = _requirements("tCZK", 2_500)
    s = http.settle(_sdk_pay(req), req)
    assert s.success, s
    assert len(_transfers("tCZK", buyer, BETA)) == 1
    assert db.one("SELECT COUNT(*) n FROM onchain_txs WHERE kind='x402_settle'")["n"] == 1
    assert [e["event"] for e in journal.entries()][:2] == ["x402_settled", "x402_settle_retry"]  # newest first


def test_payee_off_tczk_allowlist_is_refused_before_chain(tczk_bank, http):
    """N09: a bank client whose wallet is not on the tCZK allowlist → isValid false, no transfer attempted."""
    tczk.disallow(GAMA)
    req = _requirements("tCZK", 2_500, pay_to=GAMA)
    payload = _sdk_pay(req)
    v = http.verify(payload, req)
    s = http.settle(payload, req)
    assert (v.is_valid, v.invalid_reason) == (False, "COUNTERPARTY_NOT_IN_SCOPE")
    assert (s.success, s.error_reason) == (False, "COUNTERPARTY_NOT_IN_SCOPE")
    assert _balance("tCZK", GAMA) == 0
    assert db.one("SELECT COUNT(*) n FROM onchain_txs WHERE kind='x402_settle'")["n"] == 0


def test_frozen_payee_is_under_review(tczk_bank, http):
    tczk.freeze(BETA, case_ref="case-9")
    req = _requirements("tCZK", 2_500)
    v = http.verify(_sdk_pay(req), req)
    assert (v.is_valid, v.invalid_reason) == (False, "UNDER_REVIEW")


def test_replayed_payload_after_settle_fails_verify(tczk_bank, http):
    """N11 at the facilitator: the SDK's eth_call simulation sees the used nonce; no second transfer."""
    buyer = Account.from_key(BUYER_KEY).address
    req = _requirements("tCZK", 2_500)
    payload = _sdk_pay(req)
    assert http.settle(payload, req).success
    v = http.verify(payload, req)
    assert v.is_valid is False
    assert len(_transfers("tCZK", buyer, BETA)) == 1


# ---- EURC on an anvil fork of Base Sepolia ----

@pytest.fixture
def eurc_bank(seeded, monkeypatch):
    if os.getenv("FORK") != "1":
        pytest.skip("fork test: set FORK=1 (needs network to https://sepolia.base.org)")
    proc, port = _start_anvil("--fork-url", os.getenv("FORK_URL", "https://sepolia.base.org"))
    try:
        w3 = _use(port, monkeypatch)
        _fund(w3, ALFA_REAL)
        eurc = w3.eth.contract(address=Web3.to_checksum_address(config.EURC), abi=chain.ERC20_ABI)
        w3.provider.make_request("anvil_impersonateAccount", [ALFA_REAL])
        buyer = Account.from_key(BUYER_KEY).address
        h = eurc.functions.transfer(buyer, 1_000_000).transact({"from": ALFA_REAL})  # 1.00 EURC to our test key
        assert w3.eth.wait_for_transaction_receipt(h)["status"] == 1
        yield w3
    finally:
        chain.set_w3(None)
        facilitator.reset()
        proc.kill()


@pytest.mark.fork
def test_eurc_payment_verified_and_settled_on_fork(eurc_bank, http):
    buyer = Account.from_key(BUYER_KEY).address
    beta_before = _balance("EURC", BETA)
    req = _requirements("EURC", 250_000)  # 0.25 €
    payload = _sdk_pay(req)
    v = http.verify(payload, req)
    assert v.is_valid, v
    s = http.settle(payload, req)
    assert s.success, s
    assert _balance("EURC", BETA) - beta_before == 250_000
    assert _balance("EURC", buyer) == 750_000
    receipt = chain.w3().eth.get_transaction_receipt(s.transaction)
    assert receipt["from"] == Account.from_key(BANK_KEY).address  # the bank's relayer paid the gas


@pytest.mark.fork
def test_eurc_to_non_client_refused_on_fork(eurc_bank, http):
    stranger = "0x000000000000000000000000000000000000dEaD"
    before = _balance("EURC", stranger)
    req = _requirements("EURC", 250_000, pay_to=stranger)
    s = http.settle(_sdk_pay(req), req)
    assert (s.success, s.error_reason) == (False, "COUNTERPARTY_NOT_IN_SCOPE")
    assert _balance("EURC", stranger) == before
