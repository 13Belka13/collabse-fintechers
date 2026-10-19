# Hackathon datasets on Mostly Right (SPEC §6.4 p.6): czech_companies KYB flags and counterparty_records
# (licences, regulator warnings, sanctions). Only with config.MOSTLYRIGHT_API_KEY; without it the source is
# skipped (TODO(credential)). No row = "unknown", not "clean" (≈475k s.r.o. are missing) -> ARES decides.
from bank import codes, config

API = "https://mostlyright.md/api/v2/public/tables/{table}/query"
CZECH_COMPANIES = "4cf4dc81-377d-4703-8ea6-70cd5a5e21d2"
COUNTERPARTY_RECORDS = "b0bc56bb-2e3b-4304-a46d-eb94ceaeed9f"
SOURCE_URL = "https://mostlyright.md/datasets/czech-companies-with-kyb-risk-flags"
RECORDS_URL = "https://mostlyright.md/datasets/eu-financial-firm-licences-and-regulator-warnings"
COMPANY_COLUMNS = ["ico", "companies_at_seat", "generic_trade_licence_only", "foreign_directors_count",
                   "has_insolvency_record", "in_liquidation", "has_enforcement_record", "cnb_licensed"]
RECORD_COLUMNS = ["record_type", "name", "national_id", "lei", "website_domain", "in_vop"]
MASS_SEAT = 100  # companies registered at one address: a shell-company signal, not proof


def enabled() -> bool:
    return bool(config.MOSTLYRIGHT_API_KEY)


def _query(client, table: str, columns: list, column: str, value: str) -> list[dict]:
    r = client.post(API.format(table=table), headers={"x-api-key": config.MOSTLYRIGHT_API_KEY},
                    json={"columns": columns, "filters": [{"column": column, "operator": "eq", "value": value}],
                          "limit": 20})
    r.raise_for_status()
    body = r.json()
    # TODO(credential): confirm the response envelope on the first keyed call; accept both common shapes.
    if isinstance(body, list):
        return body
    return body.get("rows") or body.get("data") or []


def fetch(ico: str, client) -> dict:
    return {"company": _query(client, CZECH_COMPANIES, COMPANY_COLUMNS, "ico", ico),
            "records": _query(client, COUNTERPARTY_RECORDS, RECORD_COLUMNS, "national_id", ico)}


def _yes(v) -> bool:
    return v is True or str(v).lower() in ("true", "1", "yes", "t")


def reasons(raw: dict) -> list[dict]:
    out = []
    row = (raw.get("company") or [None])[0]
    if row:
        if _yes(row.get("has_insolvency_record")):
            out.append(codes.reason("insolvency_record", True, "Dataset: insolvency record.", SOURCE_URL))
        if _yes(row.get("in_liquidation")):
            out.append(codes.reason("in_liquidation", True, "Dataset: company in liquidation.", SOURCE_URL))
        if _yes(row.get("has_enforcement_record")):
            out.append(codes.reason("enforcement_record", True, "Dataset: enforcement (exekuce) record.", SOURCE_URL))
        if int(row.get("companies_at_seat") or 0) >= MASS_SEAT:
            out.append(codes.reason("mass_registered_address", False,
                                    f"{row['companies_at_seat']} companies registered at the same seat.", SOURCE_URL,
                                    companies_at_seat=row["companies_at_seat"]))
    for rec in raw.get("records") or []:
        kind = str(rec.get("record_type") or "").lower()
        if "sanction" in kind:
            out.append(codes.reason("sanctions_match", False, "Dataset: counterparty is on a sanctions list.",
                                    RECORDS_URL, record_type=kind))
        elif "warning" in kind:
            out.append(codes.reason("regulator_warning", True, "A financial regulator published a warning.",
                                    RECORDS_URL, record_type=kind))
    return out
