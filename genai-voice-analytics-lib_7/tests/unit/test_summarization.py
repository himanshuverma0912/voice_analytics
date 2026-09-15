"""Summarization behaviour, with the gateway stubbed out."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AuthenticationError, RateLimitError

from voice_analytics import summarize_audio, summarize_text
from voice_analytics.exceptions import (
    LLMAuthenticationError,
    LLMRateLimitError,
    SummarizationError,
)
from voice_analytics.summarization.formats import (
    SummaryFormat,
    select_formats,
    supported_formats,
)
from voice_analytics.summarization.service import _coerce_summary

MODEL = "gemini-2.5-flash-transcription"


def _reply(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _client(*contents: str) -> AsyncMock:
    """A client returning each content in turn, cycling on the last."""
    client = AsyncMock()
    replies = [_reply(c) for c in contents]

    async def _create(**_kwargs):
        return replies.pop(0) if len(replies) > 1 else replies[0]

    client.chat.completions.create = AsyncMock(side_effect=_create)
    return client


def _rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    return RateLimitError(
        "slow down", response=httpx.Response(429, request=request), body=None
    )


def _auth_error() -> AuthenticationError:
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    return AuthenticationError(
        "bad key", response=httpx.Response(401, request=request), body=None
    )


# ---------------------------------------------------------------------------
# Format selection
# ---------------------------------------------------------------------------


def test_default_is_short_summary():
    assert select_formats(None) == [SummaryFormat.SHORT_SUMMARY]
    assert select_formats([]) == [SummaryFormat.SHORT_SUMMARY]


def test_formats_are_returned_in_canonical_order_regardless_of_request():
    selected = select_formats(["short_summary", "bullet_points"])
    assert selected == [SummaryFormat.BULLET_POINTS, SummaryFormat.SHORT_SUMMARY]


def test_duplicate_formats_are_collapsed():
    assert select_formats(["key_insights", "key_insights"]) == [
        SummaryFormat.KEY_INSIGHTS
    ]


def test_enum_members_and_strings_are_both_accepted():
    assert select_formats([SummaryFormat.KEY_INSIGHTS]) == select_formats(
        ["key_insights"]
    )


def test_unsupported_format_is_named_rather_than_silently_dropped():
    with pytest.raises(ValueError, match="Unsupported summary format: 'haiku'"):
        select_formats(["haiku"])


def test_supported_formats_listing():
    assert supported_formats() == ["bullet_points", "key_insights", "short_summary"]


def test_prompt_task_key_matches_the_catalogue():
    assert SummaryFormat.BULLET_POINTS.prompt_task == "summarize_bullet_points"


# ---------------------------------------------------------------------------
# Coercion -- the model ignores "must be a string" often enough to matter
# ---------------------------------------------------------------------------


def test_coerce_leaves_a_string_alone():
    assert _coerce_summary("  a summary  ") == "a summary"


def test_coerce_turns_a_list_into_bullet_points():
    assert _coerce_summary(["first", "second"]) == "- first\n- second"


def test_coerce_drops_blank_list_items():
    assert _coerce_summary(["kept", "  ", ""]) == "- kept"


@pytest.mark.parametrize(("value", "expected"), [(None, ""), ([], ""), (42, "42")])
def test_coerce_handles_odd_values(value, expected):
    assert _coerce_summary(value) == expected


# ---------------------------------------------------------------------------
# summarize_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_call_per_format_issued_in_parallel():
    client = _client("A summary.")

    result = await summarize_text(
        client=client,
        text="Agent: hello. Customer: hi.",
        model_name=MODEL,
        formats=["bullet_points", "key_insights", "short_summary"],
    )

    assert client.chat.completions.create.await_count == 3
    assert set(result.summaries) == {"bullet_points", "key_insights", "short_summary"}
    assert not result.is_empty


@pytest.mark.asyncio
async def test_each_format_gets_its_own_prompt():
    client = _client("A summary.")

    await summarize_text(
        client=client, text="transcript", model_name=MODEL,
        formats=["bullet_points", "short_summary"],
    )

    prompts = [
        call.kwargs["messages"][0]["content"]
        for call in client.chat.completions.create.await_args_list
    ]
    assert len(prompts) == 2
    assert prompts[0] != prompts[1]          # focused, not one shared prompt
    assert all("banking analyst" in p for p in prompts)


@pytest.mark.asyncio
async def test_summaries_are_requested_as_prose_not_json():
    client = _client("A summary.")
    await summarize_text(client=client, text="t", model_name=MODEL)

    assert client.chat.completions.create.await_args.kwargs["response_format"] is None


@pytest.mark.asyncio
async def test_model_name_is_forwarded():
    """The originating service dropped model_name here, breaking /summarize-text."""
    client = _client("A summary.")
    await summarize_text(client=client, text="t", model_name=MODEL)

    assert client.chat.completions.create.await_args.kwargs["model"] == MODEL


@pytest.mark.asyncio
async def test_blank_summaries_are_omitted_rather_than_stored_empty():
    client = _client("   ")
    result = await summarize_text(client=client, text="t", model_name=MODEL)

    assert result.summaries == {}
    assert result.is_empty


@pytest.mark.asyncio
async def test_requires_model_name():
    with pytest.raises(ValueError, match="model_name is required"):
        await summarize_text(client=_client("x"), text="t", model_name="")


@pytest.mark.asyncio
async def test_requires_text():
    with pytest.raises(ValueError, match="text is required"):
        await summarize_text(client=_client("x"), text="   ", model_name=MODEL)


@pytest.mark.asyncio
async def test_unsupported_format_rejected_before_the_gateway_is_called():
    client = _client("x")
    with pytest.raises(ValueError, match="Unsupported summary format"):
        await summarize_text(
            client=client, text="t", model_name=MODEL, formats=["haiku"]
        )
    client.chat.completions.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# summarize_audio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_uses_a_single_combined_call():
    """Audio is expensive to upload, so all formats come from one request."""
    client = _client(
        '{"bullet_points": "- one\\n- two", "key_insights": "Insight.", '
        '"short_summary": "Summary."}'
    )

    result = await summarize_audio(
        client=client, content=b"audio", mime_type="audio/wav", model_name=MODEL,
        formats=["bullet_points", "key_insights", "short_summary"],
    )

    assert client.chat.completions.create.await_count == 1
    assert len(result.summaries) == 3


@pytest.mark.asyncio
async def test_audio_prompt_names_the_requested_formats():
    client = _client('{"short_summary": "Summary."}')
    await summarize_audio(
        client=client, content=b"a", mime_type="audio/wav", model_name=MODEL,
        formats=["short_summary"],
    )

    prompt = client.chat.completions.create.await_args.kwargs["messages"][0]["content"]
    assert "short_summary" in prompt


@pytest.mark.asyncio
async def test_audio_keeps_only_requested_formats():
    """A model that invents a key must not smuggle it into the result."""
    client = _client('{"short_summary": "Kept.", "sentiment": "Not requested."}')

    result = await summarize_audio(
        client=client, content=b"a", mime_type="audio/wav", model_name=MODEL,
        formats=["short_summary"],
    )

    assert set(result.summaries) == {"short_summary"}


@pytest.mark.asyncio
async def test_audio_converts_a_returned_list_into_bullets():
    client = _client('{"bullet_points": ["first", "second"]}')

    result = await summarize_audio(
        client=client, content=b"a", mime_type="audio/wav", model_name=MODEL,
        formats=["bullet_points"],
    )

    assert result.summaries["bullet_points"] == "- first\n- second"


@pytest.mark.asyncio
async def test_audio_requires_content():
    with pytest.raises(ValueError, match="content is required"):
        await summarize_audio(
            client=_client("{}"), content=b"", mime_type="audio/wav", model_name=MODEL
        )


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.summarization.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=_auth_error())

    with pytest.raises(LLMAuthenticationError):
        await summarize_text(client=client, text="t", model_name=MODEL)

    assert slept == []


@pytest.mark.asyncio
async def test_rate_limit_retries_with_exponential_backoff(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.summarization.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=_rate_limit_error())

    with pytest.raises(LLMRateLimitError):
        await summarize_text(client=client, text="t", model_name=MODEL)

    assert slept == [5, 10]


@pytest.mark.asyncio
async def test_unparseable_audio_response_raises_summarization_error():
    client = _client("I cannot help with that.")

    with pytest.raises(SummarizationError, match="Could not parse"):
        await summarize_audio(
            client=client, content=b"a", mime_type="audio/wav", model_name=MODEL
        )
