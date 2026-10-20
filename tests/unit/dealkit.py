# Shared test kit for deals past the escrow lock: a deal row with fixed terms and a settlement, a seller key, signed
# deliveries through bank.deals.record_delivery, and a fake chain sender (same idem key -> same tx, like
# chain.Sender). The real escrow module builds the calls; nothing touches a chain.
import time
import types

from eth_account import Account

from bank import chain, db, deals, disputes, escrow, mandate, receipts
from seller.app import company_items

ADDR = "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140"
BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"
EURC = "0x808456652fdb597867f38412077A9182bf77359F"
PRICE = 5_000_000
TERMS_PREDICATES = [{"op": "unique", "field": "ico"},
                    {"op": "registry_active", "ico_field": "ico", "name_field": "name", "address_field": "address"},
                    {"op": "regex", "field": "postalCode", "pattern": r"^6\d{2} ?\d{2}$"},
                    {"op": "required", "field": "website"}, {"op": "max_count", "n": 20}]


class FakeSender:
    """Same idem key -> same tx, nothing new on chain (what chain.Sender guarantees)."""

    def __init__(self):
        self.sent, self.by_key, self.fail_next, self.fail_always = [], {}, None, None

    def send(self, call, **kw):
        key = kw["idem_key"]
        if key in self.by_key:
            return self.by_key[key]
        if self.fail_always:  # refused before anything was signed (e.g. gas estimation reverts)
            raise self.fail_always
        args = call.args
        self.sent.append((call.fn_name, args[1] if call.fn_name in ("capture", "authorize") else None, key,
                          args[2] if call.fn_name == "capture" else None))
        tx = {"tx_hash": "0x%064x" % len(self.sent), "link": chain.tx_link("0x%064x" % len(self.sent)),
              "status": "mined"}
        self.by_key[key] = tx
        if self.fail_next:  # the tx went out but the answer was lost (RPC timeout)
            err, self.fail_next = self.fail_next, None
            raise err
        return tx

    def money(self):
        return [(fn, amount) for fn, amount, _, _ in self.sent]


def world(monkeypatch):
    sender = FakeSender()
    monkeypatch.setattr(chain, "sender", lambda pk: sender)
    mandate.ensure_demo()
    key = Account.create()
    receipts.register_key("s-beta", key.address, now=0)
    return types.SimpleNamespace(sender=sender, key=key)


def make_deal(w, rule="B", b_pct=10, deal_id="d-test", window_s=20.0, deadline_s=300, count=20,
              predicates=None, asset="EURC", amount=PRICE, tier=None, now=None, task_hash="t-1", buyer="alfa",
              seller="s-beta", b_slot=1, reserve=True):
    """A deal right after a successful pay(): terms fixed, PaymentInfo stored and authorized, settlement open."""
    now = time.time() if now is None else now
    preds = list(TERMS_PREDICATES if predicates is None else predicates)
    tier = tier or ("T2" if rule == "C" else "T1")
    offer = {"offer_id": "o-" + deal_id, "deal_id": deal_id, "count": count, "prices": {asset: str(amount)},
             "predicates": preds, "rule": rule, "b_pct": b_pct, "window_s": window_s, "deadline_s": deadline_s}
    terms = {"rule": rule, "b_pct": b_pct if rule != "A" else 0, "tier": tier, "window_s": window_s if rule != "A" else 0,
             "deadline_s": deadline_s, "fee_bps": 0, "count": count, "predicates": preds, "task_hash": task_hash,
             "category": "data_purchase", "b_slot": b_slot}
    task = {"category": "data_purchase", "count": count, "predicates": preds, "deadline_s": deadline_s,
            "hash": task_hash}
    db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, mandate_id, offer_json, criteria_json, terms_json, asset,"
           " amount, quote_json, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (deal_id, buyer, seller, "m-alfa-1", db.dumps(offer), db.dumps(task), db.dumps(terms), asset, amount,
            db.dumps({"pay_to": BETA, "amount": amount}), "quoted", now))
    pi = {"operator": ADDR, "payer": ADDR, "receiver": BETA, "token": EURC, "maxAmount": amount,
          "preApprovalExpiry": int(now) + 600, "authorizationExpiry": int(now) + 40 * 86400,
          "refundExpiry": int(now) + 70 * 86400, "minFeeBps": 0, "maxFeeBps": 0, "feeReceiver": "0x" + "0" * 40,
          "salt": str(int(receipts.sha256(deal_id.encode()).hex()[:16], 16))}
    h = escrow.payment_hash(pi)
    db.run("UPDATE deals SET payment_info_json=?, payment_hash=?, state='authorized' WHERE deal_id=?",
           (db.dumps(pi), h, deal_id))
    db.run("INSERT INTO payment_infos (payment_hash, deal_id, n, purpose, receiver, max_amount, pi_json, authorized,"
           " created_at) VALUES (?,?,0,'deal',?,?,?,1,?)", (h, deal_id, BETA, amount, db.dumps(pi), now))
    if reserve:
        assert mandate.reserve("m-alfa-1", deal_id, amount // 10_000 if asset == "EURC" else amount // 25)["status"] == "ok"
    disputes.open_(deal_id, terms, now=now)
    return deal_id


def signed(w, deal_id, items, version=1, key=None, nonce=None):
    offer = deals.get(deal_id)["offer"]
    m = {"deal_id": deal_id, "offer_hash": receipts.offer_hash(offer), "version": version, "count": len(items),
         "merkle_root": receipts.merkle_root(items), "nonce": nonce or "0x%064x" % (version * 7 + len(items))}
    return {"manifest": m, "signature": receipts.sign_manifest(m, (key or w.key).key.hex()), "items": items}


def deliver(w, deal_id, mode="honest", count=20, version=1, now=None, items=None):
    items = company_items(count, mode) if items is None else items
    out = deals.record_delivery(deal_id, signed(w, deal_id, items, version), now=now)
    disputes.advance(deal_id, now=now)
    return out


def captures(w):
    return [amount for fn, amount, _, _ in w.sender.sent if fn == "capture"]


def fns(w):
    return [fn for fn, *_ in w.sender.sent]


def reservation(deal_id):
    return db.one("SELECT * FROM reservations WHERE deal_id=?", (deal_id,))


def state(deal_id):
    return db.one("SELECT state FROM deals WHERE deal_id=?", (deal_id,))["state"]
