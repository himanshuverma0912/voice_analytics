"""Anonymization.

The behaviour that matters most here is what happens on failure: this module
must **never** return the original text as a fallback. Doing so would leak the
personal information it exists to remove.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from voice_analytics.anonymization import (
    anonymize,
    build_anonymization_client,
    extract_anonymized_text,
)
from voice_analytics.config import Settings
from voice_analytics.exceptions import AnonymizationError, ConfigurationError

RAW = "Hello, my name is Priya Sharma and my account is 50100123456789."
CLEAN = "Hello, my name is [NAME] and my account is [ACCOUNT]."

SETTINGS = Settings(
    _env_file=None,
    LLM_BASE_URL="https://llm.example/v1",
    ANONYMIZATION_URL="https://aux.example/anonymize",
)


def _response(status: int, body) -> httpx.Response:
    request = httpx.Request("POST", "https://aux.example/anonymize")
    if isinstance(body, (dict, list)):
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, text=body, request=request)


def _client(*responses) -> AsyncMock:
    """A client returning each response in turn, repeating the last."""
    client = AsyncMock()
    queue = list(responses)

    async def _post(*_args, **_kwargs):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    client.post = AsyncMock(side_effect=_post)
    return client


# ---------------------------------------------------------------------------
# Response parsing -- the service's shape is not stable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"messages": [{"role": "user", "content": CLEAN}]},
        {"anonymized_messages": [{"role": "user", "content": CLEAN}]},
        {"anonymizedMessages": [{"role": "user", "content": CLEAN}]},
        {"data": {"messages": [{"role": "user", "content": CLEAN}]}},
        {"a": {"b": {"messages": [{"role": "user", "content": CLEAN}]}}},
        [{"role": "user", "content": CLEAN}],
    ],
)
def test_extracts_the_user_message_from_every_known_shape(body):
    assert extract_anonymized_text(body) == CLEAN


def test_skips_non_user_roles():
    body = {"messages": [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": CLEAN},
    ]}
    assert extract_anonymized_text(body) == CLEAN


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"messages": []},
        {"messages": [{"role": "assistant", "content": "x"}]},
        {"messages": [{"role": "user", "content": "   "}]},
        {"messages": "not a list"},
        None,
        "a plain string",
    ],
)
def test_missing_anonymized_text_raises_rather_than_falling_back(body):
    """No usable text must be an error, never a pass-through of the original."""
    with pytest.raises(AnonymizationError, match="Refusing to continue"):
        extract_anonymized_text(body)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_the_anonymized_text():
    client = _client(_response(200, {"messages": [{"role": "user", "content": CLEAN}]}))

    result = await anonymize(client, SETTINGS, RAW)

    assert result == CLEAN
    assert "Priya Sharma" not in result
    assert "50100123456789" not in result


@pytest.mark.asyncio
async def test_sends_the_text_as_a_user_message():
    client = _client(_response(200, {"messages": [{"role": "user", "content": CLEAN}]}))

    await anonymize(client, SETTINGS, RAW)

    kwargs = client.post.await_args.kwargs
    assert kwargs["json"] == {"messages": [{"role": "user", "content": RAW}]}
    assert client.post.await_args.args[0] == SETTINGS.ANONYMIZATION_URL


@pytest.mark.asyncio
async def test_blank_text_is_rejected():
    with pytest.raises(ValueError, match="text is required"):
        await anonymize(_client(_response(200, {})), SETTINGS, "   ")


# ---------------------------------------------------------------------------
# Failure -- every path must raise, none may return the original
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_error_is_not_retried(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.anonymization.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    client = _client(
        httpx.HTTPStatusError("bad request", request=httpx.Request("POST", "https://x"),
                              response=_response(400, {}))
    )

    with pytest.raises(AnonymizationError, match="rejected the request"):
        await anonymize(client, SETTINGS, RAW)

    assert slept == []                       # a 400 will still be a 400


@pytest.mark.asyncio
async def test_server_error_retries_then_gives_up(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.anonymization.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    client = _client(
        httpx.HTTPStatusError("boom", request=httpx.Request("POST", "https://x"),
                              response=_response(503, {}))
    )

    with pytest.raises(AnonymizationError, match="after 3 attempts"):
        await anonymize(client, SETTINGS, RAW)

    assert slept == [5, 10]


@pytest.mark.asyncio
async def test_network_failure_retries_then_gives_up(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.anonymization.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    client = _client(httpx.ConnectError("unreachable"))

    with pytest.raises(AnonymizationError, match="unreachable"):
        await anonymize(client, SETTINGS, RAW)

    assert slept == [5, 10]


@pytest.mark.asyncio
async def test_recovers_after_a_transient_failure(monkeypatch):
    monkeypatch.setattr(
        "voice_analytics.anonymization.service.asyncio.sleep", AsyncMock()
    )
    client = _client(
        httpx.ConnectError("blip"),
        _response(200, {"messages": [{"role": "user", "content": CLEAN}]}),
    )

    assert await anonymize(client, SETTINGS, RAW) == CLEAN


@pytest.mark.asyncio
async def test_non_json_response_raises():
    client = _client(_response(200, "<html>gateway error</html>"))

    with pytest.raises(AnonymizationError, match="invalid response"):
        await anonymize(client, SETTINGS, RAW)


@pytest.mark.asyncio
async def test_no_failure_path_ever_returns_the_original_text(monkeypatch):
    """The property that matters: failure never leaks the input."""
    monkeypatch.setattr(
        "voice_analytics.anonymization.service.asyncio.sleep", AsyncMock()
    )
    failures = [
        httpx.ConnectError("unreachable"),
        httpx.HTTPStatusError("server", request=httpx.Request("POST", "https://x"),
                              response=_response(500, {})),
        _response(200, {}),                      # no anonymized text
        _response(200, "not json"),
    ]

    for failure in failures:
        with pytest.raises(AnonymizationError):
            result = await anonymize(_client(failure), SETTINGS, RAW)
            assert result != RAW, "leaked the original text"


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def test_client_requires_the_url_to_be_configured():
    settings = Settings(_env_file=None, LLM_BASE_URL="https://llm.example/v1")

    with pytest.raises(ConfigurationError, match="ANONYMIZATION_URL is not configured"):
        build_anonymization_client(settings)


def test_client_uses_the_configured_timeout():
    settings = Settings(
        _env_file=None,
        LLM_BASE_URL="https://llm.example/v1",
        ANONYMIZATION_URL="https://aux.example/anonymize",
        ANONYMIZATION_TIMEOUT_SECONDS=45.0,
    )

    client = build_anonymization_client(settings)
    assert client.timeout.read == 45.0
