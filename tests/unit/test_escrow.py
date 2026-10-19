# Escrow operator without a chain (SPEC §5): PaymentInfo values and persistence order, the buyer's
# ReceiveWithAuthorization, idem keys, no capture after authorizationExpiry, capture before the final void.
# The same code against the real contracts on an anvil fork: test_escrow_fork.py.
import time

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from bank import chain, config, db, escrow

BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"


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
    db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, quote_json, asset, amount, state, created_at)"
           " VALUES ('d-1','alfa','s-beta',?,'EURC',5000000,'quoted',?)",
           (db.dumps({"pay_to": BETA, "asset": "EURC", "amount": "5000000"}), time.time()))
    sender = FakeSender("d-1")
    monkeypatch.setattr(chain, "sender", lambda pk: sender)
    return {"operator": operator, "buyer": buyer, "sender": sender}


def test_payment_info_values_and_persisted(deal):
    now = 1_800_000_000
    pi = escrow.payment_info(db.one("SELECT * FROM deals WHERE deal_id='d-1'"), now=now)
    assert [n for n, _ in escrow.PI_FIELDS] == list(pi)  # field order = hashing order (§5.1)
    assert pi["operator"] == deal["operator"].address and pi["payer"] == deal["buyer"].address
    assert pi["receiver"] == BETA and pi["token"] == config.EURC and pi["maxAmount"] == 5_000_000
    assert now < pi["preApprovalExpiry"] <= pi["authorizationExpiry"] <= pi["refundExpiry"]
    assert (pi["minFeeBps"], pi["maxFeeBps"], pi["feeReceiver"]) == (0, 0, escrow.ZERO)
    row = db.one("SELECT * FROM deals WHERE deal_id='d-1'")
    assert db.loads(row["payment_info_json"]) == pi and row["payment_hash"] == escrow.payment_hash(pi)


def test_new_salt_each_attempt_changes_hash(deal):
    d = db.one("SELECT * FROM deals WHERE deal_id='d-1'")
    a, b = escrow.payment_info(d, now=1), escrow.payment_info(d, now=1)
    assert a["salt"] != b["salt"] and escrow.payment_hash(a) != escrow.payment_hash(b)


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
    assert sent["idem_key"] == "d-1:authorize" and sent["kind"] == "authorize"
    pi = escrow.stored_payment_info("d-1")
    assert sent["args"][0] == escrow.as_tuple(pi) and sent["args"][1] == 5_000_000
    assert sent["args"][2] == config.COLLECTOR
    escrow.authorize("d-1")  # a retry reuses the stored PaymentInfo (same idem key -> same tx in chain.sender)
    assert deal["sender"].sent[1]["args"][0] == sent["args"][0]


def test_capture_and_void_idem_keys_fee_zero(deal):
    escrow.authorize("d-1")
    escrow.capture("d-1", 4_500_000, 1)
    escrow.void("d-1")
    _, cap, void = deal["sender"].sent
    assert (cap["fn"], cap["args"][1:], cap["idem_key"]) == ("capture", (4_500_000, 0, escrow.ZERO), "d-1:capture:1")
    assert (void["fn"], void["idem_key"]) == ("void", "d-1:void")


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
