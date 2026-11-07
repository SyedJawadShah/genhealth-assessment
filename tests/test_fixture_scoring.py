"""Score extraction against the synthetic fixtures in tests/fixtures.

The offline tests run the real extractor and SDK parsing over a mock HTTP transport. The live
test calls the real model, allows no wrong fields, and is skipped unless RUN_LIVE_EXTRACTION=1
(and ANTHROPIC_API_KEY is set): `make score`.
"""

import base64
import hashlib
import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from app.extraction import AnthropicExtractor, ExtractedOrder, ExtractionError, Extractor
from app.main import _default_extractor
from app.orders import FIELDS, _check_extracted
from app.settings import Settings
from app.validation import FieldCheck

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED: dict[str, dict[str, Any]] = json.loads((FIXTURES / "expected.json").read_text())
TODAY = date(2026, 11, 6)


def _normalize(value: object) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if isinstance(value, date) else str(value)
    return " ".join(text.split()).casefold()


def _field_matches(want: dict[str, str | None], got: FieldCheck[Any], page: int | None) -> bool:
    if (_normalize(got.value), got.status) != (_normalize(want["value"]), want["status"]):
        return False
    return "page" not in want or str(page) == want["page"]


def _misses(name: str, fields: dict[str, Any], extracted: ExtractedOrder) -> list[str]:
    checks = _check_extracted(extracted, TODAY)
    misses = []
    for field in FIELDS:
        want, got, page = fields[field], checks[field], getattr(extracted, field).page
        if not _field_matches(want, got, page):
            misses.append(
                f"{name} {field}: expected {want['value']!r} ({want['status']}"
                f"{', page ' + want['page'] if 'page' in want else ''}), "
                f"got {got.value!r} ({got.status}, {got.reason}, page {page})"
            )
    return misses


def _packet_misses(
    name: str, alternatives: list[dict[str, Any]], extracted: ExtractedOrder
) -> list[str]:
    """Compare against the closest alternative; nulls are fine, the other patient's values not."""
    checks = _check_extracted(extracted, TODAY)
    hits = [{f for f in FIELDS if _field_matches(alt[f], checks[f], None)} for alt in alternatives]
    best = max(range(len(alternatives)), key=lambda i: len(hits[i]))
    misses = [
        m
        for m in _misses(name, alternatives[best], extracted)
        if checks[m.split()[1].rstrip(":")].status != "missing"
    ]
    others = set().union(*(h for i, h in enumerate(hits) if i != best)) - hits[best]
    if others:
        misses.append(f"{name}: fields mixed from both patients ({', '.join(sorted(others))})")
    return misses


def score(extractor: Extractor) -> tuple[int, int, list[str]]:
    """Run every fixture through extraction and validation; count fields matching exactly.

    A fixture with `any_of` (two patients in one packet) matches when every field agrees with
    one alternative, or is null; values taken from different patients count as misses."""
    matched, total, all_misses = 0, 0, []
    for name, fields in EXPECTED.items():
        extracted = extractor.extract((FIXTURES / name).read_bytes())
        if "any_of" in fields:
            misses = _packet_misses(name, fields["any_of"], extracted)
        else:
            misses = _misses(name, fields, extracted)
        total += len(FIELDS)
        matched += len(FIELDS) - min(len(misses), len(FIELDS))
        all_misses.extend(misses)
    return matched, total, all_misses


def _api_message(text: str, stop_reason: str = "end_turn") -> dict[str, object]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def recorded_extractor(answer: Callable[[bytes], str]) -> AnthropicExtractor:
    """The real extractor and SDK parsing, over a mock transport answering with `answer(pdf)`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        document = json.loads(request.content)["messages"][0]["content"][0]
        pdf = base64.b64decode(document["source"]["data"])
        return httpx2.Response(200, json=_api_message(answer(pdf)))

    client = anthropic.Anthropic(
        api_key="test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicExtractor(
        api_key="test",
        model="claude-opus-5-5",
        effort="medium",
        timeout_seconds=5,
        max_retries=0,
        client=client,
    )


def _expected_answer(pdf: bytes) -> str:
    """The JSON a perfect model would return for a fixture, found by the PDF's hash."""
    by_hash = {hashlib.sha256((FIXTURES / n).read_bytes()).digest(): n for n in EXPECTED}
    fields = EXPECTED[by_hash[hashlib.sha256(pdf).digest()]]
    fields = fields["any_of"][0] if "any_of" in fields else fields
    return json.dumps(
        {k: {"value": fields[k]["value"], "page": int(fields[k].get("page", 1))} for k in FIELDS}
    )


def test_fixtures_are_small_pdfs_with_complete_expectations() -> None:
    assert len(EXPECTED) == 10
    for name, fields in EXPECTED.items():
        data = (FIXTURES / name).read_bytes()
        assert data.startswith(b"%PDF-")
        assert len(data) < 500 * 1024
        for expectation in fields.get("any_of", [fields]):
            assert set(expectation) == set(FIELDS)
    assert EXPECTED["missing_dob.pdf"]["patient_date_of_birth"] == {
        "value": None,
        "status": "missing",
    }


def test_scorer_gives_full_marks_through_the_real_parsing_path() -> None:
    assert score(recorded_extractor(_expected_answer)) == (50, 50, [])


def test_scorer_counts_mixed_patients_as_misses() -> None:
    packet = EXPECTED["two_patient_packet.pdf"]["any_of"]
    mixed = {f: packet[0][f] if f.startswith("patient") else packet[1][f] for f in FIELDS}

    def answer(pdf: bytes) -> str:
        if pdf == (FIXTURES / "two_patient_packet.pdf").read_bytes():
            return json.dumps({k: {"value": mixed[k]["value"], "page": 1} for k in FIELDS})
        return _expected_answer(pdf)

    matched, total, misses = score(recorded_extractor(answer))

    assert matched < total
    assert any("mixed from both patients" in m for m in misses)


def test_scorer_checks_expected_pages() -> None:
    def answer(pdf: bytes) -> str:
        body = json.loads(_expected_answer(pdf))
        if pdf == (FIXTURES / "messy_five_page.pdf").read_bytes():
            body["provider_npi"]["page"] = 4
        return json.dumps(body)

    _, _, misses = score(recorded_extractor(answer))

    assert misses == [
        "messy_five_page.pdf provider_npi: expected "
        f"{EXPECTED['messy_five_page.pdf']['provider_npi']['value']!r} (valid, page 5), "
        f"got {EXPECTED['messy_five_page.pdf']['provider_npi']['value']!r} (valid, None, page 4)"
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"patient_first_name": {"value": "Ada"}}',
        '{"patient_first_name": "Ada", "patient_last_name": null}',
    ],
)
def test_malformed_raw_sdk_response_is_extraction_failed(raw: str) -> None:
    extractor = recorded_extractor(lambda _pdf: raw)

    with pytest.raises(ExtractionError) as info:
        extractor.extract((FIXTURES / "multipage_split.pdf").read_bytes())
    assert (info.value.code, info.value.status_code) == ("extraction_failed", 502)


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
    assert misses == []
