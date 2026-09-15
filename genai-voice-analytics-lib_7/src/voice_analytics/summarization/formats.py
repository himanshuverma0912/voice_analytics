"""The summary formats this library supports.

An allow-list, not free text: each format name is interpolated into a prompt
catalogue key (``summarize_{format}``), so only known values may reach it.
"""

from __future__ import annotations

from enum import StrEnum


class SummaryFormat(StrEnum):
    """A supported summary style.

    ``StrEnum`` members compare equal to their string value, so callers may pass
    either ``SummaryFormat.SHORT_SUMMARY`` or ``"short_summary"``.
    """

    BULLET_POINTS = "bullet_points"
    KEY_INSIGHTS = "key_insights"
    SHORT_SUMMARY = "short_summary"

    @property
    def prompt_task(self) -> str:
        """The prompt catalogue key for this format."""
        return f"summarize_{self.value}"


#: Canonical ordering. Output follows this regardless of the order requested,
#: so results are comparable between calls.
FORMAT_ORDER: tuple[SummaryFormat, ...] = (
    SummaryFormat.BULLET_POINTS,
    SummaryFormat.KEY_INSIGHTS,
    SummaryFormat.SHORT_SUMMARY,
)

DEFAULT_FORMATS: tuple[SummaryFormat, ...] = (SummaryFormat.SHORT_SUMMARY,)


def select_formats(requested) -> list[SummaryFormat]:
    """Validate, de-duplicate and order the requested formats.

    Accepts ``SummaryFormat`` members or their string values. Returns them in
    :data:`FORMAT_ORDER`, with duplicates removed. An empty request yields
    :data:`DEFAULT_FORMATS`.

    Raises:
        ValueError: A format is not supported. Named explicitly rather than
            silently dropped, so a typo is not mistaken for a missing summary.
    """
    if not requested:
        return list(DEFAULT_FORMATS)

    resolved: set[SummaryFormat] = set()
    for item in requested:
        try:
            resolved.add(SummaryFormat(item))
        except ValueError as exc:
            supported = ", ".join(f.value for f in FORMAT_ORDER)
            raise ValueError(
                f"Unsupported summary format: {item!r}. Use one of: {supported}."
            ) from exc

    return [fmt for fmt in FORMAT_ORDER if fmt in resolved]


def supported_formats() -> list[str]:
    """The supported format names, in canonical order."""
    return [fmt.value for fmt in FORMAT_ORDER]
