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
DEFAULT_ANALYSIS_MODEL_NAME = "qwen3-30b-a3b-instruct"
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
    """Model used for audio work when a caller does not name one."""

    ANALYSIS_MODEL_NAME: str = DEFAULT_ANALYSIS_MODEL_NAME
    """Model used for scoring and extraction when a caller does not name one.

    Separate from ``MODEL_NAME`` because they are different models on
    different gateways in the originating service: Gemini transcribes, Qwen
    scores. Falling back to ``MODEL_NAME`` for scoring would quietly judge
    calls with the transcription model -- different scores, no warning.

    The originating service had this setting too, as ``ANALYTICS_MODEL_NAME``,
    and ignored it: ``call_llm`` hardcoded the model name and read neither the
    setting nor its own ``model_name`` argument. Here it is honoured.
    """

    TRANSCRIPTION_MODEL_NAMES: str = ",".join(DEFAULT_TRANSCRIPTION_MODEL_NAMES)
    """Comma-separated allow-list of models able to process audio."""

    # ------------------------------------------------------------------
    # PromptHub (KPI prompt registry)
    # ------------------------------------------------------------------
    PROMPTHUB_BASE_URL: str = ""
    """Base URL of the prompt registry. Only needed when resolving KPI prompts."""

    PROMPTHUB_PROMPTS_PATH: str = "/extenal/get-all-prompts"
    """Path to the prompt listing. The spelling matches the upstream service."""

    PROMPTHUB_TIMEOUT_SECONDS: float = 60.0
    """Per-request timeout. The listing can be large."""

    PROMPTHUB_DEFAULT_LITELLM_KEY: str = ""
    """Key for a *default* use case, consulted when the active one has no
    prompt for a KPI -- or none at all.

    The originating service called this ``DEFAULT_USECASE_LITELLM_KEY`` and
    hardcoded it in source. It is optional here: leave it unset and a KPI with
    no prompt of its own is simply skipped, which is the same outcome as a
    default use case that does not define it either.
    """

    # ------------------------------------------------------------------
    # Anonymization service
    # ------------------------------------------------------------------
    ANONYMIZATION_URL: str = ""
    """Endpoint that strips personal information from text.

    No default: a wrong URL would silently skip a compliance control. Callers
    that never anonymize can leave it unset -- it is only required when
    :func:`voice_analytics.anonymization.anonymize` is used.
    """

    ANONYMIZATION_TIMEOUT_SECONDS: float = 30.0
    """Per-request timeout for the anonymization service."""

    # ------------------------------------------------------------------
    # SFTP source (only needed by the transfer command)
    # ------------------------------------------------------------------
    SFTP_HOST: str = ""
    """Hostname of the SFTP server holding the recordings."""

    SFTP_PORT: int = 22

    SFTP_USERNAME: str = ""

    SFTP_PASSWORD: str = ""
    """Password auth. Prefer a key -- this exists because some corporate SFTP
    servers still offer nothing else."""

    SFTP_PRIVATE_KEY_PATH: str = ""
    """Path to a private key. Takes precedence over ``SFTP_PASSWORD``."""

    SFTP_PRIVATE_KEY_PASSPHRASE: str = ""

    SFTP_KNOWN_HOSTS: str = ""
    """Path to a known_hosts file naming the server's host key.

    **Required.** Without it there is nothing to prove the host answering is
    the host expected, and an SFTP session carries both the credential and the
    recordings. The transfer refuses to run rather than trusting whatever
    answers -- see :meth:`sftp_missing_settings`.
    """

    SFTP_TIMEOUT_SECONDS: float = 60.0

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

    def sftp_missing_settings(self) -> list[str]:
        """Which SFTP settings are missing, in the order a reader should fix them.

        Returned rather than raised so a caller can report every problem at
        once instead of one failed run per missing variable.
        """
        missing = [
            name for name in ("SFTP_HOST", "SFTP_USERNAME", "SFTP_KNOWN_HOSTS")
            if not getattr(self, name)
        ]
        if not self.SFTP_PASSWORD and not self.SFTP_PRIVATE_KEY_PATH:
            missing.append("SFTP_PASSWORD or SFTP_PRIVATE_KEY_PATH")
        return missing

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
