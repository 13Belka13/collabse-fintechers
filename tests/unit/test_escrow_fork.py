# L3 escrow cycle on an anvil fork of Base Sepolia, against the deployed AuthCaptureEscrow 0xf968…B19c and
# ERC-3009 collector 0x8612…ab88 with real EURC: authorize 5,00 € with the MODEL_V2 §8 terms (T1 rule B: fee
# 1,2 % to the clearing address, salt keccak(deal_id ‖ 0), long expiries) -> seller checks escrow -> capture
# 4,50 € (seller gets 4,446 €, the clearing address 0,054 €) -> void 0,50 €. Balances and escrow state are read
# from the (forked) chain — this also proves AuthCaptureEscrow v1.1 accepts feeAmount = floor(amount·bps/10000).
# Skipped when anvil or the public RPC is unavailable (CI). EURC comes from the funded demo buyer by impersonation.
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from eth_account import Account
from web3 import Web3

from bank import chain, config, db, escrow

FORK_URL = os.getenv("FORK_URL", "https://sepolia.base.org")
ANVIL = os.getenv("ANVIL") or shutil.which("anvil") or str(Path.home() / ".foundry" / "bin" / "anvil")
FUNDED_BUYER = "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140"  # demo buyer, holds testnet EURC
BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"
PRICE = 5_000_000


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def anvil():
    if not Path(ANVIL).exists():
        pytest.skip("anvil not installed (foundry)")
    port = _free_port()
    proc = subprocess.Popen([ANVIL, "--fork-url", FORK_URL, "--port", str(port), "--silent"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
    for _ in range(60):
        if proc.poll() is not None:
            pytest.skip(f"anvil fork failed: {proc.stderr.read().decode()[:200]}")
        try:
            if w3.eth.chain_id == 84532:
                break
        except Exception:  # noqa: BLE001 — not up yet
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.skip("anvil fork did not start (RPC unreachable?)")
    yield url, w3
    proc.kill()
    proc.wait()


def _fund_buyer(w3, buyer):
    eurc = w3.eth.contract(address=config.EURC, abi=chain.ERC20_ABI)
    if eurc.functions.balanceOf(FUNDED_BUYER).call() < PRICE:
        pytest.skip("demo buyer has < 5 EURC on Base Sepolia (faucet.circle.com)")
    w3.provider.make_request("anvil_impersonateAccount", [FUNDED_BUYER])
    w3.provider.make_request("anvil_setBalance", [FUNDED_BUYER, hex(10**18)])
    h = eurc.functions.transfer(buyer, PRICE).transact({"from": FUNDED_BUYER})
    assert w3.eth.wait_for_transaction_receipt(h)["status"] == 1
    w3.provider.make_request("anvil_stopImpersonatingAccount", [FUNDED_BUYER])


def test_escrow_cycle_authorize_capture_void_on_fork(anvil, monkeypatch):
    from scripts.seed import seed
    from seller.app import verify_escrow

    url, w3 = anvil
    operator, buyer_acct = Account.create(), Account.create()  # fresh keys: empty on the fork
    buyer = buyer_acct.address
    w3.provider.make_request("anvil_setBalance", [operator.address, hex(10**18)])  # gas for the operator only
    monkeypatch.setenv("RPC_URL", url)
    monkeypatch.setenv("BANK_OPERATOR_PK", operator.key.to_0x_hex())
    monkeypatch.setenv("ALFA_PK", buyer_acct.key.to_0x_hex())
    monkeypatch.setenv("ALFA_ADDR", buyer)
    config.reload()
    chain.set_w3(None)
    seed()
    _fund_buyer(w3, buyer)
    try:
        terms = {"rule": "B", "b_pct": 10, "tier": "T1", "window_s": 20, "deadline_s": 300, "fee_bps": 120}
        db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, quote_json, terms_json, asset, amount, state,"
               " created_at) VALUES ('d-fork','alfa','s-beta',?,?,'EURC',?,'quoted',?)",
               (db.dumps({"pay_to": BETA, "asset": "EURC", "amount": str(PRICE)}), db.dumps(terms), PRICE, time.time()))
        bal = lambda a: chain.balance("EURC", a)  # noqa: E731
        seller0, clearing0 = bal(BETA), bal(config.BANK_CLEARING_ADDR)
        assert bal(buyer) == PRICE

        pi = escrow.payment_info(db.one("SELECT * FROM deals WHERE deal_id='d-fork'"))
        on_chain = escrow.contract().functions.getHash(escrow.as_tuple(pi)).call()
        assert escrow.payment_hash(pi) == "0x" + on_chain.hex().removeprefix("0x")

        tx = escrow.authorize("d-fork")
        assert tx["status"] == "mined"
        assert escrow.state("d-fork") == {"collected": True, "capturable": PRICE, "refundable": 0}
        assert bal(buyer) == 0

        offer = {"deal_id": "d-fork", "pay_to": BETA, "prices": {"EURC": str(PRICE)}, "rule": "B", "window_s": 20,
                 "deadline_s": 300}
        notice = {"n": 0, "tier": "T1", "kind": "deal"}
        assert verify_escrow(offer, "EURC", pi, notice=notice) is None  # the seller's own on-chain check passes
        assert verify_escrow({**offer, "prices": {"EURC": "4000000"}}, "EURC", {**pi, "maxAmount": 4_000_000},
                             notice=notice) == "escrow_capturable_not_price"  # never authorized: nothing locked

        to_seller, back = 4_500_000, 500_000  # 18 of 20 items at 0,25 €
        out = escrow.finish("d-fork", to_seller)
        assert out["capture"]["status"] == "mined" and out["void"]["status"] == "mined"
        fee = to_seller * 120 // 10_000
        assert bal(BETA) - seller0 == to_seller - fee and bal(config.BANK_CLEARING_ADDR) - clearing0 == fee
        assert bal(buyer) == back
        assert escrow.state("d-fork") == {"collected": True, "capturable": 0, "refundable": to_seller}

        again = escrow.capture("d-fork", to_seller, 1)  # same idem key: the stored tx, no second capture
        assert again.get("reused") and bal(BETA) - seller0 == to_seller - fee
        kinds = [r["kind"] for r in db.rows("SELECT kind FROM onchain_txs WHERE deal_id='d-fork' ORDER BY created_at")]
        assert kinds == ["authorize", "capture", "void"]
    finally:
        chain.set_w3(None)
