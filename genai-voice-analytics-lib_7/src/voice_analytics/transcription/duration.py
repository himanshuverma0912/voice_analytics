"""Working out how long a call was from its transcript.

Ported unchanged from the originating service, where the value populates
``calls.duration_sec``. Pure text parsing -- no audio is inspected, so this
works on a transcript that has already been anonymized.
"""

from __future__ import annotations

import re

#: ``[MM:SS]`` -- the format this library's own formatter produces.
_SIMPLE = re.compile(r"\[(\d{1,2}):(\d{2})\]")

#: ``[MM:SS-MM:SS]`` -- a span, as some upstream transcripts use.
_RANGE = re.compile(r"\[(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})\]")


def extract_duration_sec(transcript: str | None) -> int | None:
    """The latest timestamp in a transcript, in seconds.

    Returns the largest timestamp found, which approximates the call length --
    the final utterance's start, not the true end. That is what the originating
    service stores, so the numbers stay comparable.

    Returns ``None`` when the transcript is empty or carries no timestamps,
    rather than a misleading zero.
    """
    if not transcript:
        return None

    furthest = 0

    for minutes, seconds in _SIMPLE.findall(transcript):
        furthest = max(furthest, int(minutes) * 60 + int(seconds))

    # A range contributes its end, which is later than its start.
    for _start_m, _start_s, end_m, end_s in _RANGE.findall(transcript):
        furthest = max(furthest, int(end_m) * 60 + int(end_s))

    return furthest or None
