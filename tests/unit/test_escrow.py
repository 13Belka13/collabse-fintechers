# Escrow operator without a chain (SPEC §5, MODEL_V2 §8): PaymentInfo values (fee to the clearing address at the
# tariff, salt keccak(deal_id ‖ n), long expiries) and persistence order, the buyer's ReceiveWithAuthorization,
# idem keys, the fee taken at capture, "paid" over the seller's PaymentInfos only, no capture after
# authorizationExpiry, capture before the final void.
# The same code against the real contracts on an anvil fork: test_escrow_fork.py.
import time

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from bank import chain, config, db, escrow

BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"
TERMS = {"rule": "B", "b_pct": 10, "tier": "T1", "window_s": 20, "deadline_s": 300, "fee_bps": 120}


class FakeSender:
    def __init__(self, deal_id):
        self.deal_id, self.sent = deal_id, []

    def send(self, call, **kw):
        stored = db.one("SELECT payment_info_json FROM deals WHERE deal_id=?", (self.deal_id,))["payment_info_json"]
        self.sent.append({"fn": call.fn_name, "args": call.args, "stored_before_send": stored is not None, **kw})
        h = "0x%064x" % len(self.sent)
        return {"tx_hash": h, "link": chain.tx_link(h), "status": "mined"}


@pytest.fixture
def deal(monkeypatch, seeded):
    operator, buyer = Account.create(), Account.create()
    monkeypatch.setenv("BANK_OPERATOR_PK", operator.key.hex())
    monkeypatch.setenv("ALFA_PK", buyer.key.hex())
    config.reload()
    db.run("UPDATE companies SET wallet_address=? WHERE id='alfa'", (buyer.address,))
    db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, quote_json, terms_json, asset, amount, state, created_at)"
           " VALUES ('d-1','alfa','s-beta',?,?,'EURC',5000000,'quoted',?)",
           (db.dumps({"pay_to": BETA, "asset": "EURC", "amount": "5000000"}), db.dumps(TERMS), time.time()))
    sender = FakeSender("d-1")
    monkeypatch.setattr(chain, "sender", lambda pk: sender)
    return {"operator": operator, "buyer": buyer, "sender": sender}


def test_payment_info_values_and_persisted(deal):
    now = 1_800_000_000
    pi = escrow.payment_info(db.one("SELECT * FROM deals WHERE deal_id='d-1'"), now=now)
    assert [n for n, _ in escrow.PI_FIELDS] == list(pi)  # field order = hashing order (§5.1)
    assert pi["operator"] == deal["operator"].address and pi["payer"] == deal["buyer"].address
    assert pi["receiver"] == BETA and pi["token"] == config.EURC and pi["maxAmount"] == 5_000_000
    assert (pi["minFeeBps"], pi["maxFeeBps"]) == (120, 120)  # T1 rule B tariff, both ends
    assert pi["feeReceiver"].lower() == config.BANK_CLEARING_ADDR.lower()  # never 0: the operator may not pick
    assert pi["authorizationExpiry"] == now + 300 + 20 + 30 * 86400  # deadline + window + 30 days (T1)
    assert pi["refundExpiry"] == pi["authorizationExpiry"] + 30 * 86400
    assert int(pi["salt"]) == escrow.salt("d-1", 0)
    row = db.one("SELECT * FROM deals WHERE deal_id='d-1'")
    assert db.loads(row["payment_info_json"]) == pi and row["payment_hash"] == escrow.payment_hash(pi)
    assert db.one("SELECT receiver, purpose FROM payment_infos")["receiver"] == BETA


def test_salt_is_keccak_of_deal_and_n_and_tier_t2_locks_longer(deal):
    from web3 import Web3
    assert escrow.salt("d-1", 0) == int.from_bytes(Web3.keccak(b"d-1" + (0).to_bytes(32, "big")), "big")
    d = db.one("SELECT * FROM deals WHERE deal_id='d-1'")
    a = escrow.payment_info(d, now=1)
    db.run("UPDATE deals SET pi_n=1 WHERE deal_id='d-1'")  # a rebuilt PaymentInfo is the next n
    b = escrow.payment_info(db.one("SELECT * FROM deals WHERE deal_id='d-1'"), now=1)
    assert a["salt"] != b["salt"] and escrow.payment_hash(a) != escrow.payment_hash(b)
    t2 = escrow.expiries({**TERMS, "tier": "T2", "window_s": 300}, 0)
    assert t2[0] == 300 + 300 + 120 * 86400


def test_buyer_signs_receive_with_authorization(deal):
    pi = escrow.payment_info(db.one("SELECT * FROM deals WHERE deal_id='d-1'"))
    sig = escrow.buyer_signature(pi, "EURC", deal["buyer"].key.hex())
    msg = encode_typed_data(
        domain_data={"name": "EURC", "version": "2", "chainId": 84532, "verifyingContract": config.EURC},
        message_types=escrow.RECEIVE_TYPES,
        message_data={"from": pi["payer"], "to": config.COLLECTOR, "value": 5_000_000, "validAfter": 0,
                      "validBefore": pi["preApprovalExpiry"],
                      "nonce": bytes.fromhex(escrow.payment_hash({**pi, "payer": escrow.ZERO})[2:])})
    assert len(sig) == 65 and Account.recover_message(msg, signature=sig) == deal["buyer"].address


def test_authorize_persists_before_send_and_uses_idem_key(deal):
    escrow.authorize("d-1")
    sent = deal["sender"].sent[0]
    assert sent["fn"] == "authorize" and sent["stored_before_send"]
    assert sent["idem_key"] == escrow.idem_key("d-1", "authorize") and sent["kind"] == "authorize"
    pi = escrow.stored_payment_info("d-1")
    assert sent["args"][0] == escrow.as_tuple(pi) and sent["args"][1] == 5_000_000
    assert sent["args"][2] == config.COLLECTOR
    escrow.authorize("d-1")  # a retry reuses the stored PaymentInfo (same idem key -> same tx in chain.sender)
    assert deal["sender"].sent[1]["args"][0] == sent["args"][0]


def test_capture_takes_the_tariff_fee_to_the_clearing_address(deal):
    escrow.authorize("d-1")
    escrow.capture("d-1", 4_500_000, 1)
    escrow.void("d-1")
    _, cap, void = deal["sender"].sent
    assert cap["fn"] == "capture" and cap["idem_key"] == escrow.idem_key("d-1", "capture:1")
    amount, fee, receiver = cap["args"][1:]
    assert (amount, fee) == (4_500_000, 54_000) and receiver.lower() == config.BANK_CLEARING_ADDR.lower()
    assert escrow.fee_of({"maxFeeBps": 120}, 3) == 0  # floor: a tiny capture carries no fee
    assert (void["fn"], void["idem_key"]) == ("void", escrow.idem_key("d-1", "void"))


@pytest.mark.parametrize("bps", [0, 1, 80, 120, 150, 10_000])
@pytest.mark.parametrize("amount", [1, 3, 83, 4_499_999, 4_500_000, 5_000_000, 125_000_000_000_000_000_000])
def test_capture_fee_is_always_accepted_by_the_contract_check(bps, amount):
    """The fee and receiver we pass satisfy AuthCaptureEscrow v1.1 _validateFee for every tariff and amount,
    including a fee that rounds to 0 (T0 at 0 bps) with the clearing address configured as feeReceiver."""
    pi = {"minFeeBps": bps, "maxFeeBps": bps, "feeReceiver": config.BANK_CLEARING_ADDR}
    fee, receiver = escrow.capture_fee(pi, amount)
    assert fee == amount * bps // 10_000 and escrow.contract_fee_error(pi, amount, fee, receiver) is None
    assert receiver.lower() == config.BANK_CLEARING_ADDR.lower()


def test_contract_fee_check_rejects_what_the_contract_rejects():
    pi = {"minFeeBps": 120, "maxFeeBps": 120, "feeReceiver": config.BANK_CLEARING_ADDR}
    assert escrow.contract_fee_error(pi, 4_499_999, 54_000, config.BANK_CLEARING_ADDR).startswith("FeeAmountOutOfRange")
    assert escrow.contract_fee_error(pi, 4_499_999, 53_999, config.BANK_CLEARING_ADDR) is None  # floor, not round
    zero_bps = {**pi, "minFeeBps": 0, "maxFeeBps": 0}
    assert escrow.contract_fee_error(zero_bps, 5_000_000, 0, escrow.ZERO).startswith("InvalidFeeReceiver")  # old bug
    open_receiver = {**zero_bps, "feeReceiver": escrow.ZERO}  # the C-fee PaymentInfos
    assert escrow.capture_fee(open_receiver, 5_000_000) == (0, escrow.ZERO)
    assert escrow.contract_fee_error(open_receiver, 5_000_000, 0, escrow.ZERO) is None
    with pytest.raises(escrow.EscrowError):
        escrow.capture_fee({**pi, "feeReceiver": escrow.ZERO}, 5_000_000)


def test_t0_capture_passes_the_configured_receiver_with_a_zero_fee(deal):
    db.run("UPDATE deals SET terms_json=? WHERE deal_id='d-1'", (db.dumps({**TERMS, "rule": "A", "tier": "T0",
                                                                             "fee_bps": 0}),))
    escrow.authorize("d-1")
    escrow.capture("d-1", 5_000_000, 1)
    amount, fee, receiver = deal["sender"].sent[1]["args"][1:]
    assert (amount, fee) == (5_000_000, 0) and receiver.lower() == config.BANK_CLEARING_ADDR.lower()


def test_paid_counts_only_payment_infos_with_the_seller_as_receiver(deal):
    escrow.authorize("d-1")
    h = db.one("SELECT payment_hash FROM deals WHERE deal_id='d-1'")["payment_hash"]
    db.run("UPDATE payment_infos SET captured=4500000 WHERE payment_hash=?", (h,))
    db.run("INSERT INTO payment_infos (payment_hash, deal_id, n, purpose, receiver, max_amount, pi_json, captured,"
           " authorized, created_at) VALUES ('0xfee','d-1',100,'fee_buyer',?,50000,'{}',50000,1,0)",
           (config.BANK_CLEARING_ADDR,))
    assert escrow.paid("d-1") == 4_500_000  # a C dispute fee paid to the clearing address is not "paid to seller"


def test_no_capture_at_or_after_authorization_expiry(deal):
    escrow.authorize("d-1")
    pi = escrow.stored_payment_info("d-1")
    pi["authorizationExpiry"] = int(time.time()) + 5  # inside the safety margin
    db.run("UPDATE deals SET payment_info_json=? WHERE deal_id='d-1'", (db.dumps(pi),))
    with pytest.raises(escrow.EscrowError, match="authorizationExpiry"):
        escrow.capture("d-1", 4_500_000, 1)
    assert [s["fn"] for s in deal["sender"].sent] == ["authorize"]  # nothing was sent


@pytest.mark.parametrize("amount", [0, -1, 5_000_001])
def test_bad_capture_amount_sends_nothing(deal, amount):
    escrow.authorize("d-1")
    with pytest.raises(escrow.EscrowError):
        escrow.capture("d-1", amount, 1)
    assert len(deal["sender"].sent) == 1


@pytest.mark.parametrize("to_seller,expected", [(4_500_000, ["capture", "void"]), (5_000_000, ["capture"]),
                                                (0, ["void"])])
def test_finish_captures_before_the_final_void(deal, to_seller, expected):
    escrow.authorize("d-1")
    escrow.finish("d-1", to_seller)
    assert [s["fn"] for s in deal["sender"].sent[1:]] == expected


def test_authorize_after_pre_approval_expiry_refused(deal):
    pi = escrow.payment_info(db.one("SELECT * FROM deals WHERE deal_id='d-1'"), now=int(time.time()) - 700)
    assert pi["preApprovalExpiry"] < time.time()
    with pytest.raises(escrow.EscrowError, match="preApprovalExpiry"):
        escrow.authorize("d-1")
    assert deal["sender"].sent == []
