# ISIR insolvency register, SOAP getIsirWsCuzkData (SPEC §6.4 p.4). Port 8443 is required,
# SOAPAction is empty, an empty result is kodChyby=WS2, dates look like "2016-05-09Z".
import xml.etree.ElementTree as ET
from datetime import date

from bank import codes
from bank.screening.ares import RegistryError

ENDPOINT = "https://isir.justice.cz:8443/isir_cuzk_ws/IsirWsCuzkService"
NS = "http://isirws.cca.cz/types/"
SOURCE_URL = "https://isir.justice.cz/isir/common/index.do"
# Proceedings that are still running (druhStavKonkursu). Finished ones (VYRIZENA, PRAVOMOCNA, ODSKRTNUTA,
# ZRUŠENO VS, MYLNÝ ZÁP., OBZIVLA ...) are history, not a reason to refuse; an unknown state counts as running.
FINISHED = {"VYRIZENA", "PRAVOMOCNA", "ODSKRTNUTA", "ZRUŠENO VS", "ZRUSENO VS", "MYLNÝ ZÁP.", "MYLNY ZAP.",
            "OBZIVLA", "K-PO ZRUŠ", "K-PO ZRUS"}


def envelope(ico: str) -> str:
    return (f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:typ="{NS}">'
            f"<soapenv:Body><typ:getIsirWsCuzkDataRequest><ic>{ico}</ic><maxPocetVysledku>10</maxPocetVysledku>"
            f"</typ:getIsirWsCuzkDataRequest></soapenv:Body></soapenv:Envelope>")


def fetch(ico: str, client) -> str:
    r = client.post(ENDPOINT, content=envelope(ico).encode(),
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'})
    if r.status_code != 200:
        raise RegistryError(f"ISIR HTTP {r.status_code}")
    return r.text


def _date(s: str | None) -> str | None:
    return date.fromisoformat(s.rstrip("Z")).isoformat() if s else None


def parse(xml: str) -> dict:
    root = ET.fromstring(xml)
    err = root.findtext(".//stav/kodChyby")
    if err and err != "WS2":
        raise RegistryError(f"ISIR error {err}: {root.findtext('.//stav/textChyby')}")
    records = []
    for d in root.iter("data"):
        state = (d.findtext("druhStavKonkursu") or "").strip()
        records.append({"case": f"{d.findtext('druhVec')} {d.findtext('bcVec')}/{d.findtext('rocnik')}",
                        "court": d.findtext("nazevOrganizace"), "state": state,
                        "running": state.upper() not in FINISHED,
                        "started": _date(d.findtext("datumPmZahajeniUpadku")),
                        "url": d.findtext("urlDetailRizeni")})
    return {"records": records}


def reasons(p: dict) -> list[dict]:
    running = [r for r in p["records"] if r["running"]]
    if not running:
        return []
    r = running[0]
    return [codes.reason("insolvency_record", True,
                         f"Insolvency proceedings {r['case']} ({r['state']}) at {r['court']}.",
                         r["url"] or SOURCE_URL, case=r["case"], state=r["state"], started=r["started"])]
