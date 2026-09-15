"""Result types for summarization."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SummaryResult(BaseModel):
    """Summaries keyed by format name.

    Keys are :class:`~voice_analytics.summarization.SummaryFormat` values such
    as ``"short_summary"``. A format that the model failed to produce is absent
    rather than present-but-empty, so ``in`` is a meaningful test.
    """

    summaries: dict[str, str] = Field(default_factory=dict)
    processing_time_ms: float = Field(
        default=0.0, description="Wall-clock duration of the call."
    )

    @property
    def is_empty(self) -> bool:
        """True when no format produced usable text."""
        return not any(value.strip() for value in self.summaries.values())

    def get(self, fmt: str) -> str | None:
        """The summary for a format, or ``None`` when it was not produced."""
        return self.summaries.get(str(fmt))
