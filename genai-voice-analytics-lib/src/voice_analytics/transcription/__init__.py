"""Transcription: audio in, structured transcript out."""

from voice_analytics.transcription.formatting import (
    format_segments,
    parse_segments,
    plain_text,
)
from voice_analytics.transcription.languages import (
    INDIAN_LANGUAGES,
    normalize_language,
    supported_languages,
)
from voice_analytics.transcription.service import transcribe, translate

__all__ = [
    "INDIAN_LANGUAGES",
    "format_segments",
    "normalize_language",
    "parse_segments",
    "plain_text",
    "supported_languages",
    "transcribe",
    "translate",
]
