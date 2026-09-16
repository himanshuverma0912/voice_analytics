"""Result types returned by the library."""

from voice_analytics.schemas.summarization import SummaryResult
from voice_analytics.schemas.transcription import (
    TranscriptionResult,
    TranscriptSegment,
)

__all__ = ["SummaryResult", "TranscriptSegment", "TranscriptionResult"]
