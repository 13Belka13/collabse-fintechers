# Czech VAT payer register, SOAP getStatusNespolehlivyPlatce (SPEC §6.4 p.3): httpx + f-string + xml.etree.
# DIČ without "CZ". Answer: unreliable payer ANO/NE/NENALEZEN and the published bank accounts
# (standardniUcet predcisli?/cislo/kodBanky, or nestandardniUcet = IBAN), normalised to "pre-num/bank".
import xml.etree.ElementTree as ET

from bank import codes
from bank.screening.accounts import normalize_account
from bank.screening.ares import RegistryError

ENDPOINT = "https://adisrws.mfcr.cz/dpr/axis2/services/rozhraniCRPDPH.rozhraniCRPDPHSOAP"
NS = "http://adis.mfcr.cz/rozhraniCRPDPH/"
# Public lookup page for people (the SOAP endpoint itself is not human-readable).
SOURCE_URL = "https://adisspr.mfcr.cz/dpr/DphReg"
STATUS_TEXT = {1: "too many DIČ in one request", 2: "maintenance window 0:00-0:10", 3: "service unavailable"}


def envelope(dic: str) -> str:
    return (f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:roz="{NS}">'
            f"<soapenv:Body><roz:StatusNespolehlivyPlatceRequest><roz:dic>{dic}</roz:dic>"
            f"</roz:StatusNespolehlivyPlatceRequest></soapenv:Body></soapenv:Envelope>")


def plain_dic(dic: str) -> str:
    d = dic.strip().upper().removeprefix("CZ")
    if not d.isdigit():
        raise ValueError(f"bad DIČ {dic!r}")
    return d


def fetch(dic: str, client) -> str:
    r = client.post(ENDPOINT, content=envelope(plain_dic(dic)).encode(),
                    headers={"Content-Type": "text/xml; charset=utf-8",
                             "SOAPAction": f'"{NS}getStatusNespolehlivyPlatce"'})
    if r.status_code != 200:
        raise RegistryError(f"VAT register HTTP {r.status_code}")
    return r.text


def parse(xml: str) -> dict:
    root = ET.fromstring(xml)
    status = root.find(f".//{{{NS}}}status")
    code = int(status.get("statusCode")) if status is not None else -1
    if code != 0:
        raise RegistryError(f"VAT register status {code}: {STATUS_TEXT.get(code, 'no status')}")
    payer = root.find(f".//{{{NS}}}statusPlatceDPH")
    unreliable = payer.get("nespolehlivyPlatce") if payer is not None else "NENALEZEN"
    accounts = []
    for ucet in root.iter(f"{{{NS}}}ucet"):
        std = ucet.find(f"{{{NS}}}standardniUcet")
        nonstd = ucet.find(f"{{{NS}}}nestandardniUcet")
        if std is not None:
            pre = std.get("predcisli")
            raw = f"{pre + '-' if pre else ''}{std.get('cislo')}/{std.get('kodBanky')}"
        elif nonstd is not None:
            raw = nonstd.get("cislo")
        else:
            continue
        try:
            accounts.append(normalize_account(raw))
        except ValueError:
            accounts.append(raw.replace(" ", ""))  # foreign IBAN: keep as published
    return {"unreliable": unreliable, "published_accounts": accounts,
            "answered_on": status.get("odpovedGenerovana")}


def reasons(p: dict) -> list[dict]:
    if p["unreliable"] == "ANO":
        return [codes.reason("vat_unreliable_payer", True, "The VAT register lists the seller as an unreliable payer.",
                             SOURCE_URL, nespolehlivyPlatce="ANO")]
    return []
