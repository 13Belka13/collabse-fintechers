# In-process stand-in for the bank HTTP API (docs/INTERFACES "Bank HTTP API for agents") so the buyer loop
# runs offline in MOCK mode and in tests, before/without bank/api.py (stream P). Same paths and shapes;
# pay() = 402 quote from the seller agent -> mandate.decide -> reserve, and nothing goes on chain here.
# Screening answers are fixtures shaped like screening.cached() (stream S); "demo_fixture": True.
from bank import codes, core, journal, mandate
from agents import seller_agent

ARES = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"
VAT = "https://adisspr.mfcr.cz/dpr/DphReg"
SCREENING = {
    "s-beta": {"level": "ok", "reasons": [], "sources": [ARES.format(ico="30181020"), VAT], "demo_fixture": True,
               "vat": {"payer": True, "unreliable": "NE", "published_accounts": ["5638829718/9999"], "source_url": VAT}},
    "s-gama": {"level": "ok", "reasons": [], "sources": [ARES.format(ico="59093994")], "demo_fixture": True,
               "vat": {"payer": False, "unreliable": "NENALEZEN", "published_accounts": []}},
    "s-delta": {"level": "blocked", "sources": [ARES.format(ico="54763363")], "demo_fixture": True,
                "reasons": [codes.reason("ico_mismatch_on_website", True,
                                         "The IČO shown on delta-data.example differs from the IČO in ARES.",
                                         ARES.format(ico="54763363"))],
                "vat": {"payer": True, "unreliable": "NE", "published_accounts": []}},
}
MODES = {"s-beta": "honest", "s-gama": "fake_data", "s-delta": "honest"}


class OfflineBank:
    def __init__(self, modes: dict | None = None, screening: dict | None = None):
        self.modes = {**MODES, **(modes or {})}
        self.screening = {**SCREENING, **(screening or {})}
        self.deals: dict[str, dict] = {}

    @classmethod
    def setup(cls, **kw) -> "OfflineBank":
        from scripts.seed import seed
        seed()
        mandate.ensure_demo()
        return cls(**kw)

    def call(self, method: str, path: str, json=None, params=None):
        parts = path.strip("/").split("/")  # api, sellers|deals|statement, ...
        if (method, parts[1]) == ("GET", "sellers") and len(parts) == 2:
            return [{k: s[k] for k in ("seller_id", "name", "ico", "profile_url")}
                    for s in core.sellers((params or {}).get("category"))]
        if (method, parts[1]) == ("GET", "sellers") and parts[3:] == ["check"]:
            return self.check(parts[2])
        if (method, path) == ("POST", "/api/deals"):
            return self.create_deal(json)
        if method == "POST" and parts[1] == "deals" and parts[3] == "negotiate":
            return self.negotiate(parts[2], json["message"])
        if method == "POST" and parts[1] == "deals" and parts[3] == "pay":
            return self.pay(parts[2])
        if (method, parts[1]) == ("GET", "statement"):
            return mandate.summary(params["mandate_id"])
        raise KeyError(f"no route {method} {path}")

    def check(self, seller_id: str) -> dict:
        scr = self.screening.get(seller_id) or {"level": "review", "reasons": []}
        view = codes.client_view({"status": "x", "code": None, "reasons": scr["reasons"]})
        if view["code"] == codes.Code.UNDER_REVIEW:  # something non-public: the client sees only "review"
            return {"level": "review", "public_reasons": []}
        return {"level": scr["level"], "public_reasons": view["reasons"]}

    def create_deal(self, body: dict) -> dict:
        deal_id = f"d-{len(self.deals) + 1:03d}"
        self.deals[deal_id] = {"deal_id": deal_id, "buyer_id": body["buyer_id"], "seller_id": body["seller_id"],
                               "mandate_id": body["mandate_id"], "task": body["task"], "offer": None,
                               "state": "negotiating"}
        journal.append("bank", "deal_created", {"seller_id": body["seller_id"]}, deal_id=deal_id)
        return {"deal_id": deal_id}

    def negotiate(self, deal_id: str, message: str) -> dict:
        deal = self.deals[deal_id]
        seller = core.seller(deal["seller_id"])
        r = seller_agent.reply(deal, message, self.modes.get(deal["seller_id"], "honest"), seller=seller)
        deal["offer"], deal["state"] = r["offer"], "offered"
        journal.append("agent:seller", "negotiate_reply", {"source": "untrusted:seller", "reply_text": r["reply_text"],
                                                           "offer": r["offer"]}, deal_id=deal_id)
        return r

    def pay(self, deal_id: str) -> dict:
        deal = self.deals[deal_id]
        seller = core.seller(deal["seller_id"])
        quote = (seller_agent.quote(deal["offer"], seller["wallet_address"], self.modes.get(deal["seller_id"], "honest"))
                 if deal["offer"] else {})
        d = mandate.decide(deal["mandate_id"], deal, quote, self.screening.get(deal["seller_id"]))
        if d["status"] == "ok":
            r = mandate.reserve(deal["mandate_id"], deal_id, d["eur_cents"])
            d = d if r["status"] == "ok" else r
        deal["state"] = "authorized" if d["status"] == "ok" else ("awaiting_human" if d["status"] ==
                                                                   "requires_user_action" else "refused")
        journal.append("bank", "pay_decision", {"status": d["status"], "code": d["code"], "quote": quote,
                                                "reasons": d["reasons"]}, deal_id=deal_id)
        return codes.client_view(d)
