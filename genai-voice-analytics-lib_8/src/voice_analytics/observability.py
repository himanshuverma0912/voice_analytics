"""Logging built to be read by a person.

These libraries run as Airflow ``KubernetesPodOperator`` tasks, and the pod's
output becomes the Airflow task log. Whoever opens that log is often not the
person who wrote the code -- an operator, a support engineer, a business
analyst checking why a batch failed. So the default output is plain English
sentences, not machine syntax.

Two rules keep it short:

* **Log an outcome, not a function call.** A step that takes under a second and
  cannot meaningfully fail on its own does not deserve a line.
* **Log before anything slow**, so a pause in the log is explained rather than
  looking like a hang.

A successful transcription produces about eight lines. Pass ``--log-format json``
for one structured object per line when feeding a log aggregator.

Nothing here logs a secret. Use :func:`mask_secret` for anything credential-like.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar

#: Correlates every line emitted during one command invocation.
_run_id: ContextVar[str] = ContextVar("run_id", default="-")


def new_run_id() -> str:
    """Generate and install a run id for this execution context."""
    run_id = uuid.uuid4().hex[:12]
    _run_id.set(run_id)
    return run_id


def get_run_id() -> str:
    """The current run id, or ``"-"`` when none has been set."""
    return _run_id.get()


def mask_secret(value: str | None) -> str:
    """Render a credential safely: ``sk-abc...9999`` becomes ``sk-****9999``.

    Enough to confirm *which* key is in play without disclosing it.
    """
    if not value:
        return "<missing>"
    if len(value) < 8:
        # Too short to reveal a suffix without disclosing most of the value.
        return "****"
    prefix = "sk-" if value.startswith("sk-") else ""
    return f"{prefix}****{value[-4:]}"


def human_bytes(count: int) -> str:
    """``2646060`` becomes ``"2.5 MB"``. Nobody reads raw byte counts."""
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def human_duration(milliseconds: float) -> str:
    """``8432.1`` becomes ``"8.4s"``; short durations stay in milliseconds."""
    if milliseconds < 1000:
        return f"{milliseconds:.0f}ms"
    seconds = milliseconds / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {seconds % 60:.0f}s"


#: Field names whose values are masked wherever they appear in detail.
_SENSITIVE_FIELDS = frozenset(
    {"api_key", "key", "token", "password", "secret", "credential", "authorization"}
)


def say(
    logger: logging.Logger,
    message: str,
    *args: object,
    level: int = logging.INFO,
    **detail: object,
) -> None:
    """Log one line that serves both a person and a developer.

    ``message`` is a plain-English sentence, ``%``-formatted with ``args``.
    ``detail`` holds the technical context, appended in brackets::

        say(logger, "Read input file %s (%s).", name, size,
            path="/data/call.wav", bytes=2646060)

    renders in text mode as::

        Read input file call.wav (2.5 MB). [path=/data/call.wav bytes=2646060]

    and in JSON mode puts ``detail`` in its own object, so an aggregator gets
    properly separated fields rather than a string it has to parse.

    A reader who wants the gist stops at the full stop; a developer reads on.
    """
    logger.log(level, message, *args, extra={"detail": detail} if detail else {})


def _render_detail(detail: dict) -> str:
    """Format detail fields as ``key=value``, masking anything credential-like."""
    parts = []
    for key, value in detail.items():
        if key.lower() in _SENSITIVE_FIELDS:
            rendered = mask_secret(str(value) if value is not None else None)
        elif value is None:
            rendered = "none"
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, float):
            rendered = f"{value:.1f}"
        elif isinstance(value, (list, tuple, set)):
            rendered = ",".join(str(item) for item in value) or "none"
        else:
            text = str(value)
            rendered = f'"{text}"' if " " in text else text
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


def _safe_detail(detail: dict) -> dict:
    """Mask credential-like values, keeping everything else as typed data."""
    return {
        key: (
            mask_secret(str(value) if value is not None else None)
            if key.lower() in _SENSITIVE_FIELDS
            else value
        )
        for key, value in detail.items()
    }


class RunIdFilter(logging.Filter):
    """Attach the current run id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = get_run_id()
        return True


class HumanFormatter(logging.Formatter):
    """A readable sentence, with technical detail appended in brackets."""

    show_detail: bool = True

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        detail = getattr(record, "detail", None)
        if detail and self.show_detail:
            line = f"{line}  [{_render_detail(detail)}]"
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log aggregation systems."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", "-"),
            "message": record.getMessage(),
        }
        detail = getattr(record, "detail", None)
        if detail:
            # Real fields, not a string to re-parse.
            payload["detail"] = _safe_detail(detail)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    verbose: bool = False,
    log_format: str = "text",
    show_detail: bool = True,
) -> None:
    """Send logs to stderr, leaving stdout free for results.

    Args:
        verbose: Enable DEBUG, including httpx network tracing. For diagnosis
            only -- it is far too noisy for normal running.
        log_format: ``"text"`` for readable sentences with technical detail in
            brackets (the default, and what belongs in the Airflow UI), or
            ``"json"`` for log aggregation.
        show_detail: Include the bracketed technical detail. Set false for a
            pure plain-English view, e.g. a log shown to a business audience.
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.addFilter(RunIdFilter())

    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        # No logger name in the default format: it means nothing to a reader
        # who is not looking at the source. Available in JSON mode when needed.
        formatter = HumanFormatter(
            fmt="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
        )
        formatter.show_detail = show_detail
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # These log request headers at DEBUG, which would disclose the API key.
    for noisy in ("httpx", "httpcore", "openai", "asyncio"):
        logging.getLogger(noisy).setLevel(
            logging.DEBUG if verbose else logging.WARNING
        )
