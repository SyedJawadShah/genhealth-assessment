"""Score extraction against the synthetic fixtures in tests/fixtures.

The mocked tests check the fixtures and the scorer. The live test calls the real model and is
skipped unless RUN_LIVE_EXTRACTION=1 (and ANTHROPIC_API_KEY is set): `make score`.
"""

import json
import os
from datetime import date
from pathlib import Path

import pytest

from app.extraction import ExtractedField, ExtractedOrder, Extractor
from app.main import _default_extractor
from app.orders import FIELDS, _check_extracted
from app.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED: dict[str, dict[str, dict[str, str | None]]] = json.loads(
    (FIXTURES / "expected.json").read_text()
)
TODAY = date(2026, 11, 6)


def _normalize(value: object) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if isinstance(value, date) else str(value)
    return " ".join(text.split()).casefold()


def score(extractor: Extractor) -> tuple[int, int, list[str]]:
    """Run every fixture through extraction and validation; count fields matching exactly."""
    matched, total, misses = 0, 0, []
    for name, fields in EXPECTED.items():
        checks = _check_extracted(extractor.extract((FIXTURES / name).read_bytes()), TODAY)
        for field in FIELDS:
            want, got = fields[field], checks[field]
            total += 1
            if (_normalize(got.value), got.status) == (_normalize(want["value"]), want["status"]):
                matched += 1
            else:
                misses.append(
                    f"{name} {field}: expected {want['value']!r} ({want['status']}), "
                    f"got {got.value!r} ({got.status}, {got.reason})"
                )
    return matched, total, misses


class ExpectedAnswers:
    """Answers each fixture with its expected values, as a perfect extractor would."""

    def __init__(self) -> None:
        self._by_size = {(FIXTURES / n).stat().st_size: n for n in EXPECTED}

    def extract(self, pdf: bytes) -> ExtractedOrder:
        fields = EXPECTED[self._by_size[len(pdf)]]
        return ExtractedOrder(
            **{k: ExtractedField(value=fields[k]["value"], page=1) for k in FIELDS}
        )


def test_fixtures_are_small_pdfs_with_complete_expectations() -> None:
    assert len(EXPECTED) == 6
    for name, fields in EXPECTED.items():
        data = (FIXTURES / name).read_bytes()
        assert data.startswith(b"%PDF-")
        assert len(data) < 500 * 1024
        assert set(fields) == set(FIELDS)
    assert EXPECTED["missing_dob.pdf"]["patient_date_of_birth"] == {
        "value": None,
        "status": "missing",
    }


def test_scorer_gives_full_marks_for_expected_answers() -> None:
    assert score(ExpectedAnswers()) == (30, 30, [])


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_EXTRACTION") != "1", reason="set RUN_LIVE_EXTRACTION=1 to call the API"
)
def test_live_extraction_score() -> None:
    settings = Settings()
    if settings.anthropic_api_key is None:
        pytest.skip("ANTHROPIC_API_KEY is not set")
    matched, total, misses = score(_default_extractor(settings))

    print(f"\nLive extraction score: {matched}/{total} fields")
    for miss in misses:
        print(f"  MISS {miss}")
    assert matched / total >= 0.8
