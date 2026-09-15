"""Building and caching LLM clients.

The client is created from explicit ``Settings`` rather than a module-level
singleton, so a caller can hold several clients against different gateways or
credentials in the same process.
"""

from __future__ import annotations

from weakref import WeakSet

import httpx
from openai import AsyncOpenAI

from voice_analytics.config import Settings
from voice_analytics.exceptions import ConfigurationError

_CLIENTS: WeakSet[AsyncOpenAI] = WeakSet()


def build_llm_client(
    settings: Settings,
    api_key: str | None = None,
) -> AsyncOpenAI:
    """Create an ``AsyncOpenAI`` client pointed at the configured gateway.

    ``api_key`` overrides ``settings.LLM_API_KEY``, which is how a caller runs a
    request under an end user's own credential.

    The returned client owns an HTTP connection pool. Reuse it across requests
    and close it with :func:`close_llm_clients` at shutdown -- do not build one
    per call.
    """
    key = api_key or settings.LLM_API_KEY
    if not key:
        raise ConfigurationError(
            "No LLM API key available. Pass api_key= or set LLM_API_KEY."
        )
    if not settings.LLM_BASE_URL:
        raise ConfigurationError("LLM_BASE_URL is not configured.")

    http_client = httpx.AsyncClient(
        http2=True,
        # A bool, or a path to the corporate CA bundle. See Settings.ssl_verify.
        verify=settings.ssl_verify(),
        timeout=settings.REQUEST_TIMEOUT_SECONDS,
    )
    client = AsyncOpenAI(
        api_key=key,
        base_url=settings.LLM_BASE_URL,
        http_client=http_client,
        max_retries=settings.MAX_RETRIES,
    )
    _CLIENTS.add(client)
    return client


async def close_llm_clients() -> None:
    """Close every client built by :func:`build_llm_client`.

    Call this during application shutdown to release sockets. Snapshots the
    registry first: a ``WeakSet`` can shrink while it is being iterated.
    """
    clients = tuple(_CLIENTS)
    _CLIENTS.clear()
    for client in clients:
        await client.close()
