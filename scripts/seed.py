# Load synthetic demo companies and sellers into the bank DB (idempotent).
# Usage: python -m scripts.seed      Wallet addresses can be overridden by <ID>_ADDR (BETA_ADDR ... EPSILON_ADDR).
import json
import os

from bank import config, db

FIXTURE = config.ROOT / "fixtures" / "companies.json"
# simulated core accounts (haléře) and where a seller's tCZK goes after a capture (bank/ramp.py); set on first seed
# only, so a restart never resets a balance
ACCOUNTS = {"alfa": (1_000_000, "wallet"), "beta": (0, "account")}
# deals a client closed with the bank before the agents (synthetic history): Alfa is an established client, so the
# new-buyer B limit (bank/disputes.py, < 20 deals) does not apply to the demo buyer; the pair limit and ratio do
PRIOR_DEALS = {"alfa": 24}


def seed(path=FIXTURE) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    with db.immediate() as c:
        for co in data["companies"]:
            wallet = os.getenv(f"{co['id'].upper()}_ADDR", "").strip() or co["wallet_address"]
            c.execute("INSERT INTO companies (id, name, ico, dic, account_number, iban, wallet_address, wallet_key_ref,"
                      " role, synthetic) VALUES (?,?,?,?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                      " ico=excluded.ico, dic=excluded.dic, account_number=excluded.account_number, iban=excluded.iban,"
                      " wallet_address=excluded.wallet_address, wallet_key_ref=excluded.wallet_key_ref, role=excluded.role",
                      (co["id"], co["name"], co["ico"], co["dic"], co["account_number"], co["iban"], wallet,
                       co["wallet_key_ref"], co["role"]))
        for s in data["sellers"]:
            c.execute("INSERT INTO sellers (seller_id, company_id, category, profile_url, base_url, website_url)"
                      " VALUES (?,?,?,?,?,?) ON CONFLICT(seller_id) DO UPDATE SET company_id=excluded.company_id,"
                      " category=excluded.category, profile_url=excluded.profile_url, base_url=excluded.base_url,"
                      " website_url=excluded.website_url",
                      (s["seller_id"], s["company_id"], s["category"], s["profile_url"], s["base_url"], s["website_url"]))
        for cid, n in PRIOR_DEALS.items():
            c.execute("UPDATE companies SET prior_deals=? WHERE id=?", (n, cid))
        for co in data["companies"]:
            balance, mode = ACCOUNTS.get(co["id"], (0, "wallet"))
            c.execute("INSERT OR IGNORE INTO accounts (company_id, account_number, balance, payout_mode) VALUES (?,?,?,?)",
                      (co["id"], co["account_number"], balance, mode))
    try:  # the tCZK minted outside the ramp, once (reconciliation "bank books = chain")
        from bank import ramp
        ramp.record_initial_supply()
    except Exception as e:  # noqa: BLE001 — a chain outage must not stop the seed; reconciliation then says so
        print(f"initial tCZK supply not recorded: {type(e).__name__}: {str(e)[:120]}")
    return {"companies": len(data["companies"]), "sellers": len(data["sellers"])}


if __name__ == "__main__":
    print("seeded", seed(), "into", config.DB_PATH)
