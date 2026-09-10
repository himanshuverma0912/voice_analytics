"""Turning model segments into readable transcript text."""

from __future__ import annotations

import re
from typing import Any

from voice_analytics.schemas.transcription import TranscriptSegment

#: Matches a leading speaker label such as "[Agent]:", "[Customer]" or
#: "[Speaker 1]:". Group 1 captures the label including its optional colon.
SPEAKER_LABEL_PATTERN = re.compile(
    r"^\s*(\[(?:Agent|Customer|Speaker\s*[a-z0-9_-]+)\]:?)(?:\s+|$)",
    re.IGNORECASE,
)


def parse_segments(raw: Any) -> list[TranscriptSegment]:
    """Coerce whatever the model returned into a list of segments.

    Accepts ``{"segments": [...]}`` or a bare list, and skips entries that are
    not objects. Model output is untrusted input: it is validated, not assumed.
    """
    if isinstance(raw, dict):
        items = raw.get("segments") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    if not isinstance(items, list):
        return []

    return [
        TranscriptSegment.model_validate(item)
        for item in items
        if isinstance(item, dict)
    ]


def format_segments(
    segments: list[TranscriptSegment],
    key: str = "text",
    fallback: str | None = None,
    preserve_speaker_from: str | None = None,
) -> str:
    """Render segments as ``[MM:SS] text`` lines.

    ``fallback`` names a second field to use when ``key`` is empty, so an
    untranslated segment shows its original text instead of a blank line.

    ``preserve_speaker_from`` re-attaches a speaker label that the model dropped
    during translation. The prompt asks it to keep the label; this handles the
    cases where it does not. Asking the model *and* correcting in code is the
    right posture -- models follow instructions most of the time, not always.
    """
    lines: list[str] = []

    for segment in segments:
        timestamp = segment.timestamp or "N/A"

        text = getattr(segment, key, None) or ""
        if not text and fallback:
            text = getattr(segment, fallback, None) or ""
        text = str(text)

        if preserve_speaker_from:
            source_text = str(getattr(segment, preserve_speaker_from, None) or "")
            source_label = SPEAKER_LABEL_PATTERN.match(source_text)
            if source_label and not SPEAKER_LABEL_PATTERN.match(text):
                text = f"{source_label.group(1)} {text}"

        lines.append(f"[{timestamp}] {text}")

    return "\n".join(lines)


def plain_text(segments: list[TranscriptSegment], key: str = "text") -> str:
    """Join segment text with spaces, dropping timestamps.

    Useful when feeding a transcript to another model, which does not need the
    timing information.
    """
    return " ".join(
        str(getattr(segment, key, None) or "") for segment in segments
    ).strip()
