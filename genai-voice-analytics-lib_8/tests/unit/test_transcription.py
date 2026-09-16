"""Transcription behaviour, with the gateway stubbed out.

No network, no database, no dotenv file. Settings are constructed inline, which
is only possible because the library never reads configuration at import time.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIConnectionError, AuthenticationError, RateLimitError

from voice_analytics import Settings, transcribe
from voice_analytics.exceptions import (
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMServiceError,
    TranscriptionError,
)
from voice_analytics.transcription.formatting import format_segments, parse_segments
from voice_analytics.transcription.languages import normalize_language

MODEL = "gemini-2.5-flash-transcription"


def _gateway_response(content: str):
    """Shape a fake gateway reply the way the OpenAI SDK returns one."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _client_returning(content: str) -> AsyncMock:
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        return_value=_gateway_response(content)
    )
    return client


def _client_raising(exc: Exception) -> AsyncMock:
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


def _http_response(status_code: int) -> httpx.Response:
    """A real httpx.Response, which the OpenAI SDK's errors require."""
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    return httpx.Response(status_code, request=request)


def _auth_error() -> AuthenticationError:
    return AuthenticationError(
        "invalid api key", response=_http_response(401), body=None
    )


def _rate_limit_error() -> RateLimitError:
    return RateLimitError("slow down", response=_http_response(429), body=None)


def _connection_error() -> APIConnectionError:
    return APIConnectionError(
        request=httpx.Request("POST", "https://llm.example/v1/chat/completions")
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_require_base_url():
    with pytest.raises(Exception):
        Settings(_env_file=None)


def test_settings_parse_transcription_model_allow_list():
    settings = Settings(
        _env_file=None,
        LLM_BASE_URL="https://llm.example/v1",
        TRANSCRIPTION_MODEL_NAMES=" a , b ,, c ",
    )
    assert settings.transcription_models() == {"a", "b", "c"}
    assert settings.supports_transcription("b")
    assert not settings.supports_transcription("z")


# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("hi", "Hindi"), ("HINDI", "Hindi"), (" english ", "English"), ("oriya", "Odia")],
)
def test_normalize_language_accepts_codes_and_names(value, expected):
    assert normalize_language(value) == expected


@pytest.mark.parametrize("value", [None, "", "   "])
def test_normalize_language_treats_blank_as_no_translation(value):
    assert normalize_language(value) is None


def test_normalize_language_rejects_unknown():
    with pytest.raises(ValueError, match="Unsupported language"):
        normalize_language("klingon")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_parse_segments_accepts_wrapped_and_bare_shapes():
    wrapped = parse_segments({"segments": [{"timestamp": "00:01", "text": "hi"}]})
    bare = parse_segments([{"timestamp": "00:01", "text": "hi"}])
    assert len(wrapped) == len(bare) == 1
    assert wrapped[0].text == bare[0].text == "hi"


def test_parse_segments_skips_non_objects():
    assert parse_segments({"segments": ["nonsense", None, {"text": "ok"}]})[0].text == "ok"


def test_parse_segments_handles_unexpected_shape():
    assert parse_segments("not json") == []
    assert parse_segments({"segments": None}) == []


def test_format_segments_renders_timestamped_lines():
    segments = parse_segments(
        {"segments": [{"timestamp": "00:12", "text": "[Agent]: Namaste"}]}
    )
    assert format_segments(segments) == "[00:12] [Agent]: Namaste"


def test_format_segments_reattaches_dropped_speaker_label():
    segments = parse_segments(
        {
            "segments": [
                {
                    "timestamp": "00:12",
                    "text": "[Agent]: Namaste",
                    "translated_text": "Greetings",  # model dropped the label
                }
            ]
        }
    )
    output = format_segments(
        segments,
        key="translated_text",
        fallback="text",
        preserve_speaker_from="text",
    )
    assert output == "[00:12] [Agent]: Greetings"


def test_format_segments_falls_back_when_translation_missing():
    segments = parse_segments({"segments": [{"timestamp": "00:05", "text": "original"}]})
    output = format_segments(segments, key="translated_text", fallback="text")
    assert output == "[00:05] original"


def test_format_segments_does_not_escape_html():
    """A library returns data; the rendering boundary decides encoding."""
    segments = parse_segments({"segments": [{"timestamp": "00:01", "text": "Tata & Sons"}]})
    assert format_segments(segments) == "[00:01] Tata & Sons"


# ---------------------------------------------------------------------------
# transcribe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_returns_structured_result():
    client = _client_returning(
        '{"primary_language": "Hindi", "segments": '
        '[{"timestamp": "00:01", "text": "Namaste"}]}'
    )

    result = await transcribe(
        client=client, content=b"audio", mime_type="audio/wav", model_name=MODEL
    )

    assert result.primary_language == "Hindi"
    assert result.transcript == "[00:01] Namaste"
    assert result.translated_transcript is None
    assert result.processing_time_ms >= 0
    assert not result.is_empty


@pytest.mark.asyncio
async def test_transcribe_populates_translation_when_requested():
    client = _client_returning(
        '{"segments": [{"timestamp": "00:01", "text": "Namaste", '
        '"translated_text": "Greetings"}]}'
    )

    result = await transcribe(
        client=client,
        content=b"audio",
        mime_type="audio/wav",
        model_name=MODEL,
        target_lang="en",
    )

    assert result.translated_transcript == "[00:01] Greetings"


@pytest.mark.asyncio
async def test_transcribe_recovers_json_from_markdown_fence():
    client = _client_returning(
        'Here you go:\n```json\n{"segments": [{"timestamp": "00:01", "text": "ok"}]}\n```'
    )
    result = await transcribe(
        client=client, content=b"audio", mime_type="audio/wav", model_name=MODEL
    )
    assert result.transcript == "[00:01] ok"


@pytest.mark.asyncio
async def test_transcribe_rejects_unsupported_language_before_calling_gateway():
    client = _client_returning("{}")
    with pytest.raises(ValueError, match="Unsupported language"):
        await transcribe(
            client=client,
            content=b"audio",
            mime_type="audio/wav",
            model_name=MODEL,
            target_lang="klingon",
        )
    client.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_transcribe_requires_model_name():
    with pytest.raises(ValueError, match="model_name is required"):
        await transcribe(
            client=_client_returning("{}"),
            content=b"audio",
            mime_type="audio/wav",
            model_name="",
        )


@pytest.mark.asyncio
async def test_transcribe_requires_content():
    with pytest.raises(ValueError, match="content is required"):
        await transcribe(
            client=_client_returning("{}"),
            content=b"",
            mime_type="audio/wav",
            model_name=MODEL,
        )


@pytest.mark.asyncio
async def test_transcribe_raises_on_unparseable_response():
    client = _client_returning("I am afraid I cannot do that.")
    with pytest.raises(TranscriptionError, match="Could not parse"):
        await transcribe(
            client=client, content=b"audio", mime_type="audio/wav", model_name=MODEL
        )


@pytest.mark.asyncio
async def test_transcribe_does_not_retry_authentication_failures(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.transcription.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    client = _client_raising(_auth_error())

    with pytest.raises(LLMAuthenticationError):
        await transcribe(
            client=client, content=b"audio", mime_type="audio/wav", model_name=MODEL
        )

    assert client.chat.completions.create.await_count == 1
    assert slept == []


@pytest.mark.asyncio
async def test_transcribe_retries_rate_limits_with_exponential_backoff(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.transcription.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    client = _client_raising(_rate_limit_error())

    with pytest.raises(LLMRateLimitError):
        await transcribe(
            client=client, content=b"audio", mime_type="audio/wav", model_name=MODEL
        )

    assert client.chat.completions.create.await_count == 3  # 1 + 2 retries
    assert slept == [5, 10]


@pytest.mark.asyncio
async def test_transcribe_succeeds_after_a_transient_rate_limit(monkeypatch):
    monkeypatch.setattr(
        "voice_analytics.transcription.service.asyncio.sleep", AsyncMock()
    )
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[
            _rate_limit_error(),
            _gateway_response('{"segments": [{"timestamp": "00:01", "text": "ok"}]}'),
        ]
    )

    result = await transcribe(
        client=client, content=b"audio", mime_type="audio/wav", model_name=MODEL
    )

    assert result.transcript == "[00:01] ok"
    assert client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_transcribe_retries_connection_failures_then_gives_up(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.transcription.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    client = _client_raising(_connection_error())

    with pytest.raises(LLMServiceError):
        await transcribe(
            client=client, content=b"audio", mime_type="audio/wav", model_name=MODEL
        )

    assert client.chat.completions.create.await_count == 3
    assert slept == [5, 10]


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------


def test_duration_is_the_latest_timestamp():
    from voice_analytics.transcription import extract_duration_sec

    assert extract_duration_sec("[00:02] hi\n[01:30] bye") == 90


def test_duration_handles_timestamp_ranges():
    from voice_analytics.transcription import extract_duration_sec

    assert extract_duration_sec("[00:02-00:10] hi\n[01:00-01:45] bye") == 105


@pytest.mark.parametrize("value", [None, "", "no timestamps here"])
def test_duration_is_none_when_unknowable(value):
    """None rather than a misleading zero."""
    from voice_analytics.transcription import extract_duration_sec

    assert extract_duration_sec(value) is None


def test_duration_works_on_our_own_formatter_output():
    from voice_analytics.transcription import extract_duration_sec

    segments = parse_segments({"segments": [
        {"timestamp": "00:05", "text": "a"}, {"timestamp": "02:15", "text": "b"},
    ]})
    assert extract_duration_sec(format_segments(segments)) == 135


# ---------------------------------------------------------------------------
# Romanisation and cost attribution -- both carried over from the old service
# ---------------------------------------------------------------------------


def test_romanized_text_is_preferred_when_romanisation_was_asked_for():
    """Some responses put Latin script in its own field instead of `text`."""
    segments = parse_segments({"segments": [
        {"timestamp": "00:01", "text": "नमस्ते", "romanized_text": "Namaste"},
    ]})

    assert format_segments(segments, prefer_romanized=True) == "[00:01] Namaste"
    assert format_segments(segments, prefer_romanized=False) == "[00:01] नमस्ते"


def test_romanized_preference_falls_back_when_the_field_is_absent():
    segments = parse_segments({"segments": [{"timestamp": "00:01", "text": "Namaste"}]})
    assert format_segments(segments, prefer_romanized=True) == "[00:01] Namaste"


@pytest.mark.asyncio
async def test_romanize_flag_reaches_the_formatter():
    client = _client_returning(
        '{"segments": [{"timestamp": "00:01", "text": "नमस्ते", '
        '"romanized_text": "Namaste"}]}'
    )
    result = await transcribe(
        client=client, content=b"a", mime_type="audio/wav",
        model_name=MODEL, romanize=True,
    )
    assert result.transcript == "[00:01] Namaste"


@pytest.mark.asyncio
async def test_cost_attribution_tags_match_the_originating_service():
    """LiteLLM dashboards group by these; renaming either would orphan history."""
    from voice_analytics.llm import BATCH_METADATA, DEFAULT_METADATA

    assert DEFAULT_METADATA == {"service": "voice-transcription-service", "mode": "api"}
    assert BATCH_METADATA == {"service": "voice-transcription-service", "mode": "batch"}

    client = _client_returning('{"segments": [{"timestamp": "00:01", "text": "x"}]}')
    await transcribe(client=client, content=b"a", mime_type="audio/wav", model_name=MODEL)

    sent = client.chat.completions.create.await_args.kwargs["metadata"]
    assert sent["service"] == "voice-transcription-service"
