"""Transcription: audio in, structured transcript out.

The public entry point is :func:`transcribe`. It owns the retry policy and the
translation of gateway failures into library exceptions; the gateway mechanics
live in ``voice_analytics.llm.base``.

Every step is logged as a start/complete pair with a duration, so an Airflow
task log shows exactly where a slow or failed run spent its time. The error
handlers here add context and re-raise -- none of them swallow a failure.
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
    PromptNotFoundError,
    TranscriptionError,
)
from voice_analytics.llm.base import process_audio_request, process_text_request
from voice_analytics.observability import human_bytes, human_duration, say
from voice_analytics.prompts import PromptManager, default_prompt_manager
from voice_analytics.schemas.transcription import TranscriptionResult
from voice_analytics.transcription.formatting import format_segments, parse_segments
from voice_analytics.transcription.languages import normalize_language

logger = logging.getLogger(__name__)

DEFAULT_DOMAIN = "banking"
MAX_RETRIES = 2
"""Retries after the first attempt, so three attempts in total."""

BASE_RETRY_DELAY_SECONDS = 5
"""Backoff doubles each attempt: 5s, then 10s."""


def _retry_delay(attempt: int) -> int:
    """Exponential backoff. Fixed delays let every client retry in lockstep."""
    return BASE_RETRY_DELAY_SECONDS * (2**attempt)


def _multimodal_hint(error: APIError) -> str | None:
    """Turn the gateway's opaque multimodal error into an actionable message."""
    if "is not a multimodal model" in str(error):
        return (
            "The selected model cannot process audio. Choose a multimodal, "
            "transcription-capable model."
        )
    return None


async def transcribe(
    client: AsyncOpenAI,
    content: bytes,
    mime_type: str,
    model_name: str,
    target_lang: str | None = None,
    romanize: bool = False,
    user_instruction: str | None = None,
    domain: str = DEFAULT_DOMAIN,
    prompt_manager: PromptManager | None = None,
    llm_metadata: dict[str, str] | None = None,
) -> TranscriptionResult:
    """Transcribe audio, optionally translating and romanizing it.

    Args:
        client: Gateway client from :func:`voice_analytics.llm.build_llm_client`.
        content: Raw audio bytes.
        mime_type: MIME type of the audio, e.g. ``"audio/wav"``.
        model_name: A model able to accept audio input.
        target_lang: Language code or name to translate into. ``None`` skips
            translation. Validated against the supported allow-list.
        romanize: Ask for Latin-script output.
        user_instruction: Caller instruction that outranks the system prompt.
        domain: Prompt catalogue domain. Defaults to ``"banking"``.
        prompt_manager: Override the bundled prompt catalogue.
        llm_metadata: Tags for usage attribution at the gateway.

    Returns:
        A :class:`TranscriptionResult` with segments and flattened text.

    Raises:
        ValueError: ``model_name`` missing, ``content`` empty, or ``target_lang``
            unsupported.
        LLMAuthenticationError: The gateway rejected the credential.
        LLMRateLimitError: Still rate-limited after all retries.
        LLMServiceError: Gateway unreachable or failing after all retries.
        TranscriptionError: The response could not be parsed.

    Note:
        Translation happens in the same call as transcription. Where the
        transcript must be anonymized before any PII reaches storage or a second
        service, call this with ``target_lang=None``, anonymize, then translate
        the clean text with :func:`translate`.
    """
    started = time.perf_counter()

    if not model_name:
        raise ValueError("model_name is required")
    if not content:
        raise ValueError("content is required")
    normalized_lang = normalize_language(target_lang)

    manager = prompt_manager or default_prompt_manager()
    try:
        system_prompt = manager.get(
            domain=domain,
            task="transcription",
            target_lang=normalized_lang,
            romanize=romanize,
        )
    except PromptNotFoundError:
        # Add the context a reader needs, then let it propagate unchanged.
        say(logger,
            "No instructions found for '%s / transcription'.", domain,
            level=logging.ERROR,
            domain=domain, task="transcription",
            available_domains=manager.domains(),
        )
        raise

    say(logger,
        "Transcribing %s of audio%s using model '%s'...",
        human_bytes(len(content)),
        f" and translating to {normalized_lang}" if normalized_lang else "",
        model_name,
        model=model_name, audio_bytes=len(content), mime_type=mime_type,
        target_lang=normalized_lang, romanize=romanize,
        prompt_chars=len(system_prompt),
    )

    raw = await _call_gateway_with_retries(
        client=client,
        system_prompt=system_prompt,
        content=content,
        mime_type=mime_type,
        model_name=model_name,
        user_instruction=user_instruction,
        llm_metadata=llm_metadata,
    )

    result = _build_result(raw, normalized_lang, started, romanize)

    if result.is_empty:
        # Not raised here: the caller decides whether silence is a failure.
        say(logger,
            "The AI returned no speech. The audio may be silent, corrupt, "
            "or not a recording of a conversation.",
            level=logging.WARNING,
            segments=0, audio_bytes=len(content), model=model_name,
        )
    else:
        say(logger,
            "Transcript ready: %d segments, %d characters, language detected as %s.",
            len(result.segments), len(result.transcript),
            result.primary_language or "unknown",
            segments=len(result.segments),
            transcript_chars=len(result.transcript),
            language=result.primary_language,
            translated=bool(result.translated_transcript),
            elapsed_ms=result.processing_time_ms,
        )

    return result


async def _call_gateway_with_retries(
    client: AsyncOpenAI,
    system_prompt: str,
    content: bytes,
    mime_type: str,
    model_name: str,
    user_instruction: str | None,
    llm_metadata: dict[str, str] | None,
) -> dict:
    """Send the request, retrying transient failures with exponential backoff.

    Each error class is logged with whether it is being retried and why, so the
    task log explains the decision rather than only its outcome.
    """
    total_attempts = MAX_RETRIES + 1

    for attempt in range(total_attempts):
        attempt_started = time.perf_counter()

        try:
            raw = await process_audio_request(
                client=client,
                system_prompt=system_prompt,
                content=content,
                mime_type=mime_type,
                model_name=model_name,
                user_instruction=user_instruction,
                llm_metadata=llm_metadata,
            )
            say(logger,
                "AI service responded in %s.",
                human_duration((time.perf_counter() - attempt_started) * 1000),
                attempt=attempt + 1, of=total_attempts,
                elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                response_keys=sorted(raw) if isinstance(raw, dict) else "non-dict",
            )
            return raw

        except AuthenticationError as exc:
            # A rejected key will still be rejected in five seconds.
            say(logger,
                "The AI service rejected our credentials. Check that "
                "LLM_API_KEY is set correctly for this environment. "
                "Not retrying -- a rejected key will not start working.",
                level=logging.ERROR,
                error_type=type(exc).__name__, retryable=False, exit_code=5,
            )
            raise LLMAuthenticationError(
                "Invalid API key or gateway credential"
            ) from exc

        except RateLimitError as exc:
            if attempt < MAX_RETRIES:
                delay = _retry_delay(attempt)
                say(logger,
                    "The AI service is busy (too many requests). "
                    "Waiting %d seconds, then trying again (attempt %d of %d).",
                    delay, attempt + 2, total_attempts,
                    level=logging.WARNING,
                    error_type=type(exc).__name__, attempt=attempt + 1,
                    of=total_attempts, retry_in_seconds=delay, retryable=True,
                )
                await asyncio.sleep(delay)
                continue

            say(logger,
                "The AI service is still too busy after %d attempts. "
                "Try again later.", total_attempts,
                level=logging.ERROR,
                error_type=type(exc).__name__, attempts=total_attempts, exit_code=6,
            )
            raise LLMRateLimitError(
                "Gateway rate limit exceeded after all retries"
            ) from exc

        except (APIError, APIConnectionError) as exc:
            hint = _multimodal_hint(exc) if isinstance(exc, APIError) else None
            if hint:
                # A capability mismatch is a caller error, not a transient blip.
                say(logger, "%s Not retrying.", hint,
                    level=logging.ERROR,
                    model=model_name, retryable=False, exit_code=8,
                )
                raise TranscriptionError(hint) from exc

            if attempt < MAX_RETRIES:
                delay = _retry_delay(attempt)
                say(logger,
                    "The AI service did not respond properly (%s). "
                    "Waiting %d seconds, then trying again (attempt %d of %d).",
                    type(exc).__name__, delay, attempt + 2, total_attempts,
                    level=logging.WARNING,
                    error_type=type(exc).__name__, error=str(exc)[:200],
                    attempt=attempt + 1, of=total_attempts,
                    retry_in_seconds=delay, retryable=True,
                )
                await asyncio.sleep(delay)
                continue

            say(logger,
                "The AI service failed after %d attempts.", total_attempts,
                level=logging.ERROR,
                error_type=type(exc).__name__, error=str(exc)[:300],
                attempts=total_attempts, exit_code=7,
            )
            raise LLMServiceError("Gateway failed after all retries") from exc

        except LLMOutputParsingError as exc:
            # Retrying rarely helps a model that is emitting malformed output.
            say(logger,
                "The AI's reply could not be understood. Not retrying.",
                level=logging.ERROR,
                error=exc.message[:300], retryable=False, exit_code=8,
            )
            raise TranscriptionError(
                f"Could not parse the transcription response: {exc.message}"
            ) from exc

    raise LLMServiceError("Transcription exhausted all retries")


def _build_result(
    raw: dict,
    normalized_lang: str | None,
    started: float,
    romanize: bool = False,
) -> TranscriptionResult:
    """Assemble the result object from a parsed gateway response."""
    segments = parse_segments(raw)

    transcript = format_segments(segments, key="text", prefer_romanized=romanize)
    translated_transcript = (
        format_segments(
            segments,
            key="translated_text",
            fallback="text",
            preserve_speaker_from="text",
        )
        if normalized_lang
        else None
    )

    return TranscriptionResult(
        segments=segments,
        primary_language=raw.get("primary_language") if isinstance(raw, dict) else None,
        transcript=transcript,
        translated_transcript=translated_transcript,
        processing_time_ms=round((time.perf_counter() - started) * 1000, 2),
    )


async def translate(
    client: AsyncOpenAI,
    text: str,
    target_lang: str,
    model_name: str,
    user_instruction: str | None = None,
    domain: str = DEFAULT_DOMAIN,
    prompt_manager: PromptManager | None = None,
    llm_metadata: dict[str, str] | None = None,
) -> str:
    """Translate an existing transcript, preserving timestamps and labels.

    Separate from :func:`transcribe` so a transcript can be anonymized before
    it is sent anywhere else.
    """
    if not model_name:
        raise ValueError("model_name is required")
    if not text.strip():
        raise ValueError("text is required")
    normalized_lang = normalize_language(target_lang)
    if not normalized_lang:
        raise ValueError("target_lang is required")

    manager = prompt_manager or default_prompt_manager()
    system_prompt = manager.get(
        domain=domain, task="translation", target_lang=normalized_lang
    )

    say(logger, "Translating %d characters to %s...", len(text), normalized_lang,
        model=model_name, target_lang=normalized_lang, text_chars=len(text),
    )

    translated = await process_text_request(
        client=client,
        system_prompt=system_prompt,
        text_content=text,
        model_name=model_name,
        user_instruction=user_instruction,
        is_json=False,
        llm_metadata=llm_metadata,
    )
    return translated
