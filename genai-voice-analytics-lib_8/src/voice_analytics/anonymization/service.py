"""Stripping personally identifiable information from text.

This exists to enforce an **ordering constraint**, not merely to call a service.
In the originating pipeline the comment reads:

    # Translation must run after anonymization so neither the
    # database nor the translation service receives raw PII.

So the correct sequence is transcribe (no translation) -> anonymize ->
translate. Collapsing that into a single transcribe-and-translate call would
send raw PII to the translation model and store it, which is the thing the
ordering prevents.

**This module fails closed.** If anonymization cannot be completed, it raises.
It never returns the original text as a fallback -- doing so would leak exactly
the data it exists to remove. A failed run is recoverable; a leak is not.

The business logic is the originating service's, unchanged:

* the request is ``{"messages": [{"role": "user", "content": text}]}`` -- no
  identifier list, no configuration, because the originating service sends
  none and the identifiers live in the auxiliary service itself;
* the response is searched for the anonymized user message rather than read
  from a fixed path, over the same keys in the same order;
* the whole transcript goes in one request, and the anonymized result is what
  gets stored and what gets translated.

``tests/unit/test_anonymization_parity.py`` runs the originating function and
this one over the same responses and asserts they agree, so the port is
demonstrated rather than claimed.

Three things here are **not** in the original, each a transport concern rather
than a change to what gets anonymized:

* **Retries.** The original made one attempt, so a transient 503 lost the file
  for that run. This makes three, backing off 5s then 10s. A 4xx is still not
  retried, because a malformed request will not become well-formed.
* **TLS with a corporate CA.** The original passed ``verify=SSL_VERIFY``, a
  bare boolean. This shares the gateway's CA bundle, so verification can
  actually be switched on inside the network -- see ADR 0004.
* **A specific exception.** The original raised ``ValueError`` into a generic
  ``except Exception`` that marked the row failed. This raises
  ``AnonymizationError``, which the CLI maps to exit code 9, so an orchestrator
  can tell "PII was not removed" apart from "the model was busy".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from voice_analytics.config import Settings
from voice_analytics.exceptions import AnonymizationError
from voice_analytics.observability import human_duration, say

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
"""Retries after the first attempt, so three attempts in total."""

BASE_RETRY_DELAY_SECONDS = 5
"""Backoff doubles each attempt: 5s, then 10s."""

#: Keys the service has been observed to use for the anonymized payload.
_MESSAGE_KEYS = ("messages", "anonymized_messages", "anonymizedMessages")


def _retry_delay(attempt: int) -> int:
    return BASE_RETRY_DELAY_SECONDS * (2**attempt)


def _content_from_messages(messages: Any) -> str | None:
    """Pull the user message's content out of a messages array."""
    if not isinstance(messages, list):
        return None

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content

    return None


def extract_anonymized_text(response_body: Any) -> str:
    """Find the anonymized user message anywhere in the service's response.

    The response shape is not stable, so this searches the known keys at the top
    level, then recurses. A bare list of messages is also accepted.

    Raises:
        AnonymizationError: No anonymized user message was found. Deliberately
            an error rather than a fallback -- returning the input unchanged
            would defeat the purpose of the call.
    """

    def find(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in _MESSAGE_KEYS:
                content = _content_from_messages(value.get(key))
                if content:
                    return content
            for nested in value.values():
                content = find(nested)
                if content:
                    return content
        elif isinstance(value, list):
            return _content_from_messages(value)
        return None

    content = find(response_body)
    if content:
        return content

    raise AnonymizationError(
        "The anonymization service returned no anonymized text. Refusing to "
        "continue with the original, which may contain personal information."
    )


async def anonymize(
    client: httpx.AsyncClient,
    settings: Settings,
    text: str,
) -> str:
    """Remove personal information from ``text``.

    Args:
        client: From :func:`build_anonymization_client`.
        settings: Supplies the service URL.
        text: The text to anonymize, typically a transcript.

    Returns:
        The anonymized text.

    Raises:
        ValueError: ``text`` is blank.
        AnonymizationError: The service was unreachable after all retries,
            rejected the request, or returned nothing usable.

    Note:
        There is no fallback. If this raises, the caller must **not** proceed
        with the original text -- that is the whole point of the call.
    """
    if not text or not text.strip():
        raise ValueError("text is required")

    payload = {"messages": [{"role": "user", "content": text}]}
    total_attempts = MAX_RETRIES + 1

    say(logger, "Removing personal information from %d characters of text...",
        len(text), service=settings.ANONYMIZATION_URL, chars=len(text))

    started = time.perf_counter()

    for attempt in range(total_attempts):
        try:
            response = await client.post(settings.ANONYMIZATION_URL, json=payload)
            response.raise_for_status()
            clean = extract_anonymized_text(response.json())

            say(logger,
                "Personal information removed in %s (%d characters in, %d out).",
                human_duration((time.perf_counter() - started) * 1000),
                len(text), len(clean),
                chars_in=len(text), chars_out=len(clean),
                attempt=attempt + 1, of=total_attempts,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
            return clean

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # 4xx means the request itself is wrong; retrying cannot fix it.
            if status < 500:
                say(logger,
                    "The anonymization service rejected the request (HTTP %d). "
                    "Not retrying.", status,
                    level=logging.ERROR,
                    status_code=status, retryable=False,
                    service=settings.ANONYMIZATION_URL,
                )
                raise AnonymizationError(
                    f"Anonymization service rejected the request with HTTP {status}"
                ) from exc

            if attempt < MAX_RETRIES:
                delay = _retry_delay(attempt)
                say(logger,
                    "The anonymization service returned an error (HTTP %d). "
                    "Waiting %d seconds, then trying again (attempt %d of %d).",
                    status, delay, attempt + 2, total_attempts,
                    level=logging.WARNING,
                    status_code=status, attempt=attempt + 1, of=total_attempts,
                    retry_in_seconds=delay, retryable=True,
                )
                await asyncio.sleep(delay)
                continue

            say(logger,
                "The anonymization service is still failing after %d attempts "
                "(HTTP %d).", total_attempts, status,
                level=logging.ERROR, status_code=status, attempts=total_attempts,
            )
            raise AnonymizationError(
                f"Anonymization service failed with HTTP {status} after "
                f"{total_attempts} attempts"
            ) from exc

        except httpx.RequestError as exc:
            if attempt < MAX_RETRIES:
                delay = _retry_delay(attempt)
                say(logger,
                    "Could not reach the anonymization service (%s). "
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
                "The anonymization service is unreachable after %d attempts.",
                total_attempts,
                level=logging.ERROR,
                error_type=type(exc).__name__, error=str(exc)[:300],
                attempts=total_attempts, service=settings.ANONYMIZATION_URL,
            )
            raise AnonymizationError(
                f"Anonymization service is unreachable after {total_attempts} attempts"
            ) from exc

        except ValueError as exc:
            # response.json() on a non-JSON body.
            say(logger,
                "The anonymization service returned a response that is not JSON. "
                "Not retrying.",
                level=logging.ERROR, retryable=False, error=str(exc)[:200],
            )
            raise AnonymizationError(
                "Anonymization service returned an invalid response"
            ) from exc

    raise AnonymizationError("Anonymization exhausted all retries")
