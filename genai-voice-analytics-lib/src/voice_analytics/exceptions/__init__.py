"""Exceptions raised by this library."""

from voice_analytics.exceptions.base import (
    ConfigurationError,
    LLMAuthenticationError,
    LLMOutputParsingError,
    LLMRateLimitError,
    LLMServiceError,
    PromptNotFoundError,
    TranscriptionError,
    VoiceAnalyticsError,
)

__all__ = [
    "ConfigurationError",
    "LLMAuthenticationError",
    "LLMOutputParsingError",
    "LLMRateLimitError",
    "LLMServiceError",
    "PromptNotFoundError",
    "TranscriptionError",
    "VoiceAnalyticsError",
]
