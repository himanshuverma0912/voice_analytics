"""Shared helpers for CLI commands.

Two conventions every command follows:

* **Results go to stdout, diagnostics go to stderr.** A caller can pipe stdout
  into another tool without log lines corrupting it.
* **Secrets come from the environment, never from arguments.** Command-line
  arguments are visible in pod specs, ``ps`` output and Airflow task logs.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from voice_analytics.observability import configure_logging as _configure_logging
from voice_analytics.observability import human_bytes, say

logger = logging.getLogger("voice_analytics")


# Re-exported so commands keep importing it from one place. The
# implementation lives in voice_analytics.observability.
configure_logging = _configure_logging


def read_bytes(path: str) -> bytes:
    """Read a binary input file, raising ``ValueError`` with a clear message."""
    file_path = Path(path)
    if not file_path.is_file():
        say(logger, "Input file not found: %s", file_path.resolve(),
            level=logging.ERROR, path=str(file_path), exit_code=4)
        raise ValueError(f"Input file not found: {path}")
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        say(logger, "Could not read %s.", path,
            level=logging.ERROR, path=path, error=str(exc), exit_code=4)
        raise ValueError(f"Could not read input file {path}: {exc}") from exc

    say(logger, "Read input file %s (%s).", file_path.name, human_bytes(len(data)),
        path=str(file_path), bytes=len(data))
    return data


def read_text(path: str, encoding: str = "utf-8") -> str:
    """Read a text input file, raising ``ValueError`` with a clear message."""
    file_path = Path(path)
    if not file_path.is_file():
        say(logger, "Input file not found: %s", file_path.resolve(),
            level=logging.ERROR, path=str(file_path), exit_code=4)
        raise ValueError(f"Input file not found: {path}")
    try:
        text = file_path.read_text(encoding=encoding)
    except (OSError, UnicodeDecodeError) as exc:
        say(logger, "Could not read %s.", path,
            level=logging.ERROR, path=path, error=str(exc), exit_code=4)
        raise ValueError(f"Could not read input file {path}: {exc}") from exc

    say(logger, "Read input file %s (%d characters).", file_path.name, len(text),
        path=str(file_path), chars=len(text), encoding=encoding)
    return text


def write_json(payload: Any, path: str | None) -> None:
    """Write a result as JSON to ``path``, or to stdout when ``path`` is None or '-'.

    ``ensure_ascii=False`` keeps Devanagari, Tamil and other non-Latin scripts
    as real characters rather than escape sequences.
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    if path in (None, "-"):
        sys.stdout.write(text + "\n")
        return

    out_path = Path(path)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        say(logger, "Could not write %s.", path,
            level=logging.ERROR, path=path, error=str(exc), exit_code=4)
        raise ValueError(f"Could not write output file {path}: {exc}") from exc

    say(logger, "Saved result to %s (%s).", out_path, human_bytes(len(text)),
        path=str(out_path), bytes=len(text))


def infer_mime_type(path: str, override: str | None = None) -> str:
    """Determine the audio MIME type from an explicit flag or the extension.

    Falls back to ``audio/mpeg``, which is what the gateway assumes for most
    compressed formats.
    """
    if override:
        return override

    suffix = Path(path).suffix.lower()
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
        ".aac": "audio/aac",
    }.get(suffix, "audio/mpeg")
