from datetime import date

import pytest

from app.validation import (
    check_birth_date_value,
    check_date_of_birth,
    check_npi,
    check_person_name,
    check_provider_name,
    npi_check_digit_ok,
)

TODAY = date(2026, 11, 6)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Marie", "Marie"),
        ("  Mary   Jane ", "Mary Jane"),
        ("O'Brien", "O'Brien"),
        ("Smith-Jones", "Smith-Jones"),
        ("St. John", "St. John"),
        ("José", "José"),
    ],
)
def test_person_name_valid(raw: str, expected: str) -> None:
    check = check_person_name(raw)
    assert (check.status, check.value) == ("valid", expected)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_person_name_missing(raw: str | None) -> None:
    assert check_person_name(raw).status == "missing"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("Marie1", "unexpected_characters"),
        ("<script>", "unexpected_characters"),
        ("A" * 101, "too_long"),
    ],
)
def test_person_name_invalid(raw: str, reason: str) -> None:
    check = check_person_name(raw)
    assert (check.status, check.value, check.reason) == ("invalid", None, reason)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Arjun Raj D.P.M", "Arjun Raj"),
        ("Gregory House MD", "Gregory House"),
        ("Jane Smith, M.D., FACP", "Jane Smith"),
        ("Dr. Jane Smith", "Jane Smith"),
        ("Pat Lee PA-C", "Pat Lee"),
        ("Jane Smith,", "Jane Smith"),
    ],
)
def test_provider_name_strips_title_and_credentials(raw: str, expected: str) -> None:
    check = check_provider_name(raw)
    assert (check.status, check.value) == ("valid", expected)


@pytest.mark.parametrize(
    "raw", [None, "", "MD", "Dr. MD", "Dr. MD, FACP", "MD, FACP", "M.D., F.A.C.P.", "DO FACC"]
)
def test_provider_name_missing(raw: str | None) -> None:
    assert check_provider_name(raw).status == "missing"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jane Do", "Jane Do"),
        ("Alex Co", "Alex Co"),
        ("Mary Pa", "Mary Pa"),
        ("Jane Do MD", "Jane Do"),
        ("JANE DO", "JANE DO"),
        ("Raj DPM", "Raj DPM"),
        ("Dr. Smith", "Dr. Smith"),
        ("Jane Do, DO", "Jane Do"),
        ("Ann Lee Phd", "Ann Lee Phd"),
    ],
)
def test_provider_name_never_strips_real_surnames(raw: str, expected: str) -> None:
    check = check_provider_name(raw)
    assert (check.status, check.value) == ("valid", expected)


def test_provider_name_keeps_unknown_suffix_and_validates_it() -> None:
    assert check_provider_name("Smith Clinic #2").status == "invalid"


@pytest.mark.parametrize("raw", ["1234567893", "123-456-7893", " 1234567893 ", "1245319599"])
def test_npi_valid(raw: str) -> None:
    check = check_npi(raw)
    assert check.status == "valid"
    assert check.value == raw.replace("-", "").strip()


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("123456789", "not_10_digits"),
        ("12345678931", "not_10_digits"),
        ("12345A7893", "not_10_digits"),
        ("１２３４５６７８９３", "not_10_digits"),  # full-width digits  # noqa: RUF001
    ],
)
def test_npi_invalid(raw: str, reason: str) -> None:
    check = check_npi(raw)
    assert (check.status, check.value, check.reason) == ("invalid", None, reason)


@pytest.mark.parametrize("raw", ["9182734556", "1234567890", "918-273-4556"])
def test_npi_failing_checksum_is_kept_and_flagged(raw: str) -> None:
    # The sample fax's NPI (9182734556) fails the check digit; keep it, never correct it.
    check = check_npi(raw)
    assert (check.status, check.reason) == ("invalid_checksum", "checksum_failed")
    assert check.value == raw.replace("-", "")


def test_npi_missing() -> None:
    assert check_npi(None).status == "missing"
    assert check_npi(" - ").status == "missing"


def test_npi_check_digit_uses_80840_prefix() -> None:
    assert npi_check_digit_ok("1234567893")
    assert not npi_check_digit_ok("1234567894")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12/05/1900", date(1900, 12, 5)),
        ("1/2/1980", date(1980, 1, 2)),
        ("01-02-1980", date(1980, 1, 2)),
        ("01.02.1980", date(1980, 1, 2)),
        ("1980-01-02", date(1980, 1, 2)),
        ("January 2, 1980", date(1980, 1, 2)),
        ("Jan. 2nd 1980", date(1980, 1, 2)),
        ("2 Jan 1980", date(1980, 1, 2)),
        ("02-JAN-1980", date(1980, 1, 2)),
        ("Sept 30, 2000", date(2000, 9, 30)),
    ],
)
def test_date_of_birth_formats(raw: str, expected: date) -> None:
    check = check_date_of_birth(raw, TODAY)
    assert (check.status, check.value) == ("valid", expected)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("2/30/1990", "not_a_real_date"),
        ("13/01/1990", "not_a_real_date"),
        ("2/3/90", "ambiguous_two_digit_year"),
        ("12/05/2030", "in_the_future"),
        ("12/05/1899", "before_1900"),
        ("yesterday", "unrecognized_format"),
        ("Smarch 3, 1990", "unrecognized_format"),
        ("124", "unrecognized_format"),
    ],
)
def test_date_of_birth_invalid(raw: str, reason: str) -> None:
    check = check_date_of_birth(raw, TODAY)
    assert (check.status, check.value, check.reason) == ("invalid", None, reason)


def test_date_of_birth_missing() -> None:
    assert check_date_of_birth(None, TODAY).status == "missing"
    assert check_birth_date_value(None, TODAY).status == "missing"


def test_birth_date_today_is_allowed() -> None:
    assert check_birth_date_value(TODAY, TODAY).status == "valid"
