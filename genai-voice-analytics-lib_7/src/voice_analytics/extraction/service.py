"""Pulling named values out of a transcript.

One generic call -- "here are the things to look for, find them" -- with the
two uses the voice analytics flow actually needs built on top of it, in
:mod:`voice_analytics.extraction.presets`.

Each call costs a gateway request, so these run **alongside** scoring rather
than inside it. The originating service made both extraction calls per call, in
addition to the scoring call: three requests per transcript, not one.
"""

from __future__ import annotations

import logging
import time

from openai import (
    APIConnectionError,
    APIError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from voice_analytics.exceptions import (
    ExtractionError,
    LLMAuthenticationError,
    LLMOutputParsingError,
    LLMRateLimitError,
    LLMServiceError,
)
from voice_analytics.extraction.models import (
    ExtractionResult,
    KeywordDefinition,
    KeywordMatch,
)
from voice_analytics.llm.base import process_text_request
from voice_analytics.observability import human_duration, say
from voice_analytics.prompts import default_prompt_manager

logger = logging.getLogger(__name__)

MAX_ITEMS = 100
"""Cap on matches, and on values within a match. Carried over from the
originating service: a model that starts repeating itself must not be able to
turn one call into an unbounded response."""


def _keywords_block(keywords: list[KeywordDefinition]) -> str:
    """Render the keyword list exactly as the originating service did."""
    return "\n".join(
        f"- {kw.keyword}: {kw.description or 'Extract the relevant value.'}"
        for kw in keywords
    )


def _parse_matches(raw: object) -> list[KeywordMatch]:
    """Read the ``matches`` array, ignoring anything malformed inside it."""
    block = raw.get("matches") if isinstance(raw, dict) else None
    if not isinstance(block, list):
        return []

    matches: list[KeywordMatch] = []
    for item in block[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        values = item.get("extracted_values")
        context = item.get("context")
        matches.append(
            KeywordMatch(
                keyword=str(item.get("keyword", "")),
                found=bool(item.get("found", False)),
                count=int(item.get("count", 0) or 0),
                extracted_values=[
                    str(v) for v in (values if isinstance(values, list) else [])[:MAX_ITEMS]
                ],
                context=[
                    str(v) for v in (context if isinstance(context, list) else [])[:MAX_ITEMS]
                ],
            )
        )
    return matches


async def extract_keywords(
    client: AsyncOpenAI,
    text: str,
    keywords: list[KeywordDefinition],
    model_name: str,
    user_instruction: str | None = None,
    llm_metadata: dict[str, str] | None = None,
) -> ExtractionResult:
    """Find each keyword's value in ``text``.

    Args:
        client: Gateway client from :func:`voice_analytics.llm.build_llm_client`.
        text: The transcript to read.
        keywords: What to look for. Each carries its own instructions.
        model_name: Model to use.
        user_instruction: Extra instruction that takes priority over the
            system prompt.
        llm_metadata: Tags for usage attribution at the gateway.

    Returns:
        An :class:`ExtractionResult`. A keyword the model could not find comes
        back with ``found=False`` rather than being missing.

    Raises:
        ValueError: ``text``, ``keywords`` or ``model_name`` is empty.
        LLMAuthenticationError: The gateway rejected the credential.
        LLMRateLimitError: The gateway is rate limiting.
        LLMServiceError: The gateway is unreachable or failing.
        ExtractionError: The response could not be parsed.

    Note:
        Unlike scoring, this does **not** retry. The originating service did
        not either, and both callers treat a failure as "leave the field
        empty" rather than as a reason to fail the call -- so a retry loop
        would spend money to populate an optional field.
    """
    started = time.perf_counter()

    if not model_name:
        raise ValueError("model_name is required")
    if not text or not text.strip():
        raise ValueError("text is required")
    if not keywords:
        raise ValueError("at least one keyword is required")

    system_prompt = default_prompt_manager().get(
        domain="banking",
        task="keyword_extraction",
        target_keywords=_keywords_block(keywords),
    )

    say(logger,
        "Looking for %d thing(s) in %d characters of transcript: %s.",
        len(keywords), len(text), ", ".join(kw.keyword for kw in keywords),
        model=model_name, keywords=[kw.keyword for kw in keywords],
        transcript_chars=len(text),
    )

    try:
        raw = await process_text_request(
            client=client,
            system_prompt=system_prompt,
            text_content=text,
            model_name=model_name,
            is_json=True,
            user_instruction=user_instruction,
            llm_metadata=llm_metadata,
        )
    except AuthenticationError as exc:
        say(logger,
            "The AI service rejected our credentials while extracting values.",
            level=logging.ERROR, error_type=type(exc).__name__, exit_code=5)
        raise LLMAuthenticationError("Invalid API key or gateway credential") from exc
    except RateLimitError as exc:
        say(logger, "The AI service is too busy to extract values right now.",
            level=logging.WARNING, error_type=type(exc).__name__, exit_code=6)
        raise LLMRateLimitError("Gateway rate limit exceeded") from exc
    except (APIError, APIConnectionError) as exc:
        say(logger, "The AI service did not respond properly (%s).",
            type(exc).__name__, level=logging.ERROR,
            error=str(exc)[:300], exit_code=7)
        raise LLMServiceError(f"Gateway failed during extraction: {exc}") from exc
    except LLMOutputParsingError as exc:
        say(logger, "The AI's reply could not be understood.",
            level=logging.ERROR, error=exc.message[:300], exit_code=8)
        raise ExtractionError(
            f"Could not parse the extraction response: {exc.message}"
        ) from exc

    matches = _parse_matches(raw)
    found = sum(1 for m in matches if m.found)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    say(logger,
        "Found %d of %d in %s.",
        found, len(keywords), human_duration(elapsed_ms),
        found=found, requested=len(keywords), elapsed_ms=elapsed_ms,
    )

    return ExtractionResult(
        matches=matches,
        fields_extracted_percentage=round(100 * found / len(keywords), 2),
        processing_time_ms=elapsed_ms,
    )
