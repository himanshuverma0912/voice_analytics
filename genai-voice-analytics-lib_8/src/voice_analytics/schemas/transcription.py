"""Result types for transcription.

These are plain data models. Unlike the schemas in the original service they
carry no HTML-escaping serializers -- a library returns data, and the boundary
that renders it decides how to encode it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TranscriptSegment(BaseModel):
    """One utterance, as returned by the model."""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    timestamp: str | None = Field(
        default=None, description="Position in the audio, usually MM:SS."
    )
    text: str = Field(default="", description="Verbatim speech.")
    translated_text: str | None = Field(
        default=None, description="Translation, when a target language was requested."
    )
    romanized_text: str | None = Field(
        default=None, description="Latin-script rendering, when romanization was requested."
    )


class TranscriptionResult(BaseModel):
    """The full outcome of one transcription call."""

    segments: list[TranscriptSegment] = Field(default_factory=list)
    primary_language: str | None = Field(
        default=None, description="Language the model detected in the audio."
    )
    transcript: str = Field(
        default="", description="Segments flattened to timestamped lines."
    )
    translated_transcript: str | None = Field(
        default=None, description="Translated transcript, when requested."
    )
    processing_time_ms: float = Field(
        default=0.0, description="Wall-clock duration of the call."
    )

    @property
    def is_empty(self) -> bool:
        """True when the model returned no usable speech."""
        return not self.transcript.strip()
