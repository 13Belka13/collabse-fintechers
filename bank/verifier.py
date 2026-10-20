# Predicate engine and autocodes (MODEL_V2 §6). No model and no arbitrary code: a closed dictionary of predicates
# joined only by AND — an item is accepted when it passes every predicate, so a predicate the seller adds can only
# narrow acceptance. The engine checks what the seller itself declared in the item and what public registries say,
# never "quality". Every predicate on a field also requires the field (absent, null or blank -> CRITERIA_FAIL).
#
# Dictionary (one JSON object per predicate, "op" picks it):
#   {"op": "required", "field": f}
#   {"op": "eq", "field": f, "value": v, "type"?: t}         t: string|number|date|code|enum (default from v)
#   {"op": "eq", "field": f, "snapshot_field": g}            item field equals the bank's source snapshot (§7C.2)
#   {"op": "in", "field": f, "values": [...], "type"?: t}
#   {"op": "range", "field": f, "gt"|"gte"|"lt"|"lte": v, "type"?: "number"|"date"}
#   {"op": "regex", "field": f, "pattern": p}                RE2 only (linear time), search semantics
#   {"op": "json_schema", "schema": {...}}                   depth/size/time limits, no external $ref, RE2 patterns
#   {"op": "unique", "field": f}                             over every accepted item of the deal and of the task
#   {"op": "max_count", "n": n}                              never more paid items than ordered in any case
#   {"op": "registry_active", "ico_field": f, "name_field": g, "address_field"?: h}
#   {"op": "source", "template": t, "key_field": f}          bank-built snapshot URL (bank/snapshots.py)
# Item autocodes: SCHEMA_FAIL, CRITERIA_FAIL, DUPLICATE, REGISTRY_CONTRADICTION, SOURCE_UNAVAILABLE; "pending"
# items (registry or source temporarily unavailable — not the seller's fault) are neither accepted nor rejected.
import re
import time
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

import jsonschema
import re2
from jsonschema import validators

from bank import receipts

OPS = ("required", "eq", "in", "range", "regex", "json_schema", "unique", "max_count", "registry_active", "source")
TYPES = ("string", "number", "date", "code", "enum")
ITEM_CODES = ("SCHEMA_FAIL", "CRITERIA_FAIL", "DUPLICATE", "REGISTRY_CONTRADICTION", "SOURCE_UNAVAILABLE")
DELIVERY_CODES = ("NO_DELIVERY", "SHORT", "BAD_SIGNATURE", "WRONG_DEAL", "ROOT_MISMATCH", "QUOTA_EXCEEDED")
BANK_ERROR_CODES = ("OUT_OF_MANDATE", "DOUBLE_PAYMENT")  # refunded from the bank's money, never the seller's
SELLER_DEFECTS = ("SCHEMA_FAIL", "CRITERIA_FAIL", "DUPLICATE", "REGISTRY_CONTRADICTION")  # §9 "defects" (cross-deal
#                                                    DUPLICATE is excluded there: the seller cannot filter it)
# Personal data never enters the bank (CLAUDE.md "Data"): an item carrying these fields is not paid and not stored.
PERSONAL_FIELDS = ("phone", "phoneUnformatted", "phones", "phonesUncertain", "reviews", "email", "emails")
SCHEMA_MAX_DEPTH, SCHEMA_MAX_BYTES, SCHEMA_TIME_LIMIT_S = 16, 16_384, 0.05
REGEX_MAX_LEN = 512
LEGAL_FORM_WORDS = ("spol s r o", "s r o", "sro", "a s", "as", "k s", "v o s", "z s", "s p", "se", "o p s", "z u")


class PredicateError(ValueError):
    """The predicate list itself is not valid (refused when terms are confirmed or an offer is checked)."""


class Pending(Exception):
    """A registry or source did not answer — the item waits, the bank retries; not the seller's fault (§5.1)."""


# ---- normalization (the same in predicates and in C admissibility, §7C.2) ----

def plain(s) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return " ".join(s.split())


def norm(value, ctype: str):
    """Value in comparable form for the comparison type, or None when it is not of that type."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if ctype in ("string", "enum"):
        return plain(value) if isinstance(value, str) else None
    if ctype == "code":
        return re.sub(r"\s", "", str(value)).upper() if isinstance(value, (str, int)) and not isinstance(value, bool) \
            else None
    if ctype == "number":
        if isinstance(value, bool):
            return None
        try:
            d = Decimal(str(value).strip().replace(",", ".")) if isinstance(value, (int, float, str)) else None
        except InvalidOperation:
            return None
        return d if d is not None and d.is_finite() else None
    if ctype == "date":
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            return None
    raise PredicateError(f"unknown comparison type {ctype!r}")


def type_of(value) -> str:
    return "number" if isinstance(value, (int, float)) and not isinstance(value, bool) else "string"


def norm_company_name(name) -> str:
    """ARES name vs item name: case, diacritics, punctuation and the legal form ignored."""
    s = " " + re.sub(r"[^\w]+", " ", plain(name)).strip() + " "
    for form in sorted(LEGAL_FORM_WORDS, key=len, reverse=True):
        s = s.replace(f" {form} ", " ")
    return " ".join(s.split())


def psc_of(text) -> str | None:
    m = re.search(r"\b(\d{3})\s?(\d{2})\b", str(text or ""))
    return m.group(1) + m.group(2) if m else None


# ---- the predicate list ----

def _schema_depth(node, d=0) -> int:
    if isinstance(node, dict):
        return max([d] + [_schema_depth(v, d + 1) for v in node.values()])
    if isinstance(node, list):
        return max([d] + [_schema_depth(v, d + 1) for v in node])
    return d


def _refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("$ref", "$dynamicRef", "$recursiveRef") and isinstance(v, str):
                yield v
            yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


def _rx(pattern: str):
    if not isinstance(pattern, str) or len(pattern) > REGEX_MAX_LEN:
        raise PredicateError("regex must be a string up to 512 characters")
    try:
        return re2.compile(pattern)
    except Exception as e:  # noqa: BLE001 — re2.error: not RE2 (e.g. a backreference)
        raise PredicateError(f"regex is not RE2: {e}") from e


def validate(predicates: list) -> list[dict]:
    """Check a predicate list (shape, RE2, schema limits). Returns it unchanged or raises PredicateError."""
    if not isinstance(predicates, list):
        raise PredicateError("predicates must be a list")
    for p in predicates:
        if not isinstance(p, dict) or p.get("op") not in OPS:
            raise PredicateError(f"unknown predicate {p!r}: allowed {', '.join(OPS)} (AND only, no or/not)")
        receipts.jcs(p)  # I-JSON, so "verbatim" can be checked byte for byte
        op = p["op"]
        if op in ("required", "eq", "in", "range", "regex", "unique") and not isinstance(p.get("field"), str):
            raise PredicateError(f"{op} needs a field")
        if "type" in p and p["type"] not in TYPES:
            raise PredicateError(f"unknown type {p['type']!r}")
        if op == "eq" and ("value" in p) == ("snapshot_field" in p):
            raise PredicateError("eq needs exactly one of value, snapshot_field")
        if op == "in" and not (isinstance(p.get("values"), list) and p["values"]):
            raise PredicateError("in needs a non-empty values list")
        if op == "range" and not ({"gt", "gte", "lt", "lte"} & set(p)):
            raise PredicateError("range needs gt, gte, lt or lte")
        if op == "regex":
            _rx(p.get("pattern"))
        if op == "json_schema":
            s = p.get("schema")
            if not isinstance(s, dict) or len(receipts.jcs(s)) > SCHEMA_MAX_BYTES or _schema_depth(s) > SCHEMA_MAX_DEPTH:
                raise PredicateError("json_schema must be an object within the size and depth limits")
            if any(not r.startswith("#") for r in _refs(s)):
                raise PredicateError("json_schema may not reference external schemas")
            try:
                _validator_cls().check_schema(s)
            except jsonschema.SchemaError as e:
                raise PredicateError(f"invalid json_schema: {e.message}") from e
        if op == "max_count" and not (isinstance(p.get("n"), int) and p["n"] > 0):
            raise PredicateError("max_count needs a positive n")
        if op == "registry_active" and not (isinstance(p.get("ico_field"), str) and isinstance(p.get("name_field"), str)):
            raise PredicateError("registry_active needs ico_field and name_field")
        if op == "source":
            from bank import snapshots
            if p.get("template") not in snapshots.TEMPLATES or not isinstance(p.get("key_field"), str):
                raise PredicateError("source needs a published template and a key_field")
    return predicates


def verbatim_missing(confirmed: list, offered: list) -> list[dict]:
    """Confirmed predicates whose JCS bytes are not in the offer (§2.1): the seller may add, never drop or edit."""
    have = {receipts.jcs(p) for p in offered}
    return [p for p in confirmed if receipts.jcs(p) not in have]


def fields_named(predicates: list) -> list[str]:
    out = []
    for p in predicates:
        for k in ("field", "ico_field", "name_field", "address_field", "key_field"):
            if isinstance(p.get(k), str) and p[k] not in out:
                out.append(p[k])
    return out


# ---- json_schema with RE2 ----

def _re2_pattern(validator, pattern, instance, schema):
    if validator.is_type(instance, "string") and not _rx(pattern).search(instance):
        yield jsonschema.ValidationError(f"{instance!r} does not match {pattern!r}")


def _re2_pattern_properties(validator, patterns, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    for pattern, sub in patterns.items():
        rx = _rx(pattern)
        for k, v in instance.items():
            if rx.search(k):
                yield from validator.descend(v, sub, path=k, schema_path=pattern)


def _validator_cls():
    base = validators.validator_for({"$schema": "https://json-schema.org/draft/2020-12/schema"})
    return validators.extend(base, {"pattern": _re2_pattern, "patternProperties": _re2_pattern_properties})


def _no_remote(uri):
    raise PredicateError(f"external $ref {uri} is not allowed")


def schema_problem(schema: dict, item) -> str | None:
    cls = _validator_cls()
    try:
        from referencing import Registry
        v = cls(schema, registry=Registry(retrieve=_no_remote))
    except ImportError:  # older jsonschema
        v = cls(schema)
    t0 = time.perf_counter()
    err = next(iter(v.iter_errors(item)), None)
    if time.perf_counter() - t0 > SCHEMA_TIME_LIMIT_S:
        return "schema check over the time limit"
    return None if err is None else err.message[:200]


# ---- fields ----

def get_field(item, path: str):
    """(present, value) for a dotted path; absent, null and blank strings count as missing."""
    cur = item
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    if cur is None or (isinstance(cur, str) and not cur.strip()):  # False and 0 are values, not "missing"
        return False, None
    return True, cur


def unique_key(item, field: str | None) -> str:
    """Hash of the unique field's normalized value; without a `unique` predicate, of the whole item (safest:
    the same item is never paid twice)."""
    if field is None:
        return receipts.hex32(receipts.sha256(receipts.jcs(item)))
    _, v = get_field(item, field)
    n = norm(v, "code") if isinstance(v, (int, str)) and not isinstance(v, bool) else None
    return receipts.hex32(receipts.sha256(receipts.jcs([field, n if n is not None else v])))


# ---- one item against the field predicates ----

def _fail(code: str, p: dict | None, detail: str) -> dict:
    return {"code": code, "predicate": p, "detail": detail}


def _field_pred(p: dict, item, snap: dict | None) -> dict | None:
    present, v = get_field(item, p["field"])
    if not present:
        return _fail("CRITERIA_FAIL", p, f"{p['field']} is missing")
    op = p["op"]
    if op == "required":
        return None
    if op == "eq":
        if "snapshot_field" in p:
            if snap is None:
                raise Pending("snapshot not taken yet")
            sv, ctype = snap.get(p["snapshot_field"]), snap.get(p["snapshot_field"] + ":type", "string")
            a, b = norm(v, ctype), norm(sv, ctype)
            ok = a is not None and a == b
            return None if ok else _fail("CRITERIA_FAIL", p, f"{p['field']} differs from the source")
        if isinstance(p["value"], bool):
            return None if v is p["value"] else _fail("CRITERIA_FAIL", p, f"{p['field']} is not {p['value']}")
        ctype = p.get("type") or type_of(p["value"])
        a = norm(v, ctype)
        return None if a is not None and a == norm(p["value"], ctype) else _fail(
            "CRITERIA_FAIL", p, f"{p['field']} is not {p['value']!r}")
    if op == "in":
        ctype = p.get("type") or type_of(p["values"][0])
        a = norm(v, ctype)
        return None if a is not None and a in {norm(x, ctype) for x in p["values"]} else _fail(
            "CRITERIA_FAIL", p, f"{p['field']} is not one of the allowed values")
    if op == "range":
        ctype = p.get("type", "number")
        a = norm(v, ctype)
        if a is None:
            return _fail("CRITERIA_FAIL", p, f"{p['field']} is not a {ctype}")
        for k, test in (("gt", lambda x, y: x > y), ("gte", lambda x, y: x >= y),
                        ("lt", lambda x, y: x < y), ("lte", lambda x, y: x <= y)):
            if k in p and not test(a, norm(p[k], ctype)):
                return _fail("CRITERIA_FAIL", p, f"{p['field']} is outside the range")
        return None
    if op == "regex":
        return None if isinstance(v, str) and _rx(p["pattern"]).search(v) else _fail(
            "CRITERIA_FAIL", p, f"{p['field']} does not match")
    return None


def _registry_pred(p: dict, item, registry) -> dict | None:
    for k in ("ico_field", "name_field", "address_field"):
        if p.get(k) and not get_field(item, p[k])[0]:
            return _fail("CRITERIA_FAIL", p, f"{p[k]} is missing")
    _, ico = get_field(item, p["ico_field"])
    rec = registry(str(ico).strip())  # raises Pending when the registry did not answer
    if rec is None or not rec.get("found"):
        return _fail("REGISTRY_CONTRADICTION", p, f"IČO {ico} is not in ARES")
    if not rec.get("legal_entity"):
        return _fail("CRITERIA_FAIL", p, "registry check only for legal entities (s.r.o., a.s.)")
    if rec.get("dissolved"):
        return _fail("REGISTRY_CONTRADICTION", p, f"IČO {ico} was dissolved on {rec['dissolved']} (ARES)")
    if rec.get("insolvency"):
        return _fail("REGISTRY_CONTRADICTION", p, f"IČO {ico} has an insolvency record (ISIR)")
    if norm_company_name(get_field(item, p["name_field"])[1]) != norm_company_name(rec.get("name") or ""):
        return _fail("REGISTRY_CONTRADICTION", p, f"IČO {ico} belongs to another company in ARES")
    if p.get("address_field"):
        addr = get_field(item, p["address_field"])[1]
        street = plain(rec.get("street") or "")
        if psc_of(addr) != (rec.get("psc") or psc_of(rec.get("address"))) or (street and street not in plain(addr)):
            return _fail("REGISTRY_CONTRADICTION", p, f"the address does not match the seat of IČO {ico} in ARES")
    return None


def registry_record(ico: str) -> dict | None:
    """ARES + ISIR for an item's IČO from the screening cache (a miss runs the check: registries in parallel,
    2 s, fail-closed). None = not in ARES; raises Pending when ARES or ISIR did not answer."""
    from bank import screening
    res = screening.cached(ico) or screening.check(ico)
    by_code = {r["code"]: r for r in res["reasons"]}
    if "ico_checksum_invalid" in by_code:
        return None
    if any(r["code"] in ("registry_timeout", "registry_error") and r["evidence"].get("source") in ("ares", "isir")
           for r in res["reasons"]) or res.get("registry") is None:
        raise Pending(f"registry did not answer for IČO {ico}")
    reg = res["registry"]
    if not reg.get("found"):
        return None
    return {**reg, "insolvency": "insolvency_record" in by_code}


# ---- the engine ----

def evaluate(items: list, predicates: list, *, ordered: int, accepted_keys=frozenset(), blocked_keys=frozenset(),
             already_accepted: int = 0, registry=None, snapshot=None) -> list[dict]:
    """Every item of one delivery version -> {index, status: accepted|rejected|pending, code, predicate, detail,
    unique_key}. Order: SCHEMA_FAIL, CRITERIA_FAIL (field predicates), REGISTRY_CONTRADICTION, DUPLICATE,
    max_count, then the source snapshot (taken only for items that passed everything else, §5.1).
    accepted_keys: unique keys already accepted in this deal (any version) or in this task; blocked_keys: keys this
    seller already refunded in this task (B/C) — both give DUPLICATE. registry(ico) -> record | None (not found),
    raises Pending; snapshot(index, item, predicate) -> {"status": "ok", "fields": {...}} | "pending" | "unavailable"."""
    validate(predicates)
    by_op = {op: [p for p in predicates if p["op"] == op] for op in OPS}
    ufield = by_op["unique"][0]["field"] if by_op["unique"] else None
    cap = min([ordered] + [p["n"] for p in by_op["max_count"]])
    seen, taken, out = set(accepted_keys), already_accepted, []
    for i, item in enumerate(items):
        res = {"index": i, "status": "rejected", "code": None, "predicate": None, "detail": "", "unique_key": None}
        out.append(res)
        try:
            fail = _check_item(item, by_op, registry)
        except Pending as e:
            res.update(status="pending", detail=str(e))
            continue
        if fail:
            res.update(fail)
            continue
        key = unique_key(item, ufield)
        res["unique_key"] = key
        if key in seen or key in blocked_keys:
            res.update(_fail("DUPLICATE", by_op["unique"][0] if by_op["unique"] else None,
                             "already delivered" if key in seen else "already refunded to this buyer"))
            continue
        if taken >= cap:
            res.update(_fail("CRITERIA_FAIL", by_op["max_count"][0] if by_op["max_count"] else
                             {"op": "max_count", "n": cap}, f"more than the {cap} ordered items"))
            continue
        seen.add(key)
        taken += 1  # pending items keep their slot until the snapshot decides
        snap_fail = None
        for p in by_op["source"]:
            present, _ = get_field(item, p["key_field"])
            from bank import snapshots
            if not present or not snapshots.key_ok(p["template"], get_field(item, p["key_field"])[1]):
                snap_fail = _fail("SOURCE_UNAVAILABLE", p, f"{p['key_field']} is missing or not a key of the source")
                break
            got = snapshot(i, item, p) if snapshot else "pending"
            if got == "pending":
                res.update(status="pending", detail="waiting for the source snapshot")
                break
            if got == "unavailable":
                snap_fail = _fail("SOURCE_UNAVAILABLE", p, "the source has no such key")
                break
            for q in by_op["eq"]:
                if "snapshot_field" in q and (f := _field_pred(q, item, got.get("fields") or {})):
                    snap_fail = f
                    break
        if snap_fail:
            seen.discard(key)
            taken -= 1
            res.update(snap_fail)
        elif res["status"] != "pending":
            res.update(status="accepted")
    return out


def _check_item(item, by_op: dict, registry) -> dict | None:
    if not isinstance(item, dict):
        return _fail("SCHEMA_FAIL", None, "an item is a JSON object")
    personal = [k for k in PERSONAL_FIELDS if k in item]
    if personal:
        return _fail("SCHEMA_FAIL", None, f"personal data fields are not accepted: {', '.join(personal)}")
    if problem := receipts.ijson_problem(item):
        return _fail("SCHEMA_FAIL", None, problem)
    for p in by_op["json_schema"]:
        if problem := schema_problem(p["schema"], item):
            return _fail("SCHEMA_FAIL", p, problem)
    for op in ("required", "eq", "in", "range", "regex", "unique"):
        for p in by_op[op]:
            if op == "eq" and "snapshot_field" in p:
                if not get_field(item, p["field"])[0]:
                    return _fail("CRITERIA_FAIL", p, f"{p['field']} is missing")
                continue  # compared once the snapshot exists
            if op == "unique":
                if not get_field(item, p["field"])[0]:
                    return _fail("CRITERIA_FAIL", p, f"{p['field']} is missing")
                continue
            if f := _field_pred(p, item, None):
                return f
    for p in by_op["registry_active"]:
        if registry is None:
            raise Pending("registry lookup not available")
        if f := _registry_pred(p, item, registry):
            return f
    return None


# ---- the buyer agent's criteria as predicates ----

def criteria_predicates(c: dict) -> list[dict]:
    """agents/buyer.py AcceptanceCriteria -> predicates, deterministic code (the model only fills the criteria; the
    human confirms the resulting predicates, §2.1). Companies with IČO by default; registry_check=False is for data
    without IČO (Google Maps places), where registry facts cannot be checked (§6)."""
    words = [w for w in (re.sub(r"[^\w]", "", str(k)) for k in c.get("category_keywords") or []) if w]
    preds = ([{"op": "unique", "field": "ico"},
              {"op": "registry_active", "ico_field": "ico", "name_field": "name", "address_field": "address"}]
             if c.get("registry_check", True) else
             [{"op": "unique", "field": "placeId"}, {"op": "eq", "field": "permanentlyClosed", "value": False}])
    if c.get("postcode_prefix"):
        preds.append({"op": "regex", "field": "postalCode", "pattern": rf"^{c['postcode_prefix']}\d{{2}} ?\d{{2}}$"})
    else:
        preds.append({"op": "eq", "field": "city", "value": c["city"]})
    if c.get("min_rating_exclusive") is not None:
        preds.append({"op": "range", "field": "totalScore", "gt": float(c["min_rating_exclusive"])})
    if words:
        preds.append({"op": "regex", "field": "categoryName", "pattern": "(?i)(" + "|".join(words) + ")"})
    if c.get("require_website", True):
        preds.append({"op": "required", "field": "website"})
    preds.append({"op": "max_count", "n": int(c["count"])})
    return validate(preds)


# ---- human language (§2.1: the human confirms predicates as text; the deal page shows them) ----

def describe(p: dict) -> str:
    f, op = p.get("field"), p["op"]
    if op == "required":
        return f"{f} is filled in"
    if op == "eq":
        return f"{f} equals {p['value']!r}" if "value" in p else f"{f} equals {p['snapshot_field']} in the source"
    if op == "in":
        return f"{f} is one of {', '.join(map(str, p['values']))}"
    if op == "range":
        sym = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
        return f"{f} " + " and ".join(f"{sym[k]} {p[k]}" for k in sym if k in p)
    if op == "regex":
        return f"{f} matches /{p['pattern']}/"
    if op == "json_schema":
        return "the item has the agreed structure (JSON schema)"
    if op == "unique":
        return f"no {f} is paid twice (in this deal and in this task)"
    if op == "max_count":
        return f"at most {p['n']} items are paid"
    if op == "registry_active":
        extra = f" and seat ({p['address_field']})" if p.get("address_field") else ""
        return (f"{p['ico_field']} is an active company in ARES, not in insolvency, and its name "
                f"({p['name_field']}){extra} matches the ARES record")
    if op == "source":
        return f"the bank keeps a dated snapshot of {p['template']} for each item's {p['key_field']}"
    return op


