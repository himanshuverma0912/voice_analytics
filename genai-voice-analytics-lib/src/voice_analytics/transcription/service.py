"""Transcription: audio in, structured transcript out.

The public entry point is :func:`transcribe`. It owns the retry policy and the
translation of gateway failures into library exceptions; the gateway mechanics
live in ``voice_analytics.llm.base``.
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
    TranscriptionError,
)
from voice_analytics.llm.base import process_audio_request, process_text_request
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
    if not model_name:
        raise ValueError("model_name is required")
    if not content:
        raise ValueError("content is required")

    normalized_lang = normalize_language(target_lang)
    manager = prompt_manager or default_prompt_manager()

    system_prompt = manager.get(
        domain=domain,
        task="transcription",
        target_lang=normalized_lang,
        romanize=romanize,
    )

    started = time.perf_counter()

    for attempt in range(MAX_RETRIES + 1):
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
            return _build_result(raw, normalized_lang, started)

        except AuthenticationError as exc:
            # A rejected key will still be rejected in five seconds.
            logger.error("Gateway rejected the credential during transcription")
            raise LLMAuthenticationError(
                "Invalid API key or gateway credential"
            ) from exc

        except RateLimitError as exc:
            if attempt < MAX_RETRIES:
                delay = _retry_delay(attempt)
                logger.warning(
                    "Rate limited, retry %s/%s in %ss",
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise LLMRateLimitError(
                "Gateway rate limit exceeded after all retries"
            ) from exc

        except (APIError, APIConnectionError) as exc:
            hint = _multimodal_hint(exc) if isinstance(exc, APIError) else None
            if hint:
                # A model-capability mismatch is a caller error, not a blip.
                raise TranscriptionError(hint) from exc

            if attempt < MAX_RETRIES:
                delay = _retry_delay(attempt)
                logger.warning(
                    "Transient gateway failure, retry %s/%s in %ss",
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise LLMServiceError("Gateway failed after all retries") from exc

        except LLMOutputParsingError as exc:
            raise TranscriptionError(
                f"Could not parse the transcription response: {exc.message}"
            ) from exc

    raise LLMServiceError("Transcription exhausted all retries")


def _build_result(
    raw: dict,
    normalized_lang: str | None,
    started: float,
) -> TranscriptionResult:
    """Assemble the result object from a parsed gateway response."""
    segments = parse_segments(raw)

    transcript = format_segments(segments, key="text")
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

    return await process_text_request(
        client=client,
        system_prompt=system_prompt,
        text_content=text,
        model_name=model_name,
        user_instruction=user_instruction,
        is_json=False,
        llm_metadata=llm_metadata,
    )
