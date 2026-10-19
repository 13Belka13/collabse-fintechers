# Czech bank account formats (SPEC §6.5): internal "predcisli-cislo/kodBanky" <-> IBAN
# "CZkk BBBB PPPPPP NNNNNNNNNN" (bank 4 digits, prefix 6, number 10, zero-padded). Compared in one format.
import re

_INTERNAL = re.compile(r"^(?:(\d{1,6})-)?(\d{1,10})/(\d{4})$")


def _iban_ok(iban: str) -> bool:
    s = iban[4:] + iban[:4]
    return int("".join(str(int(ch, 36)) for ch in s)) % 97 == 1


def normalize_account(s: str) -> str:
    """'CZ65 0800 0000 1920 0014 5399' / '000019-2000145399/0800' -> '19-2000145399/0800'.
    Leading zeros dropped, prefix omitted when 0. Raises ValueError on anything else (incl. bad IBAN checksum)."""
    t = re.sub(r"\s+", "", s or "").upper()
    if t.startswith("CZ"):
        if not re.fullmatch(r"CZ\d{22}", t) or not _iban_ok(t):
            raise ValueError(f"not a valid Czech IBAN: {s!r}")
        bank, pre, num = t[4:8], int(t[8:14]), int(t[14:24])
    else:
        m = _INTERNAL.match(t)
        if not m:
            raise ValueError(f"not a Czech account number: {s!r}")
        pre, num, bank = int(m.group(1) or 0), int(m.group(2)), m.group(3)
    if num == 0:
        raise ValueError(f"empty account number: {s!r}")
    return f"{str(pre) + '-' if pre else ''}{num}/{bank}"


def to_iban(account: str) -> str:
    """'19-2000145399/0800' -> 'CZ6508000000192000145399'."""
    pre_num, bank = normalize_account(account).split("/")
    pre, _, num = pre_num.rpartition("-")
    bban = f"{bank}{int(pre or 0):06d}{int(num):010d}"
    check = 98 - int("".join(str(int(ch, 36)) for ch in bban + "CZ00")) % 97
    return f"CZ{check:02d}{bban}"
