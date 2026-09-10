"""Library configuration.

Deliberately different from an application's config module: this file defines
the ``Settings`` *class* but never instantiates it at import time.  A library
that reads ``.env`` on import would inspect the *caller's* working directory
and could fail before the caller has had a chance to configure anything.

Callers build a ``Settings`` object explicitly, either from the environment::

    from voice_analytics.config import load_settings
    settings = load_settings()

or by passing values directly, which is what tests should do::

    settings = Settings(LLM_BASE_URL="https://llm.internal/v1", LLM_API_KEY="sk-...")
"""

from __future__ import annotations

import ssl
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL_NAME = "gemini-2.5-flash"
DEFAULT_TRANSCRIPTION_MODEL_NAMES = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-transcription",
)


class Settings(BaseSettings):
    """Connection and model settings needed to talk to an LLM gateway."""

    # ------------------------------------------------------------------
    # LLM gateway (LiteLLM or any OpenAI-compatible endpoint)
    # ------------------------------------------------------------------
    LLM_BASE_URL: str
    """Base URL of the OpenAI-compatible gateway. Required."""

    LLM_API_KEY: str = ""
    """Fallback API key. Callers may instead pass a key per request."""

    MODEL_NAME: str = DEFAULT_MODEL_NAME
    """Model used when a caller does not name one."""

    TRANSCRIPTION_MODEL_NAMES: str = ",".join(DEFAULT_TRANSCRIPTION_MODEL_NAMES)
    """Comma-separated allow-list of models able to process audio."""

    # ------------------------------------------------------------------
    # TLS
    # ------------------------------------------------------------------
    SSL_VERIFY: bool = False
    """Verify the gateway's TLS certificate.

    ``False`` matches the current service default and suits internal endpoints
    using self-signed certificates. Prefer ``True`` together with
    ``SSL_CA_BUNDLE`` wherever a corporate CA is available.
    """

    SSL_CA_BUNDLE: str | None = None
    """Path to a PEM bundle holding the corporate CA, e.g. ``/etc/ssl/certs/corp-ca.pem``.

    Required when ``SSL_VERIFY`` is true and the gateway presents a certificate
    signed by an internal CA -- the system trust store will not contain it, so
    verification fails without this. Setting it implies verification is on.
    """

    MAX_RETRIES: int = 3
    """Retries the OpenAI SDK performs for transient failures.

    This stacks with the retry loop in :func:`voice_analytics.transcription.transcribe`
    -- three attempts here multiply with the SDK's own. Set to ``0`` to let this
    library's policy be the only one.
    """

    REQUEST_TIMEOUT_SECONDS: float = 120.0
    """Per-request timeout. Audio transcription can legitimately be slow."""

    model_config = SettingsConfigDict(
        env_file=None,  # opt in via load_settings(env_file=...)
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def ssl_verify(self) -> bool | ssl.SSLContext:
        """Resolve TLS settings into the value httpx expects for ``verify``.

        Returns an :class:`ssl.SSLContext` when a CA bundle is configured, and
        a plain boolean otherwise. A context is built rather than a path being
        passed through because ``verify=<str>`` is deprecated in httpx.

        A configured bundle implies verification, so it wins over a false
        ``SSL_VERIFY``: supplying a CA is an explicit intent to verify.

        Raises:
            ConfigurationError: The bundle is missing or not a readable PEM.
                Failing here beats an opaque handshake error mid-request.
        """
        if not self.SSL_CA_BUNDLE:
            return self.SSL_VERIFY

        from voice_analytics.exceptions import ConfigurationError

        bundle = Path(self.SSL_CA_BUNDLE)
        if not bundle.is_file():
            raise ConfigurationError(
                f"SSL_CA_BUNDLE does not exist: {self.SSL_CA_BUNDLE}"
            )

        try:
            return ssl.create_default_context(cafile=str(bundle))
        except (ssl.SSLError, OSError) as exc:
            raise ConfigurationError(
                f"SSL_CA_BUNDLE is not a readable PEM bundle "
                f"({self.SSL_CA_BUNDLE}): {exc}"
            ) from exc

    def transcription_models(self) -> set[str]:
        """Parse ``TRANSCRIPTION_MODEL_NAMES`` into a set of model names."""
        raw = self.TRANSCRIPTION_MODEL_NAMES
        if not raw:
            return set(DEFAULT_TRANSCRIPTION_MODEL_NAMES)
        return {name.strip() for name in raw.split(",") if name.strip()}

    def supports_transcription(self, model_name: str) -> bool:
        """Whether ``model_name`` is allowed to process audio."""
        return model_name in self.transcription_models()


@lru_cache(maxsize=8)
def load_settings(env_file: str | None = ".env") -> Settings:
    """Build ``Settings`` from the environment, optionally reading ``env_file``.

    Cached so repeated calls in one process are free. Pass ``env_file=None`` to
    read only real environment variables and skip any dotenv file.

    Raises ``pydantic.ValidationError`` when a required setting is missing --
    call this once, early, at the edge of your application.
    """
    return Settings(_env_file=env_file)
