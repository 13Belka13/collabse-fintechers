# Load synthetic demo companies and sellers into the bank DB (idempotent).
# Usage: python -m scripts.seed      Wallet addresses can be overridden by BETA_ADDR / GAMA_ADDR / DELTA_ADDR.
import json
import os

from bank import config, db

FIXTURE = config.ROOT / "fixtures" / "companies.json"


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
    return {"companies": len(data["companies"]), "sellers": len(data["sellers"])}


if __name__ == "__main__":
    print("seeded", seed(), "into", config.DB_PATH)
