# Bank core: client companies, wallet <-> account binding, seller registry (SPEC §1 "core").
# The agent never sees keys; wallet_key_ref names the .env variable holding a testnet key.
import os

from web3 import Web3

from bank import db


def company(company_id: str) -> dict | None:
    return db.one("SELECT * FROM companies WHERE id=?", (company_id,))


def company_by_ico(ico: str) -> dict | None:
    return db.one("SELECT * FROM companies WHERE ico=?", (ico,))


def company_by_wallet(address: str) -> dict | None:
    return db.one("SELECT * FROM companies WHERE lower(wallet_address)=lower(?)", (address,))


def bound_account(address: str) -> str | None:
    """The bank account a wallet is bound to (tCZK: the wallet *is* that account in token form)."""
    c = company_by_wallet(address)
    return c["account_number"] if c else None


def seller(seller_id: str) -> dict | None:
    return db.one("SELECT s.*, c.name, c.ico, c.dic, c.wallet_address, c.account_number, c.iban"
                  " FROM sellers s JOIN companies c ON c.id = s.company_id WHERE s.seller_id=?", (seller_id,))


def sellers(category: str | None = None) -> list[dict]:
    sql = ("SELECT s.seller_id, c.name, c.ico, s.profile_url, s.category FROM sellers s"
           " JOIN companies c ON c.id = s.company_id")
    if category:
        return db.rows(sql + " WHERE s.category=? ORDER BY s.seller_id", (category,))
    return db.rows(sql + " ORDER BY s.seller_id")


def wallet_key(company_id: str) -> str:
    """Testnet private key the bank holds for a client (prototype custody, SPEC §1 decision 2)."""
    c = company(company_id)
    ref = c and c["wallet_key_ref"]
    key = os.getenv(ref, "").strip() if ref else ""
    if not key:
        raise RuntimeError(f"no key for {company_id} (TODO(credential): set {ref or 'a key ref'} in .env)")
    return key


def checksum(address: str) -> str:
    return Web3.to_checksum_address(address)


# Google Maps fields a delivery may carry. Everything else (phone numbers, reviews ...) is dropped before
# storing: no personal data in the bank (SPEC §10.2, CLAUDE.md "Data").
DELIVERY_FIELDS = ("title", "categoryName", "totalScore", "reviewsCount", "address", "city", "postalCode", "website",
                   "placeId", "url", "permanentlyClosed")


def clean_delivery_items(items: list) -> list[dict]:
    return [{k: it[k] for k in DELIVERY_FIELDS if k in it} for it in items if isinstance(it, dict)]
