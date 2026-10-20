# Seed the bank DB with demo data for the UI on a simulated chain: the §13 scenarios and the no-tipping-off one
# (sanctions_hit, before tczk so the final scene stays the latest deal on /story), the VAT risk
# acknowledged once for Beta in EURC (scene 1), and a B claim on the fake-data deal (refund without investigation).
# Accounts (scripts/seed.py): Alfa 10 000,00 Kč, Beta 0,00 Kč with tCZK paid out to its account after each capture.
# Usage: MOCK=1 python -m scripts.demo_seed      then: DEMO_SIM=1 uvicorn bank.app:app --port 8000
# (DEMO_SIM=1 keeps the simulated chain in the server, so the timer can finish the claim windows.)
import os

os.environ.setdefault("BANK_TIMER", "0")


def main():
    import time

    from bank import db, deadlines, deals, disputes, mandate
    from scripts import demo_scenarios, demo_sim, seed
    demo_sim.install()  # first: the seed records the simulator's tCZK supply, never the live chain's
    seed.seed()
    m = mandate.ensure_demo()
    out = {}
    first = demo_scenarios.run("honest")  # Beta is a VAT payer: EURC needs the human's acknowledgement once
    deal_id = first["deals"].get("s-beta")
    if deal_id and db.one("SELECT state FROM deals WHERE deal_id=?", (deal_id,))["state"] == "awaiting_human":
        mandate.acknowledge_vat(m["mandate_id"], "30181020", "EURC", "human:alfa")
        deals.pay(deal_id)
    out["honest"] = deal_id
    deadlines.tick(now=time.time() + 3600)  # the claim window of the first deal ends: its B slot is free again
    out["fake_data"] = demo_scenarios.run("fake_data")["deals"]
    gama = out["fake_data"].get("s-gama")
    s = disputes.get(gama) if gama else None
    if s and s["rule"] == "B" and not s["claims"] and s["window_end_at"]:
        try:
            disputes.file_b(gama, [[1, 0]], "wrong_data", "agent:buyer")
        except disputes.DisputeError as e:
            print("B claim not filed:", e)
    deadlines.tick(now=time.time() + 3600)
    for name in ("attack", "suspicious", "sanctions_hit", "tczk"):
        out[name] = demo_scenarios.run(name)["deals"]
    print("seeded:", out, "into", db.config.DB_PATH)


if __name__ == "__main__":
    main()
