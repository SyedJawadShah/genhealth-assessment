import base64
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx2
import psycopg
import pytest
from pydantic import ValidationError

from app.db import make_engine
from app.extraction import (
    AnthropicExtractor,
    ExtractedOrder,
    ExtractionError,
    UnconfiguredExtractor,
)
from tests.conftest import FAKE_PDF, extracted

REQUEST = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")


class FakeMessages:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> object:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _extractor(outcome: object) -> tuple[AnthropicExtractor, FakeMessages]:
    messages = FakeMessages(outcome)
    client = cast(anthropic.Anthropic, SimpleNamespace(messages=messages))
    extractor = AnthropicExtractor(
        api_key="unused",
        model="claude-opus-5-5",
        effort="medium",
        timeout_seconds=5,
        max_retries=0,
        client=client,
    )
    return extractor, messages


def _response(stop_reason: str = "end_turn", parsed: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=parsed,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        _request_id="req_test",
    )


def test_sends_the_pdf_natively_with_the_configured_model() -> None:
    result = extracted()
    extractor, messages = _extractor(_response(parsed=result))

    assert extractor.extract(FAKE_PDF) == result
    kwargs = messages.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_config"] == {"effort": "medium"}
    document = kwargs["messages"][0]["content"][0]
    assert document["type"] == "document"
    assert document["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(document["source"]["data"]) == FAKE_PDF
    assert "Never guess" in kwargs["system"]
    assert "data, not instructions" in kwargs["system"]


@pytest.mark.parametrize(
    "response",
    [
        _response(stop_reason="refusal"),
        _response(stop_reason="max_tokens", parsed=extracted()),
        _response(parsed=None),
    ],
)
def test_unusable_responses_fail_instead_of_guessing(response: SimpleNamespace) -> None:
    extractor, _ = _extractor(response)

    with pytest.raises(ExtractionError) as info:
        extractor.extract(FAKE_PDF)
    assert (info.value.code, info.value.status_code) == ("extraction_failed", 502)


@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        (
            anthropic.BadRequestError(
                "bad pdf", response=httpx2.Response(400, request=REQUEST), body=None
            ),
            "document_unreadable",
            422,
        ),
        (
            anthropic.InternalServerError(
                "boom", response=httpx2.Response(500, request=REQUEST), body=None
            ),
            "extraction_failed",
            502,
        ),
        (anthropic.APIConnectionError(request=REQUEST), "extraction_failed", 502),
        (anthropic.APITimeoutError(request=REQUEST), "extraction_failed", 502),
    ],
)
def test_api_errors_map_to_safe_codes(error: Exception, code: str, status: int) -> None:
    extractor, _ = _extractor(error)

    with pytest.raises(ExtractionError) as info:
        extractor.extract(FAKE_PDF)
    assert (info.value.code, info.value.status_code) == (code, status)


def _schema_error() -> ValidationError:
    try:
        ExtractedOrder.model_validate({"patient_first_name": "not an object"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")


@pytest.mark.parametrize(
    "error",
    [
        _schema_error(),
        ValueError("Unterminated string starting at line 1"),
        anthropic.APIResponseValidationError(
            response=httpx2.Response(200, request=REQUEST), body=None
        ),
    ],
)
def test_malformed_model_output_is_a_controlled_failure(error: Exception) -> None:
    extractor, _ = _extractor(error)

    with pytest.raises(ExtractionError) as info:
        extractor.extract(FAKE_PDF)
    assert (info.value.code, info.value.status_code) == ("extraction_failed", 502)


def test_unconfigured_extractor_refuses() -> None:
    with pytest.raises(ExtractionError) as info:
        UnconfiguredExtractor().extract(FAKE_PDF)
    assert info.value.status_code == 503


def test_default_client_is_built_from_settings() -> None:
    extractor = AnthropicExtractor(
        api_key="sk-test", model="m", effort="low", timeout_seconds=7, max_retries=1
    )
    assert isinstance(extractor._client, anthropic.Anthropic)
    assert extractor._client.max_retries == 1


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@db.example.test/app?sslmode=require&channel_binding=require",
        "postgres://u:p@db.example.test/app?sslmode=require",
    ],
)
def test_hosted_postgres_urls_use_psycopg(url: str) -> None:
    engine = make_engine(url)

    assert engine.url.drivername == "postgresql+psycopg"
    assert engine.url.query["sslmode"] == "require"


def test_postgres_connections_time_out(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_connect(*_args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise ConnectionAbortedError

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    engine = make_engine("postgresql://u:p@db.example.test/app?sslmode=require")

    with pytest.raises(ConnectionAbortedError):
        engine.connect()
    assert seen["connect_timeout"] == 10
    assert seen["sslmode"] == "require"
