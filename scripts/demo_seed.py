# Seed the bank DB with demo data for the UI on a simulated chain: the five §13 scenarios, the VAT risk
# acknowledged once for Beta in EURC (scene 1), and a dispute half-way (one seller objection, waiting for the bank).
# Usage: MOCK=1 python -m scripts.demo_seed      then: DEMO_SIM=1 uvicorn bank.app:app --port 8000
# (DEMO_SIM=1 keeps the simulated chain in the server, so the timer can finish holdbacks and disputes.)
import os

os.environ.setdefault("BANK_TIMER", "0")


def main():
    from bank import db, deals, disputes, mandate
    from scripts import demo_scenarios, demo_sim, seed
    seed.seed()
    demo_sim.install()
    m = mandate.ensure_demo()
    out = {}
    first = demo_scenarios.run("honest")  # Beta is a VAT payer: EURC needs the human's acknowledgement once
    deal_id = first["deals"].get("s-beta")
    if deal_id and db.one("SELECT state FROM deals WHERE deal_id=?", (deal_id,))["state"] == "awaiting_human":
        mandate.acknowledge_vat(m["mandate_id"], "30181020", "EURC", "human:alfa")
        deals.pay(deal_id)
    out["honest"] = deal_id
    for name in ("fake_data", "attack", "suspicious", "tczk"):
        out[name] = demo_scenarios.run(name)["deals"]
    gama = out["fake_data"].get("s-gama")
    d = disputes.get(gama) if gama else None
    if d and d["items"]:
        disputes.seller_objection(gama, d["items"][0]["placeId"], "The warehouse moved to Brno last month; "
                                  "the listing is out of date.", "human:gama")
    print("seeded:", out, "into", db.config.DB_PATH)


if __name__ == "__main__":
    main()
