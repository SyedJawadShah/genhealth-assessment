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

import anthropic
import httpx2
import pytest

from app.extraction import AnthropicExtractor, ExtractionError, Extractor
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
    return json.dumps({k: {"value": fields[k]["value"], "page": 1} for k in FIELDS})


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


def test_scorer_gives_full_marks_through_the_real_parsing_path() -> None:
    assert score(recorded_extractor(_expected_answer)) == (30, 30, [])


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
