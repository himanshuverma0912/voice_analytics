"""Keyword extraction, and the two presets built on it.

The parsing helpers are pure functions taking an ExtractionResult, so the two
response shapes real models produce can be tested without a gateway.
"""

from __future__ import annotations

import pytest

from voice_analytics.exceptions import ExtractionError, LLMServiceError
from voice_analytics.extraction import (
    ExtractionResult,
    KeywordDefinition,
    KeywordMatch,
    agent_name_from,
    extract_keywords,
    topics_from,
)
from voice_analytics.extraction.service import _keywords_block, _parse_matches


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def test_keywords_block_matches_the_originating_format():
    block = _keywords_block([
        KeywordDefinition(keyword="topics", description="Find the topics."),
        KeywordDefinition(keyword="agent_name", description="Find the agent."),
    ])
    assert block == "- topics: Find the topics.\n- agent_name: Find the agent."


def test_keywords_block_supplies_a_default_description():
    block = _keywords_block([KeywordDefinition(keyword="amount")])
    assert block == "- amount: Extract the relevant value."


def test_the_ported_prompt_is_reachable_and_carries_the_keyword_list():
    from voice_analytics.prompts import default_prompt_manager

    prompt = default_prompt_manager().get(
        domain="banking", task="keyword_extraction",
        target_keywords="- topics: Find them.",
    )
    assert "- topics: Find them." in prompt
    assert '"matches"' in prompt


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def test_parse_matches_reads_a_well_formed_response():
    matches = _parse_matches({
        "matches": [
            {"keyword": "topics", "found": True, "count": 2,
             "extracted_values": ["credit limit", "forex card"],
             "context": ["...limit..."]},
        ]
    })
    assert len(matches) == 1
    assert matches[0].extracted_values == ["credit limit", "forex card"]


def test_parse_matches_survives_a_response_that_is_not_a_dict():
    assert _parse_matches(["not", "a", "dict"]) == []


def test_parse_matches_skips_malformed_entries_and_keeps_good_ones():
    matches = _parse_matches({
        "matches": ["junk", {"keyword": "agent_name", "found": True,
                             "extracted_values": ["Priya"]}],
    })
    assert [m.keyword for m in matches] == ["agent_name"]


def test_parse_matches_tolerates_wrong_types_inside_an_entry():
    """A model that returns a string where a list belongs must not crash."""
    matches = _parse_matches({
        "matches": [{"keyword": "topics", "found": True,
                     "extracted_values": "credit limit", "count": None}],
    })
    assert matches[0].extracted_values == []
    assert matches[0].count == 0


def test_parse_matches_caps_runaway_responses():
    matches = _parse_matches({
        "matches": [{"keyword": f"k{i}", "found": True} for i in range(500)]
    })
    assert len(matches) == 100


# ---------------------------------------------------------------------------
# Topics -- both shapes the model actually returns
# ---------------------------------------------------------------------------

def _result(*matches: KeywordMatch) -> ExtractionResult:
    return ExtractionResult(matches=list(matches))


def test_topics_canonical_shape():
    result = _result(KeywordMatch(
        keyword="topics", found=True,
        extracted_values=["Credit Limit", "Forex Card", "Imperia Program"],
    ))
    assert topics_from(result) == ["credit limit", "forex card", "imperia program"]


def test_topics_are_capped_at_three():
    result = _result(KeywordMatch(
        keyword="topics", found=True,
        extracted_values=["a", "b", "c", "d", "e"],
    ))
    assert topics_from(result) == ["a", "b", "c"]


def test_topics_deduplicate_case_insensitively_keeping_order():
    result = _result(KeywordMatch(
        keyword="topics", found=True,
        extracted_values=["Credit Limit", "credit limit", "Locker"],
    ))
    assert topics_from(result) == ["credit limit", "locker"]


def test_topics_fallback_shape_ranks_by_count():
    """Some responses split into one match per phrase instead of a list."""
    result = _result(
        KeywordMatch(keyword="locker", found=True, count=1),
        KeywordMatch(keyword="credit limit", found=True, count=9),
        KeywordMatch(keyword="forex card", found=True, count=4),
    )
    assert topics_from(result) == ["credit limit", "forex card", "locker"]


def test_topics_fallback_ignores_the_keyword_name_itself():
    result = _result(
        KeywordMatch(keyword="topics", found=True, count=99),
        KeywordMatch(keyword="locker", found=True, count=2),
    )
    assert topics_from(result) == ["locker"]


def test_a_single_canonical_value_falls_through_rather_than_being_trusted():
    """One value is usually the model echoing the keyword, not an answer."""
    result = _result(KeywordMatch(
        keyword="topics", found=True, count=3, extracted_values=["topics"],
    ))
    assert topics_from(result) is None


def test_topics_are_none_when_nothing_was_found():
    assert topics_from(_result(KeywordMatch(keyword="topics", found=False))) is None
    assert topics_from(_result()) is None


# ---------------------------------------------------------------------------
# Agent name
# ---------------------------------------------------------------------------

def test_agent_name_is_read_from_the_first_value():
    result = _result(KeywordMatch(
        keyword="agent_name", found=True, extracted_values=["Priya", "Nandi"],
    ))
    assert agent_name_from(result) == "Priya"


def test_agent_name_is_stripped():
    result = _result(KeywordMatch(
        keyword="agent_name", found=True, extracted_values=["  Devi Rashmi  "],
    ))
    assert agent_name_from(result) == "Devi Rashmi"


def test_agent_name_is_none_when_never_stated():
    assert agent_name_from(_result(
        KeywordMatch(keyword="agent_name", found=False))) is None


def test_agent_name_is_none_when_the_value_is_blank():
    """found=True with an empty string must not become an empty agent name."""
    assert agent_name_from(_result(
        KeywordMatch(keyword="agent_name", found=True,
                     extracted_values=["   "]))) is None


def test_agent_name_ignores_a_different_keyword():
    assert agent_name_from(_result(
        KeywordMatch(keyword="topics", found=True,
                     extracted_values=["Priya"]))) is None


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "keywords", "model"),
    [
        ("", [KeywordDefinition(keyword="a")], "m"),
        ("   ", [KeywordDefinition(keyword="a")], "m"),
        ("transcript", [], "m"),
        ("transcript", [KeywordDefinition(keyword="a")], ""),
    ],
)
async def test_extract_keywords_refuses_unusable_input(text, keywords, model):
    with pytest.raises(ValueError):
        await extract_keywords(client=None, text=text, keywords=keywords,
                               model_name=model)


# ---------------------------------------------------------------------------
# The gateway call: success, and every failure it is expected to translate
# ---------------------------------------------------------------------------

import json                                       # noqa: E402
from types import SimpleNamespace                 # noqa: E402
from unittest.mock import AsyncMock               # noqa: E402

import httpx                                      # noqa: E402
from openai import (                              # noqa: E402
    APIConnectionError,
    AuthenticationError,
    RateLimitError,
)

from voice_analytics.exceptions import (          # noqa: E402
    LLMAuthenticationError,
    LLMRateLimitError,
)

_KEYWORDS = [KeywordDefinition(keyword="topics", description="Find them.")]


def _reply(payload) -> SimpleNamespace:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _client(payload) -> AsyncMock:
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=_reply(payload))
    return client


def _client_raising(exc) -> AsyncMock:
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://llm.example/v1/chat/completions")


@pytest.mark.asyncio
async def test_extraction_returns_matches_and_a_percentage():
    client = _client({
        "matches": [{"keyword": "topics", "found": True,
                     "extracted_values": ["credit limit", "locker"]}]
    })
    result = await extract_keywords(
        client=client, text="a transcript", keywords=_KEYWORDS, model_name="m")

    assert result.fields_extracted_percentage == 100.0
    assert result.match("topics").extracted_values == ["credit limit", "locker"]
    assert result.processing_time_ms >= 0


@pytest.mark.asyncio
async def test_a_keyword_the_model_did_not_find_is_not_an_error():
    client = _client({"matches": [{"keyword": "topics", "found": False}]})
    result = await extract_keywords(
        client=client, text="a transcript", keywords=_KEYWORDS, model_name="m")

    assert result.fields_extracted_percentage == 0.0
    assert result.match("topics").found is False


@pytest.mark.asyncio
async def test_a_response_with_no_matches_key_yields_an_empty_result():
    client = _client({"something": "else"})
    result = await extract_keywords(
        client=client, text="a transcript", keywords=_KEYWORDS, model_name="m")

    assert result.matches == []
    assert result.fields_extracted_percentage == 0.0


@pytest.mark.asyncio
async def test_unparseable_output_becomes_an_extraction_error():
    """Prose where JSON belongs, past what json-repair can rescue."""
    client = _client("I could not do that, sorry.")
    with pytest.raises(ExtractionError):
        await extract_keywords(client=client, text="a transcript",
                               keywords=_KEYWORDS, model_name="m")


@pytest.mark.asyncio
async def test_a_rejected_credential_is_not_retried():
    client = _client_raising(AuthenticationError(
        "bad key", response=httpx.Response(401, request=_request()), body=None))
    with pytest.raises(LLMAuthenticationError):
        await extract_keywords(client=client, text="a transcript",
                               keywords=_KEYWORDS, model_name="m")
    assert client.chat.completions.create.await_count == 1


@pytest.mark.asyncio
async def test_rate_limiting_surfaces_rather_than_retrying():
    """Unlike scoring, extraction does not retry -- an optional field is not
    worth a backoff loop."""
    client = _client_raising(RateLimitError(
        "slow down", response=httpx.Response(429, request=_request()), body=None))
    with pytest.raises(LLMRateLimitError):
        await extract_keywords(client=client, text="a transcript",
                               keywords=_KEYWORDS, model_name="m")
    assert client.chat.completions.create.await_count == 1


@pytest.mark.asyncio
async def test_an_unreachable_gateway_becomes_an_llm_service_error():
    client = _client_raising(APIConnectionError(request=_request()))
    with pytest.raises(LLMServiceError):
        await extract_keywords(client=client, text="a transcript",
                               keywords=_KEYWORDS, model_name="m")


@pytest.mark.asyncio
async def test_presets_run_end_to_end_over_a_mocked_gateway():
    from voice_analytics.extraction import extract_agent_name, extract_topics

    client = _client({
        "matches": [
            {"keyword": "topics", "found": True,
             "extracted_values": ["Credit Limit", "Locker"]},
            {"keyword": "agent_name", "found": True,
             "extracted_values": ["Priya"]},
        ]
    })
    assert await extract_topics(client, "a transcript", "m") == ["credit limit", "locker"]
    assert await extract_agent_name(client, "a transcript", "m") == "Priya"
