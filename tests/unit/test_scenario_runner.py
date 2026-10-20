# scripts/scenarios.py helpers that judge the chain: the reconciliation counts a capture gross (seller + the bank's
# fee to the clearing address, MODEL_V2 §10), and the Tatum payload F04/F05 post is still there.
from web3 import Web3

from bank import config, db, journal
from scripts import scenarios

BUYER = "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140"
BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"


def test_reconcile_counts_the_fee_leg_of_a_capture(seeded, monkeypatch):
    clearing = Web3.to_checksum_address(config.BANK_CLEARING_ADDR)
    legs = {"0xa": [{"from": BUYER, "to": config.COLLECTOR, "value": 200_000}],
            "0xc": [{"from": BUYER, "to": BETA, "value": 177_840}, {"from": BUYER, "to": clearing, "value": 2_160}],
            "0xv": [{"from": BUYER, "to": BUYER, "value": 20_000}]}
    monkeypatch.setattr(scenarios, "transfers_in", lambda h, asset: legs[h])
    monkeypatch.setattr(scenarios.escrow, "read_state", lambda h: {"capturable": 0})
    for h, kind in (("0xa", "authorize"), ("0xc", "capture:1"), ("0xv", "void")):
        db.run("INSERT INTO onchain_txs (tx_hash, kind, deal_id, status, created_at) VALUES (?,?,?,?,0)",
               (h, kind, "d-r", "mined"))
    journal.append("bank", "escrow_captured", {"amount": 180_000, "fee": 2_160}, deal_id="d-r")
    deal = {"deal_id": "d-r", "asset": "EURC", "seller_id": "s-beta", "amount": 200_000, "payment_hash": "0xh"}
    out = scenarios.reconcile_deal(deal, BUYER)
    assert out["chain"] == {"authorize": 200_000, "capture": 180_000, "void": 20_000}
    assert out["checks"]["captured_chain_eq_journal"] and out["checks"]["locked_eq_captured_voided_capturable"]


def test_tatum_body_is_the_enriched_template():
    body = scenarios.tatum_body("0x1", BETA)
    assert body["subscriptionType"] == "INCOMING_FUNGIBLE_TX" and body["data"]["txId"] == "0x1"
    assert body["data"]["contractAddress"] == config.EURC and body["data"]["tokenMetadata"]["decimals"] == 6
