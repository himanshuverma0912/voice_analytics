"""HTTP client for the anonymization service."""

from __future__ import annotations

import httpx

from voice_analytics.config import Settings
from voice_analytics.exceptions import ConfigurationError


def build_anonymization_client(settings: Settings) -> httpx.AsyncClient:
    """Create a client for the anonymization service.

    Shares the TLS configuration used for the LLM gateway, so a corporate CA
    configured once applies to both.

    The returned client owns a connection pool. Reuse it across calls and close
    it when done -- ``async with`` is the simplest way::

        async with build_anonymization_client(settings) as client:
            clean = await anonymize(client, settings, transcript)

    Raises:
        ConfigurationError: ``ANONYMIZATION_URL`` is not set. Raised here rather
            than at the first request so the problem surfaces at startup.
    """
    if not settings.ANONYMIZATION_URL:
        raise ConfigurationError(
            "ANONYMIZATION_URL is not configured. Anonymization cannot run "
            "without it, and processing unanonymized text is not an option."
        )

    return httpx.AsyncClient(
        verify=settings.ssl_verify(),
        timeout=settings.ANONYMIZATION_TIMEOUT_SECONDS,
    )
