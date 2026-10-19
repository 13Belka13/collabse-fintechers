# Closed list of decision codes (SPEC §6.3) and the result shape every refusal uses.
# Refusals are results, not exceptions: {status, code, reasons, explanation, next_tool}.
from enum import StrEnum


class Code(StrEnum):
    MANDATE_NOT_ACTIVE = "MANDATE_NOT_ACTIVE"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    CATEGORY_NOT_IN_SCOPE = "CATEGORY_NOT_IN_SCOPE"
    COUNTERPARTY_NOT_IN_SCOPE = "COUNTERPARTY_NOT_IN_SCOPE"
    COUNTERPARTY_NOT_TRUSTED = "COUNTERPARTY_NOT_TRUSTED"
    AMOUNT_ABOVE_LIMIT = "AMOUNT_ABOVE_LIMIT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    QUOTE_MISMATCH = "QUOTE_MISMATCH"
    VELOCITY_EXCEEDED = "VELOCITY_EXCEEDED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    VAT_LIABILITY_RISK = "VAT_LIABILITY_RISK"
    UNDER_REVIEW = "UNDER_REVIEW"  # fail-closed: a registry did not answer in time (§19.4), or a hidden AML reason


# code -> (status, next_tool)
SPEC = {
    Code.MANDATE_NOT_ACTIVE: ("refused", "amend_mandate"),
    Code.MANDATE_EXPIRED: ("refused", "amend_mandate"),
    Code.CATEGORY_NOT_IN_SCOPE: ("refused", "ask_human"),
    Code.COUNTERPARTY_NOT_IN_SCOPE: ("refused", "ask_human"),
    Code.COUNTERPARTY_NOT_TRUSTED: ("refused", "ask_human"),  # public or not is decided per reason (§6.7)
    Code.AMOUNT_ABOVE_LIMIT: ("refused", "amend_mandate"),
    Code.BUDGET_EXCEEDED: ("refused", "amend_mandate"),
    Code.QUOTE_MISMATCH: ("refused", "ask_human"),
    Code.VELOCITY_EXCEEDED: ("refused", "retry_later"),
    Code.CONFIRMATION_REQUIRED: ("requires_user_action", "confirm"),
    Code.VAT_LIABILITY_RISK: ("requires_user_action", "confirm_or_pay_in_tczk"),
    Code.UNDER_REVIEW: ("refused", "retry_later"),
}

# Neutral client text for anything non-public (AML suspicion must not be disclosed, §6.7 / §38 AML Act).
# Registry timeouts use the same text, so the client cannot tell the two apart.
NEUTRAL_CLIENT_TEXT = "Platba je v bance na kontrole. / The payment is under review at the bank."


def reason(code: str, public: bool, text: str, source_url: str | None = None, **evidence) -> dict:
    """One sourced reason. `code` is a fine-grained reason (e.g. "vat_unreliable_payer"), not a Code."""
    return {"code": code, "public": public, "text": text, "source_url": source_url, "evidence": evidence}


def ok(**extra) -> dict:
    return {"status": "ok", "code": None, "reasons": [], "explanation": "", "next_tool": None, **extra}


def refuse(code: Code, reasons: list[dict] | None = None, explanation: str = "", **extra) -> dict:
    status, next_tool = SPEC[code]
    return {"status": status, "code": str(code), "reasons": reasons or [], "explanation": explanation,
            "next_tool": next_tool, **extra}


CLIENT_FIELDS = ("status", "code", "explanation", "next_tool", "deal_id", "marks")
CLIENT_REASON_FIELDS = ("code", "text", "source_url")


def client_view(decision: dict) -> dict:
    """What the client and its agent may see — an allowlist, nothing else passes. If any reason is
    non-public (or unflagged: fail closed), or the code is UNDER_REVIEW, the client gets only the neutral
    answer: showing the public half would hint that something else exists."""
    reasons = decision.get("reasons", [])
    if decision.get("code") == Code.UNDER_REVIEW or any(r.get("public") is not True for r in reasons):
        view = {"status": "refused", "code": str(Code.UNDER_REVIEW), "reasons": [],
                "explanation": NEUTRAL_CLIENT_TEXT, "next_tool": "retry_later"}
        if "deal_id" in decision:
            view["deal_id"] = decision["deal_id"]
        return view
    view = {k: decision[k] for k in CLIENT_FIELDS if k in decision}
    view["reasons"] = [{k: r.get(k) for k in CLIENT_REASON_FIELDS} for r in reasons]
    return view
