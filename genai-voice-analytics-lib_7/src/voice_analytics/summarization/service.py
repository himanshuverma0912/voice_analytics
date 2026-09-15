"""Summarization: transcript or audio in, summaries out.

Two entry points with a deliberate difference in strategy:

* :func:`summarize_text` issues **one call per format, in parallel**. Each gets
  a focused prompt, which produces better results, and ``asyncio.gather`` means
  three formats cost roughly the time of one.
* :func:`summarize_audio` issues **a single call requesting every format**.
  Audio is expensive to transmit -- base64 inflates it by a third -- so sending
  it three times would triple upload time and cost for a marginal gain.

Both reuse the gateway plumbing in :mod:`voice_analytics.llm.base`.
"""

from __future__ import annotations

import asyncio
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
    LLMAuthenticationError,
    LLMOutputParsingError,
    LLMRateLimitError,
    LLMServiceError,
    SummarizationError,
)
from voice_analytics.llm.base import process_audio_request, process_text_request
from voice_analytics.observability import human_bytes, human_duration
from voice_analytics.prompts import PromptManager, default_prompt_manager
from voice_analytics.schemas.summarization import SummaryResult
from voice_analytics.summarization.formats import SummaryFormat, select_formats

logger = logging.getLogger(__name__)

DEFAULT_DOMAIN = "banking"
MAX_RETRIES = 2
"""Retries after the first attempt, so three attempts in total."""

BASE_RETRY_DELAY_SECONDS = 5
"""Backoff doubles each attempt: 5s, then 10s."""

#: Cap on list items joined into a bullet string, so a runaway response cannot
#: produce an unbounded summary.
MAX_LIST_ITEMS = 100


def _retry_delay(attempt: int) -> int:
    return BASE_RETRY_DELAY_SECONDS * (2**attempt)


def _coerce_summary(value: object) -> str:
    """Force a model's value into a single string.

    The prompt states in capitals that every value must be a string, and the
    model returns a list anyway often enough to matter. A list becomes the
    bullet-point string the prompt asked for.
    """
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        items = [str(item).strip() for item in value[:MAX_LIST_ITEMS] if str(item).strip()]
        return "- " + "\n- ".join(items) if items else ""

    return str(value).strip() if value is not None else ""


def _translate_gateway_error(exc: Exception) -> Exception:
    """Map an OpenAI SDK error onto this library's exception hierarchy."""
    if isinstance(exc, AuthenticationError):
        return LLMAuthenticationError("Invalid API key or gateway credential")
    if isinstance(exc, RateLimitError):
        return LLMRateLimitError("Gateway rate limit exceeded after all retries")
    if isinstance(exc, LLMOutputParsingError):
        return SummarizationError(
            f"Could not parse the summarization response: {exc.message}"
        )
    return LLMServiceError("Gateway failed after all retries")


async def _with_retries(operation, description: str):
    """Run an awaitable factory with the shared retry policy.

    ``operation`` is a zero-argument callable returning a fresh coroutine --
    a coroutine cannot be awaited twice, so each attempt needs a new one.
    """
    total_attempts = MAX_RETRIES + 1

    for attempt in range(total_attempts):
        try:
            return await operation()

        except AuthenticationError as exc:
            logger.error(
                "The AI service rejected our credentials. Check that "
                "LLM_API_KEY is set correctly for this environment. "
                "Not retrying -- a rejected key will not start working."
            )
            raise LLMAuthenticationError(
                "Invalid API key or gateway credential"
            ) from exc

        except (RateLimitError, APIError, APIConnectionError) as exc:
            if attempt < MAX_RETRIES:
                delay = _retry_delay(attempt)
                logger.warning(
                    "The AI service did not respond properly while producing "
                    "the %s summary (%s). Waiting %d seconds, then trying "
                    "again (attempt %d of %d).",
                    description, type(exc).__name__, delay,
                    attempt + 2, total_attempts,
                )
                await asyncio.sleep(delay)
                continue

            logger.error(
                "The AI service failed after %d attempts while producing the "
                "%s summary: %s",
                total_attempts, description, str(exc)[:300],
            )
            raise _translate_gateway_error(exc) from exc

        except LLMOutputParsingError as exc:
            logger.error(
                "The AI's reply could not be understood: %s Not retrying.",
                exc.message[:300],
            )
            raise SummarizationError(
                f"Could not parse the summarization response: {exc.message}"
            ) from exc

    raise LLMServiceError(f"{description} exhausted all retries")


async def summarize_text(
    client: AsyncOpenAI,
    text: str,
    model_name: str,
    formats=None,
    user_instruction: str | None = None,
    domain: str = DEFAULT_DOMAIN,
    prompt_manager: PromptManager | None = None,
    llm_metadata: dict[str, str] | None = None,
) -> SummaryResult:
    """Summarize a transcript, one focused call per format, run in parallel.

    Args:
        client: Gateway client from :func:`voice_analytics.llm.build_llm_client`.
        text: The transcript to summarize.
        model_name: Model to use.
        formats: Formats to produce. Defaults to ``short_summary``.
        user_instruction: Caller instruction that outranks the system prompt.
        domain: Prompt catalogue domain.
        prompt_manager: Override the bundled prompt catalogue.
        llm_metadata: Tags for usage attribution at the gateway.

    Returns:
        A :class:`SummaryResult` keyed by format name.

    Raises:
        ValueError: ``model_name`` missing, ``text`` blank, or a format
            unsupported.
        LLMAuthenticationError, LLMRateLimitError, LLMServiceError,
        SummarizationError: As documented on those types.

    Note:
        If any format fails, the whole call fails -- ``asyncio.gather`` cancels
        the rest. That is deliberate: a partial set of summaries is easy to
        mistake for a complete one.
    """
    started = time.perf_counter()

    if not model_name:
        raise ValueError("model_name is required")
    if not text or not text.strip():
        raise ValueError("text is required")
    selected = select_formats(formats)

    manager = prompt_manager or default_prompt_manager()

    async def _one(fmt: SummaryFormat) -> str:
        system_prompt = manager.get(domain=domain, task=fmt.prompt_task)
        raw = await _with_retries(
            lambda: process_text_request(
                client=client,
                system_prompt=system_prompt,
                text_content=text,
                model_name=model_name,
                user_instruction=user_instruction,
                is_json=False,          # summaries are prose, not JSON
                llm_metadata=llm_metadata,
            ),
            description=f"summarize_{fmt.value}",
        )
        return _coerce_summary(raw)

    logger.info(
        "Summarizing %d characters of transcript using model '%s'. "
        "Requesting %d summary style(s): %s. These run at the same time...",
        len(text), model_name, len(selected),
        ", ".join(f.value for f in selected),
    )

    # Build coroutines first, then gather: creating them does not start them.
    results = await asyncio.gather(*(_one(fmt) for fmt in selected))

    summaries = {
        fmt.value: summary
        for fmt, summary in zip(selected, results, strict=True)
        if summary
    }

    dropped = [f.value for f in selected if f.value not in summaries]
    if dropped:
        logger.warning(
            "The AI returned nothing for: %s. Those summaries are missing "
            "from the result.", ", ".join(dropped),
        )

    logger.info(
        "Summaries ready (%s) in %s.",
        ", ".join(summaries) or "none",
        human_duration((time.perf_counter() - started) * 1000),
    )

    return SummaryResult(
        summaries=summaries,
        processing_time_ms=round((time.perf_counter() - started) * 1000, 2),
    )


async def summarize_audio(
    client: AsyncOpenAI,
    content: bytes,
    mime_type: str,
    model_name: str,
    formats=None,
    user_instruction: str | None = None,
    domain: str = DEFAULT_DOMAIN,
    prompt_manager: PromptManager | None = None,
    llm_metadata: dict[str, str] | None = None,
) -> SummaryResult:
    """Summarize audio directly, without producing a transcript first.

    One call requests every format at once, because re-sending base64 audio per
    format would triple the upload. Cheaper and faster than transcribing then
    summarizing -- but it leaves no transcript to audit the summary against.

    Args and exceptions are as :func:`summarize_text`, with ``content`` and
    ``mime_type`` replacing ``text``.
    """
    started = time.perf_counter()

    if not model_name:
        raise ValueError("model_name is required")
    if not content:
        raise ValueError("content is required")
    selected = select_formats(formats)

    manager = prompt_manager or default_prompt_manager()

    system_prompt = manager.get(
        domain=domain,
        task="direct_audio_summarization",
        formats=", ".join(fmt.value for fmt in selected),
    )

    logger.info(
        "Summarizing %s of audio directly using model '%s'. "
        "Requesting %d summary style(s) in one request: %s...",
        human_bytes(len(content)), model_name, len(selected),
        ", ".join(f.value for f in selected),
    )

    raw = await _with_retries(
        lambda: process_audio_request(
            client=client,
            system_prompt=system_prompt,
            content=content,
            mime_type=mime_type,
            model_name=model_name,
            user_instruction=user_instruction,
            llm_metadata=llm_metadata,
        ),
        description="summarize_audio",
    )

    # Keep only the formats that were asked for: a model that invents a key
    # must not be able to smuggle it into the result.
    summaries: dict[str, str] = {}
    for fmt in selected:
        if fmt.value in raw:
            summary = _coerce_summary(raw[fmt.value])
            if summary:
                summaries[fmt.value] = summary

    missing = [f.value for f in selected if f.value not in summaries]
    if missing:
        logger.warning(
            "The AI returned nothing for: %s. Those summaries are missing "
            "from the result.", ", ".join(missing),
        )

    logger.info(
        "Summaries ready (%s) in %s.",
        ", ".join(summaries) or "none",
        human_duration((time.perf_counter() - started) * 1000),
    )

    return SummaryResult(
        summaries=summaries,
        processing_time_ms=round((time.perf_counter() - started) * 1000, 2),
    )
