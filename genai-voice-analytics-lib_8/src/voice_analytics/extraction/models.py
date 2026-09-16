"""Types for keyword extraction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class KeywordDefinition(BaseModel):
    """One thing to look for, and how to recognise it.

    The ``description`` is the whole instruction the model receives for this
    keyword, so it carries the rules, the edge cases and the examples. The two
    presets in this package are long for exactly that reason.
    """

    keyword: str = Field(description="Name the model must echo back.")
    description: str = Field(
        default="", description="What to extract, in as much detail as needed."
    )


class KeywordMatch(BaseModel):
    """What the model found for one keyword."""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    keyword: str = ""
    found: bool = False
    count: int = 0
    extracted_values: list[str] = Field(default_factory=list)
    context: list[str] = Field(
        default_factory=list, description="Short snippets around each occurrence."
    )


class ExtractionResult(BaseModel):
    """The outcome of one extraction request."""

    matches: list[KeywordMatch] = Field(default_factory=list)
    fields_extracted_percentage: float = 0.0
    processing_time_ms: float = 0.0

    def match(self, keyword: str) -> KeywordMatch | None:
        """The match for one keyword, or ``None`` if the model omitted it."""
        return next((m for m in self.matches if m.keyword == keyword), None)

    @property
    def found_matches(self) -> list[KeywordMatch]:
        return [m for m in self.matches if m.found]
