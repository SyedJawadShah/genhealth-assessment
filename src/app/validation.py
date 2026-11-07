"""Checks for extracted and user-entered order fields. Pure functions, no I/O.

Every check returns the normalized value when it passes and a reason when it does not. Values
are never corrected or guessed. An NPI that fails only its check digit is kept, flagged
`invalid_checksum`: real faxes and test documents carry such NPIs and a reviewer must see them.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

# needs_review: the value passed its checks but where it came from is doubtful.
FieldStatus = Literal["valid", "missing", "invalid", "invalid_checksum", "needs_review"]


@dataclass(frozen=True)
class FieldCheck[T]:
    status: FieldStatus
    value: T | None = None
    reason: str | None = None


def _missing[T]() -> FieldCheck[T]:
    return FieldCheck(status="missing", reason="not_provided")


def _invalid[T](reason: str) -> FieldCheck[T]:
    return FieldCheck(status="invalid", reason=reason)


def _clean(raw: str | None) -> str:
    return " ".join(raw.split()) if raw else ""


# Letters (any script), joined by single spaces, hyphens, apostrophes or periods.
_NAME = re.compile(r"[^\W\d_]+(?:(?:[ '\u2019.-]|\. )[^\W\d_]+)*\.?")
MAX_NAME_LENGTH = 100


def check_person_name(raw: str | None) -> FieldCheck[str]:
    name = _clean(raw)
    if not name:
        return _missing()
    if len(name) > MAX_NAME_LENGTH:
        return _invalid("too_long")
    if not _NAME.fullmatch(name):
        return _invalid("unexpected_characters")
    return FieldCheck(status="valid", value=name)


# Credentials stripped when they trail a name without a comma: exact, case-sensitive tokens
# (a trailing period is ignored), so surnames such as "Do", "Co" or "Pa" are never removed.
_TRAILING_CREDENTIALS = frozenset(
    {"MD", "M.D", "DO", "D.O", "NP", "N.P", "PA", "P.A", "PA-C", "RN", "R.N"}
    | {"DPM", "D.P.M", "DDS", "D.D.S"}
)
# After a comma ("Jane Smith, MD, FACP") a wider set is accepted, written in capitals and
# compared without dots or hyphens.
_COMMA_CREDENTIALS = frozenset(
    {
        *("MD", "DO", "DPM", "NP", "PA", "PAC", "FNP", "FNPC", "FNPBC", "APRN", "CNP", "CRNP"),
        *("DNP", "ANP", "RN", "BSN", "MSN", "DC", "OD", "DDS", "DMD", "PHD", "MBBS", "MBCHB"),
        *("PT", "DPT", "OT", "OTR", "FACP", "FACS", "FAAFP", "FACC", "MPH", "MS", "CPO", "CO"),
        "CP",
    }
)
_TITLE = re.compile(r"^(?:dr\.?|doctor)\s+", re.IGNORECASE)


def _is_trailing_credential(token: str) -> bool:
    return token.rstrip(".") in _TRAILING_CREDENTIALS


def _is_comma_credential(token: str) -> bool:
    return token == token.upper() and re.sub(r"[.\-]", "", token) in _COMMA_CREDENTIALS


def _is_credential(token: str) -> bool:
    """Either credential set: used to decide that a value holds no name at all."""
    return _is_trailing_credential(token) or _is_comma_credential(token)


def check_provider_name(raw: str | None) -> FieldCheck[str]:
    """Strip a leading title and trailing credentials, but never down to a single word."""
    name = _clean(raw)
    bare = _TITLE.sub("", name).split()
    if bare and all(_is_credential(t.strip(",")) for t in bare):
        return _missing()  # only a title and credentials, no name
    head, *tail = [part.strip() for part in name.split(",")]
    if (
        tail
        and len(head.split()) >= 2
        and all(_is_comma_credential(t) for p in tail for t in p.split())
    ):
        name = head
    tokens = name.split()
    while len(tokens) > 2 and _is_trailing_credential(tokens[-1]):
        tokens.pop()
    untitled = _TITLE.sub("", " ".join(tokens))
    if len(untitled.split()) >= 2:
        tokens = untitled.split()
    return check_person_name(" ".join(tokens))


def npi_check_digit_ok(npi: str) -> bool:
    """Luhn check over the NPI with the 80840 card-issuer prefix (CMS NPI standard)."""
    total = 0
    for position, char in enumerate(reversed("80840" + npi)):
        digit = int(char)
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def check_npi(raw: str | None) -> FieldCheck[str]:
    npi = re.sub(r"[\s-]", "", raw or "")
    if not npi:
        return _missing()
    if not (len(npi) == 10 and npi.isascii() and npi.isdigit()):
        return _invalid("not_10_digits")
    if not npi_check_digit_ok(npi):
        return FieldCheck(status="invalid_checksum", value=npi, reason="checksum_failed")
    return FieldCheck(status="valid", value=npi)


_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
_NUMERIC_US = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})")
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_MONTH_FIRST = re.compile(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
_DAY_FIRST = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?[\s-]+([a-z]+)\.?[\s,-]+(\d{4})")
MIN_BIRTH_YEAR = 1900


def _parse_date_parts(text: str) -> tuple[int, int, int] | str:
    """Return (year, month, day) or a reason the text is not a usable date."""
    if match := _ISO.fullmatch(text):
        return int(match[1]), int(match[2]), int(match[3])
    if match := _NUMERIC_US.fullmatch(text):
        if len(match[3]) == 2:
            return "ambiguous_two_digit_year"
        return int(match[3]), int(match[1]), int(match[2])
    for pattern, month_group, day_group in ((_MONTH_FIRST, 1, 2), (_DAY_FIRST, 2, 1)):
        if (match := pattern.fullmatch(text)) and match[month_group] in _MONTHS:
            return int(match[3]), _MONTHS[match[month_group]], int(match[day_group])
    return "unrecognized_format"


def check_date_of_birth(raw: str | None, today: date) -> FieldCheck[date]:
    """Parse a date of birth as written (US month-first when numeric) and check it."""
    text = _clean(raw).lower()
    if not text:
        return _missing()
    parts = _parse_date_parts(text)
    if isinstance(parts, str):
        return _invalid(parts)
    try:
        value = date(*parts)
    except ValueError:
        return _invalid("not_a_real_date")
    return check_birth_date_value(value, today)


def check_birth_date_value(value: date | None, today: date) -> FieldCheck[date]:
    if value is None:
        return _missing()
    if value > today:
        return _invalid("in_the_future")
    if value.year < MIN_BIRTH_YEAR:
        return _invalid("before_1900")
    return FieldCheck(status="valid", value=value)
