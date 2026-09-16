"""Library exceptions.

These carry no HTTP status codes. A library should not assume it is being
called from a web service -- mapping these onto HTTP responses is the calling
application's job.
"""

from __future__ import annotations

from typing import Any


class VoiceAnalyticsError(Exception):
    """Base class for every error raised by this library."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)


class ConfigurationError(VoiceAnalyticsError):
    """A required setting is missing or invalid."""


class LLMOutputParsingError(VoiceAnalyticsError):
    """The model's response could not be parsed into usable JSON."""


class LLMAuthenticationError(VoiceAnalyticsError):
    """The gateway rejected the supplied API key."""


class LLMRateLimitError(VoiceAnalyticsError):
    """The gateway rate-limited the request after all retries."""


class LLMServiceError(VoiceAnalyticsError):
    """The gateway failed or was unreachable after all retries."""


class TranscriptionError(VoiceAnalyticsError):
    """Transcription could not be completed."""


class SummarizationError(VoiceAnalyticsError):
    """Summarization could not be completed."""


class AnalysisError(VoiceAnalyticsError):
    """KPI scoring could not be completed."""


class ExtractionError(VoiceAnalyticsError):
    """Values could not be extracted from a transcript.

    Raised only when the response was unusable. A keyword the model simply did
    not find is not an error -- it comes back as ``found=False``.
    """


class TransferError(VoiceAnalyticsError):
    """Recordings could not be moved from the source to the destination.

    Raised for a failure that stops the transfer -- an unreachable server, a
    rejected credential, an unlistable directory. A failure on one individual
    file is reported in the result instead, so one bad recording does not
    abandon the rest.
    """


class PromptHubError(VoiceAnalyticsError):
    """Prompt configuration could not be resolved from PromptHub."""


class AnonymizationError(VoiceAnalyticsError):
    """Personal information could not be removed.

    Always fatal for the caller: continuing with the original text would leak
    exactly the data anonymization exists to remove.
    """


class PromptNotFoundError(VoiceAnalyticsError):
    """No prompt exists for the requested domain and task."""
