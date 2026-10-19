# EU consolidated sanctions list (SPEC §6.4 p.5): companies only (classificationCode "E"),
# names from nameAlias@wholeName, NFKD normalisation, exact + rapidfuzz token_set_ratio >= 90,
# identification numbers compared with IČO/DIČ. Matching is local: the 25 MB XML is reduced once
# to a compact index (fixtures/sanctions/eu_companies.json, committed snapshot) so a check needs no network.
# Refresh:  python -m bank.screening.sanctions --refresh   (downloads the XML, ~70 s, rebuilds the index)
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from functools import lru_cache

from rapidfuzz import fuzz

from bank import codes, config

XML_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"
           "?token=dG9rZW4tMjAxNw")
INDEX = config.ROOT / "fixtures" / "sanctions" / "eu_companies.json"
FUZZY_MIN = 90
NS = "{http://eu.europa.ec/fpi/fsd/export}"

# Legal-form suffixes dropped before comparing (after normalisation: lower case, no diacritics/punctuation).
SUFFIXES = {
    "sro", "spol", "s", "r", "o", "as", "a", "ks", "vos", "se", "zs", "ltd", "limited", "llc", "llp", "inc",
    "corp", "corporation", "co", "company", "gmbh", "ag", "sa", "sas", "sarl", "srl", "spa", "bv", "nv", "plc",
    "oy", "ab", "jsc", "pjsc", "ojsc", "cjsc", "ooo", "oao", "zao", "pao", "ao", "fze", "fzco", "dmcc",
    "sp", "z", "oo", "kft", "doo", "ad", "eood", "ood", "lp", "pte", "pvt", "bhd", "sdn",
}


def normalize(name: str) -> str:
    """'ČEZ, a. s.' -> 'cez'. Case, diacritics (NFKD), punctuation and legal suffixes removed."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[^\w\s]", " ", s).replace("_", " ")
    return " ".join(t for t in s.split() if t not in SUFFIXES)


def build_index(xml_path) -> dict:
    """Reduce the full XML to the company entries we match on."""
    root = ET.parse(xml_path).getroot()
    entities = []
    for e in root.iter(f"{NS}sanctionEntity"):
        st = e.find(f"{NS}subjectType")
        if st is None or st.get("classificationCode") != "E":
            continue
        names = sorted({a.get("wholeName", "").strip() for a in e.iter(f"{NS}nameAlias") if a.get("wholeName")})
        ids = sorted({i.get("number", "").strip() for i in e.iter(f"{NS}identification") if i.get("number")})
        reg = e.find(f"{NS}regulation")
        entities.append({"ref": e.get("euReferenceNumber"), "logical_id": e.get("logicalId"),
                         "programme": reg.get("programme") if reg is not None else None,
                         "names": names, "ids": ids})
    return {"source_url": XML_URL, "generated": root.get("generationDate"), "count": len(entities),
            "entities": entities}


@lru_cache(maxsize=1)
def index() -> dict:
    data = json.loads(INDEX.read_text(encoding="utf-8"))
    for ent in data["entities"]:
        ent["norm"] = [n for n in {normalize(x) for x in ent["names"]} if len(n) >= 4]
        ent["id_digits"] = {re.sub(r"\D", "", i) for i in ent["ids"]} - {""}
    return data


def screen(name: str | None, ico: str | None = None, dic: str | None = None) -> dict:
    """-> {"matches": [...], "reasons": [...]}. All sanctions reasons are NON-public (§6.7): the client
    sees only "under review". Exact name or IČO/DIČ hit -> "sanctions_match"; fuzzy -> "sanctions_near_match"."""
    data = index()
    target = normalize(name or "")
    numbers = {re.sub(r"\D", "", x) for x in (ico, dic) if x} - {""}
    matches = []
    for ent in data["entities"]:
        hit = None
        if numbers & ent["id_digits"]:
            hit = ("id", 100, None)
        elif target:
            for n in ent["norm"]:
                if n == target:
                    hit = ("exact", 100, n)
                    break
                score = fuzz.token_set_ratio(target, n)
                # token_set_ratio is 100 when one token set contains the other ("delta" vs "delta shipping"):
                # one shared word is not a near-match, so the shorter side must have 2+ tokens.
                if score >= FUZZY_MIN and min(len(target.split()), len(n.split())) >= 2:
                    if hit is None or score > hit[1]:
                        hit = ("near", score, n)
        if hit:
            matches.append({"kind": hit[0], "score": hit[1], "matched": hit[2], "ref": ent["ref"],
                            "programme": ent["programme"], "names": ent["names"][:3]})
    reasons = []
    strong = [m for m in matches if m["kind"] in ("exact", "id")]
    near = [m for m in matches if m["kind"] == "near"]
    src = data["source_url"]
    if strong:
        m = strong[0]
        reasons.append(codes.reason("sanctions_match", False,
                                    f"Name or registration number matches EU sanctions entry {m['ref']}.",
                                    src, match=m, list_generated=data["generated"]))
    elif near:
        m = max(near, key=lambda x: x["score"])
        reasons.append(codes.reason("sanctions_near_match", False,
                                    f"Name is similar ({m['score']:.0f}/100) to EU sanctions entry {m['ref']}.",
                                    src, match=m, list_generated=data["generated"]))
    return {"matches": matches, "reasons": reasons, "list_generated": data["generated"]}


def refresh(xml_path=None):
    """Download the XML (unless a local copy is given) and rewrite the committed index."""
    import httpx
    if xml_path is None:
        xml_path = config.ROOT / "data" / "eu_sanctions.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", XML_URL, timeout=180, follow_redirects=True) as r:
            r.raise_for_status()
            with open(xml_path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    data = build_index(xml_path)
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    index.cache_clear()
    return data["count"], data["generated"]


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        i = sys.argv.index("--refresh")
        print("companies:", *refresh(sys.argv[i + 1] if len(sys.argv) > i + 1 else None))
