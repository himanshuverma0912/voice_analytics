"""Logging helpers.

The behaviours tested here are the ones an operator depends on: secrets never
reach the log, and sizes and durations are rendered in units a person reads.
"""

from __future__ import annotations

import json
import logging

import pytest

from voice_analytics.observability import (
    JsonFormatter,
    RunIdFilter,
    get_run_id,
    human_bytes,
    human_duration,
    mask_secret,
    new_run_id,
)


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sk-abcdefgh1234", "sk-****1234"),
        ("plain-secret-9876", "****9876"),
        ("short", "****"),
        (None, "<missing>"),
        ("", "<missing>"),
    ],
)
def test_mask_secret(value, expected):
    assert mask_secret(value) == expected


def test_mask_secret_never_reveals_the_body():
    masked = mask_secret("sk-SUPERSECRETVALUE9999")
    assert "SUPERSECRET" not in masked
    assert masked.endswith("9999")      # enough to identify, not to use


# ---------------------------------------------------------------------------
# Human-readable units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0 bytes"),
        (512, "512 bytes"),
        (2048, "2.0 KB"),
        (2646060, "2.5 MB"),
        (3 * 1024**3, "3.0 GB"),
    ],
)
def test_human_bytes(count, expected):
    assert human_bytes(count) == expected


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (42.0, "42ms"),
        (999.0, "999ms"),
        (1500.0, "1.5s"),
        (8432.1, "8.4s"),
        (65000.0, "1m 5s"),
    ],
)
def test_human_duration(milliseconds, expected):
    assert human_duration(milliseconds) == expected


# ---------------------------------------------------------------------------
# Run correlation
# ---------------------------------------------------------------------------


def test_run_id_is_generated_and_readable():
    run_id = new_run_id()
    assert len(run_id) == 12
    assert get_run_id() == run_id


def test_run_id_filter_attaches_the_id_to_records():
    new_run_id()
    record = logging.LogRecord("t", logging.INFO, "f", 1, "msg", None, None)
    assert RunIdFilter().filter(record) is True
    assert record.run_id == get_run_id()


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_json_formatter_emits_one_parseable_object():
    record = logging.LogRecord(
        "voice_analytics", logging.INFO, "f", 1, "Read input file x.wav", None, None
    )
    record.run_id = "abc123"

    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "voice_analytics"
    assert payload["run_id"] == "abc123"
    assert payload["message"] == "Read input file x.wav"


# ---------------------------------------------------------------------------
# say() -- one line for two audiences
# ---------------------------------------------------------------------------


def _render(record: logging.LogRecord, show_detail: bool = True) -> str:
    from voice_analytics.observability import HumanFormatter

    formatter = HumanFormatter(fmt="%(message)s")
    formatter.show_detail = show_detail
    return formatter.format(record)


@pytest.fixture
def captured(caplog):
    caplog.set_level(logging.DEBUG, logger="test")
    return caplog


def test_say_renders_sentence_then_detail(captured):
    from voice_analytics.observability import say

    say(logging.getLogger("test"), "Read input file %s (%s).", "call.wav", "2.5 MB",
        path="/tmp/call.wav", bytes=2646060)

    assert _render(captured.records[-1]) == (
        "Read input file call.wav (2.5 MB).  [path=/tmp/call.wav bytes=2646060]"
    )


def test_plain_mode_drops_the_detail(captured):
    """A business audience sees the sentence only."""
    from voice_analytics.observability import say

    say(logging.getLogger("test"), "Read input file %s.", "call.wav", bytes=123)

    assert _render(captured.records[-1], show_detail=False) == "Read input file call.wav."


def test_message_alone_is_readable_without_the_detail(captured):
    """The sentence must stand on its own -- detail is additive, not required."""
    from voice_analytics.observability import say

    say(logging.getLogger("test"), "AI service responded in %s.", "8.4s",
        attempt=1, elapsed_ms=8432.1)

    assert captured.records[-1].getMessage() == "AI service responded in 8.4s."


def test_say_without_detail_adds_no_brackets(captured):
    from voice_analytics.observability import say

    say(logging.getLogger("test"), "Nothing to add here.")
    assert _render(captured.records[-1]) == "Nothing to add here."


@pytest.mark.parametrize("field", ["api_key", "token", "password", "AUTHORIZATION"])
def test_detail_fields_are_masked_in_text_mode(captured, field):
    from voice_analytics.observability import say

    say(logging.getLogger("test"), "Connecting.", **{field: "sk-supersecret9999"})

    line = _render(captured.records[-1])
    assert "supersecret" not in line
    assert "9999" in line


def test_detail_fields_are_masked_in_json_mode(captured):
    from voice_analytics.observability import say

    say(logging.getLogger("test"), "Connecting.", api_key="sk-supersecret9999",
        gateway="https://llm.example/v1")

    payload = json.loads(JsonFormatter().format(captured.records[-1]))
    assert "supersecret" not in payload["detail"]["api_key"]
    assert payload["detail"]["gateway"] == "https://llm.example/v1"


def test_json_detail_keeps_native_types(captured):
    """An aggregator gets numbers as numbers, not strings to re-parse."""
    from voice_analytics.observability import say

    say(logging.getLogger("test"), "Done.", segments=3, elapsed_ms=187.5, ok=True)

    detail = json.loads(JsonFormatter().format(captured.records[-1]))["detail"]
    assert detail == {"segments": 3, "elapsed_ms": 187.5, "ok": True}


@pytest.mark.parametrize(
    ("value", "rendered"),
    [(True, "true"), (False, "false"), (None, "none"),
     (1.25, "1.2"), (["a", "b"], "a,b"), ("has space", '"has space"')],
)
def test_detail_value_rendering(captured, value, rendered):
    from voice_analytics.observability import say

    say(logging.getLogger("test"), "x", field=value)
    assert _render(captured.records[-1]) == f"x  [field={rendered}]"
