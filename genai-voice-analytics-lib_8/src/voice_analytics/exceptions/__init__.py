"""Exceptions raised by this library."""

from voice_analytics.exceptions.base import (
    AnalysisError,
    AnonymizationError,
    ConfigurationError,
    ExtractionError,
    LLMAuthenticationError,
    LLMOutputParsingError,
    LLMRateLimitError,
    LLMServiceError,
    PromptHubError,
    PromptNotFoundError,
    SummarizationError,
    TranscriptionError,
    TransferError,
    VoiceAnalyticsError,
)

__all__ = [
    "AnalysisError",
    "AnonymizationError",
    "ConfigurationError",
    "ExtractionError",
    "LLMAuthenticationError",
    "LLMOutputParsingError",
    "LLMRateLimitError",
    "LLMServiceError",
    "PromptHubError",
    "PromptNotFoundError",
    "SummarizationError",
    "TranscriptionError",
    "TransferError",
    "VoiceAnalyticsError",
]
