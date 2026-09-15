"""CLI behaviour, with emphasis on the exit-code contract.

Exit codes are what an Airflow KubernetesPodOperator acts on, so they are
tested as a public interface rather than an implementation detail.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from voice_analytics.cli import exit_codes
from voice_analytics.cli.main import main
from voice_analytics.exceptions import (
    AnonymizationError,
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMServiceError,
    PromptNotFoundError,
    TranscriptionError,
)
from voice_analytics.schemas import TranscriptionResult, TranscriptSegment

ENV = {
    "LLM_BASE_URL": "https://llm.example/v1",
    "LLM_API_KEY": "sk-test",
    "MODEL_NAME": "gemini-2.5-flash-transcription",
}


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "call.wav"
    path.write_bytes(b"RIFF-fake-audio")
    return path


@pytest.fixture
def env(monkeypatch):
    """A valid environment, with any inherited overrides cleared."""
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "MODEL_NAME", "TRANSCRIPTION_MODEL_NAMES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    # load_settings is cached; clear it so each test sees its own environment.
    from voice_analytics.config import load_settings

    load_settings.cache_clear()
    yield
    load_settings.cache_clear()


def _result(transcript: str = "[00:01] Namaste") -> TranscriptionResult:
    return TranscriptionResult(
        segments=[TranscriptSegment(timestamp="00:01", text="Namaste")],
        primary_language="Hindi",
        transcript=transcript,
        processing_time_ms=42.0,
    )


def _patch_transcribe(result_or_exc):
    """Patch the library call the command delegates to."""
    kwargs = (
        {"side_effect": result_or_exc}
        if isinstance(result_or_exc, Exception)
        else {"return_value": result_or_exc}
    )
    return patch(
        "voice_analytics.cli.transcribe.transcribe", AsyncMock(**kwargs)
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_no_command_prints_help_and_exits_usage(capsys):
    assert main([]) == exit_codes.USAGE
    assert "COMMAND" in capsys.readouterr().err


def test_version_flag_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit) as excinfo:
        main(["nonsense"])
    assert excinfo.value.code == exit_codes.USAGE


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


def test_transcribe_writes_json_to_a_file(env, audio_file, tmp_path):
    out = tmp_path / "out" / "result.json"  # nested: parent must be created
    with _patch_transcribe(_result()):
        code = main(["transcribe", "--input", str(audio_file), "--output", str(out)])

    assert code == exit_codes.SUCCESS
    payload = json.loads(out.read_text())
    assert payload["transcript"] == "[00:01] Namaste"
    assert payload["primary_language"] == "Hindi"


def test_transcribe_writes_to_stdout_by_default(env, audio_file, capsys):
    with _patch_transcribe(_result()):
        code = main(["transcribe", "--input", str(audio_file)])

    assert code == exit_codes.SUCCESS
    assert json.loads(capsys.readouterr().out)["transcript"] == "[00:01] Namaste"


def test_logs_go_to_stderr_leaving_stdout_pipeable(env, audio_file, capsys):
    with _patch_transcribe(_result()):
        main(["transcribe", "--input", str(audio_file)])

    captured = capsys.readouterr()
    json.loads(captured.out)  # stdout must be valid JSON on its own
    assert "Read input file" in captured.err
    assert "Connecting to the AI service" in captured.err


def test_transcribe_forwards_options_to_the_library(env, audio_file):
    with _patch_transcribe(_result()) as mocked:
        main([
            "transcribe", "--input", str(audio_file),
            "--target-lang", "hi", "--romanize",
            "--user-instruction", "Capitalise IMPS and NEFT",
        ])

    kwargs = mocked.call_args.kwargs
    assert kwargs["target_lang"] == "hi"
    assert kwargs["romanize"] is True
    assert kwargs["user_instruction"] == "Capitalise IMPS and NEFT"
    assert kwargs["mime_type"] == "audio/wav"  # inferred from the extension


# ---------------------------------------------------------------------------
# Exit-code contract
# ---------------------------------------------------------------------------


def test_missing_input_file_is_invalid_input(env, tmp_path):
    assert main(["transcribe", "--input", str(tmp_path / "absent.wav")]) == (
        exit_codes.INVALID_INPUT
    )


def test_missing_required_configuration_is_configuration_error(
    monkeypatch, audio_file
):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    from voice_analytics.config import load_settings

    load_settings.cache_clear()
    try:
        # env_file=None keeps a stray .env in the repo from rescuing the call.
        with patch("voice_analytics.cli.transcribe.load_settings") as mocked:
            from pydantic import ValidationError

            from voice_analytics.config import Settings

            def _raise(*_args, **_kwargs):
                Settings(_env_file=None)  # raises ValidationError

            mocked.side_effect = _raise
            code = main(["transcribe", "--input", str(audio_file)])
        assert code == exit_codes.CONFIGURATION
    finally:
        load_settings.cache_clear()


def test_non_audio_model_is_rejected_before_calling_the_gateway(
    env, monkeypatch, audio_file
):
    monkeypatch.setenv("TRANSCRIPTION_MODEL_NAMES", "only-this-one")
    from voice_analytics.config import load_settings

    load_settings.cache_clear()

    with _patch_transcribe(_result()) as mocked:
        code = main(["transcribe", "--input", str(audio_file), "--model", "text-only"])

    assert code == exit_codes.INVALID_INPUT
    mocked.assert_not_awaited()


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (LLMAuthenticationError("bad key"), exit_codes.AUTHENTICATION),
        (LLMRateLimitError("slow down"), exit_codes.RATE_LIMITED),
        (LLMServiceError("gateway down"), exit_codes.UPSTREAM_UNAVAILABLE),
        (TranscriptionError("unparseable"), exit_codes.PROCESSING_FAILED),
        (PromptNotFoundError("no prompt"), exit_codes.CONFIGURATION),
        (AnonymizationError("could not redact"), exit_codes.ANONYMIZATION_FAILED),
    ],
)
def test_library_errors_map_to_distinct_exit_codes(
    env, audio_file, exception, expected_code
):
    with _patch_transcribe(exception):
        assert main(["transcribe", "--input", str(audio_file)]) == expected_code


def test_unexpected_error_exits_one_without_leaking_a_traceback(
    env, audio_file, capsys
):
    with _patch_transcribe(RuntimeError("something odd")):
        code = main(["transcribe", "--input", str(audio_file)])

    assert code == exit_codes.UNEXPECTED
    assert "Unexpected error" in capsys.readouterr().err


def test_empty_transcript_fails_the_task(env, audio_file, tmp_path):
    """Silent audio or a wrong file should fail, not silently succeed."""
    out = tmp_path / "empty.json"
    with _patch_transcribe(_result(transcript="")):
        code = main(["transcribe", "--input", str(audio_file), "--output", str(out)])

    assert code == exit_codes.PROCESSING_FAILED
    assert out.exists()  # the result is still written, for diagnosis


def test_exit_codes_are_distinct():
    """Renumbering these would silently break every DAG that branches on them."""
    codes = [
        exit_codes.SUCCESS, exit_codes.UNEXPECTED, exit_codes.USAGE,
        exit_codes.CONFIGURATION, exit_codes.INVALID_INPUT,
        exit_codes.AUTHENTICATION, exit_codes.RATE_LIMITED,
        exit_codes.UPSTREAM_UNAVAILABLE, exit_codes.PROCESSING_FAILED,
        exit_codes.ANONYMIZATION_FAILED, exit_codes.INTERRUPTED,
    ]
    assert len(codes) == len(set(codes))


# ---------------------------------------------------------------------------
# analyse --input accepts transcribe's JSON as well as plain text
# ---------------------------------------------------------------------------

def test_extract_transcript_passes_plain_text_through():
    from voice_analytics.cli.analyse import _extract_transcript

    text = "[00:01] Agent: Hello.\n[00:04] Customer: Hi."
    assert _extract_transcript(text, "call.txt") == text


def test_extract_transcript_reads_the_field_from_transcribe_output():
    from voice_analytics.cli.analyse import _extract_transcript

    payload = json.dumps(
        {"transcript": "[00:01] Agent: Hello.", "primary_language": "Hindi"}
    )
    assert _extract_transcript(payload, "call.json") == "[00:01] Agent: Hello."


def test_extract_transcript_rejects_an_empty_transcription_result():
    from voice_analytics.cli.analyse import _extract_transcript

    payload = json.dumps({"transcript": "   ", "primary_language": None})
    with pytest.raises(ValueError, match="no transcript"):
        _extract_transcript(payload, "silent.json")


def test_extract_transcript_keeps_text_that_merely_starts_with_a_brace():
    """A transcript is not disqualified by its first character."""
    from voice_analytics.cli.analyse import _extract_transcript

    text = "{not json at all} the agent said hello"
    assert _extract_transcript(text, "odd.txt") == text


def test_extract_transcript_keeps_json_that_is_not_a_transcription_result():
    from voice_analytics.cli.analyse import _extract_transcript

    payload = json.dumps({"something": "else"})
    assert _extract_transcript(payload, "other.json") == payload


# ---------------------------------------------------------------------------
# build-prompt
# ---------------------------------------------------------------------------

def test_parse_codes_keeps_order_and_drops_blanks_and_duplicates():
    from voice_analytics.cli.build_prompt import _parse_codes

    assert _parse_codes(" b , a ,, b , c ") == ["b", "a", "c"]


def test_parse_codes_returns_nothing_for_an_empty_list():
    from voice_analytics.cli.build_prompt import _parse_codes

    assert _parse_codes(" , , ") == []


def test_build_prompt_is_registered_as_a_subcommand():
    from voice_analytics.cli.main import build_parser

    args = build_parser().parse_args(
        ["build-prompt", "--kpi-codes", "a,b", "--output", "/tmp/p.txt"]
    )
    assert args.command == "build-prompt"
    assert args.kpi_codes == "a,b"
    assert args.report is None


# ---------------------------------------------------------------------------
# extract: failure of an optional field must not fail the call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_tolerates_a_failed_extraction_and_records_it():
    from voice_analytics.cli.extract import _attempt
    from voice_analytics.exceptions import LLMServiceError

    async def boom():
        raise LLMServiceError("gateway down")

    payload = {"failed": []}
    assert await _attempt("topics", boom(), strict=False, payload=payload) is None
    assert payload["failed"] == ["topics"]


@pytest.mark.asyncio
async def test_extract_strict_lets_the_failure_through():
    from voice_analytics.cli.extract import _attempt
    from voice_analytics.exceptions import LLMServiceError

    async def boom():
        raise LLMServiceError("gateway down")

    with pytest.raises(LLMServiceError):
        await _attempt("topics", boom(), strict=True, payload={"failed": []})


@pytest.mark.asyncio
async def test_extract_passes_a_successful_value_through_untouched():
    from voice_analytics.cli.extract import _attempt

    async def ok():
        return ["credit limit"]

    payload = {"failed": []}
    assert await _attempt("topics", ok(), strict=False, payload=payload) == ["credit limit"]
    assert payload["failed"] == []


@pytest.mark.parametrize(
    ("argv", "topics", "agent"),
    [
        ([], True, True),                      # neither flag means both
        (["--topics"], True, False),
        (["--agent-name"], False, True),
        (["--topics", "--agent-name"], True, True),
    ],
)
def test_extract_flag_defaulting(argv, topics, agent):
    from voice_analytics.cli.main import build_parser

    args = build_parser().parse_args(["extract", "--input", "c.txt", *argv])
    want_topics = args.topics or not (args.topics or args.agent_name)
    want_agent = args.agent_name or not (args.topics or args.agent_name)
    assert (want_topics, want_agent) == (topics, agent)


# ---------------------------------------------------------------------------
# build-prompt --from-file
# ---------------------------------------------------------------------------
# A local snapshot is for a machine that cannot reach PromptHub. It must go
# through the same assembly as the registry path, so a prompt built either way
# has the same structure -- only the text is a snapshot.

_SNAPSHOT = {
    "base_prompt": "You are a strict call-audit engine. Return only JSON.",
    "sections": [
        {"section_name": "Compliance", "kpis": [
            {"kpi_code": "authentication_compliance", "kpi_name": "Authentication",
             "prompt": "Did the agent authenticate the caller?", "order": 1},
            {"kpi_code": "regulatory_risk", "kpi_name": "Regulatory Risk",
             "prompt": "Was anything said that creates regulatory risk?", "order": 2},
        ]},
        {"section_name": "Experience", "kpis": [
            {"kpi_code": "empathy", "kpi_name": "Empathy",
             "prompt": "Did the agent acknowledge the customer's situation?", "order": 3},
        ]},
    ],
}


def _write_snapshot(tmp_path, document=None):
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(document if document is not None else _SNAPSHOT))
    return str(path)


def test_from_file_takes_every_kpi_when_no_codes_are_named(tmp_path):
    from voice_analytics.cli.build_prompt import _load_from_file

    base, resolved, skipped = _load_from_file(_write_snapshot(tmp_path), [])
    assert base.startswith("You are a strict")
    assert [k.kpi_code for k in resolved] == [
        "authentication_compliance", "regulatory_risk", "empathy"]
    assert skipped == []


def test_from_file_honours_the_order_the_codes_were_given_in(tmp_path):
    """The prompt presents KPIs in the caller's order, and a reordered prompt
    is a different prompt."""
    from voice_analytics.cli.build_prompt import _load_from_file

    _, resolved, _ = _load_from_file(
        _write_snapshot(tmp_path), ["empathy", "authentication_compliance"])
    assert [k.kpi_code for k in resolved] == ["empathy", "authentication_compliance"]


def test_from_file_reports_a_code_the_snapshot_does_not_have(tmp_path):
    from voice_analytics.cli.build_prompt import _load_from_file

    _, resolved, skipped = _load_from_file(
        _write_snapshot(tmp_path), ["empathy", "never_defined"])
    assert [k.kpi_code for k in resolved] == ["empathy"]
    assert skipped == ["never_defined"]


def test_from_file_falls_back_to_description_when_there_is_no_prompt(tmp_path):
    from voice_analytics.cli.build_prompt import _load_from_file

    path = _write_snapshot(tmp_path, {
        "base_prompt": "BASE",
        "sections": [{"kpis": [{"kpi_code": "a", "kpi_name": "A",
                                "description": "from the description field"}]}],
    })
    _, resolved, _ = _load_from_file(path, [])
    assert resolved[0].instructions == "from the description field"


def test_from_file_skips_entries_with_no_code_or_no_text(tmp_path):
    from voice_analytics.cli.build_prompt import _load_from_file

    path = _write_snapshot(tmp_path, {
        "base_prompt": "BASE",
        "sections": [{"kpis": [
            {"kpi_name": "no code", "prompt": "x"},
            {"kpi_code": "no_text"},
            {"kpi_code": "good", "kpi_name": "Good", "prompt": "y"},
        ]}],
    })
    _, resolved, _ = _load_from_file(path, [])
    assert [k.kpi_code for k in resolved] == ["good"]


def test_a_snapshot_without_a_base_prompt_is_refused(tmp_path):
    """Without it the model has no output format to follow, so the scores
    would be unusable rather than merely different."""
    from voice_analytics.cli.build_prompt import _load_from_file

    path = _write_snapshot(tmp_path, {"sections": _SNAPSHOT["sections"]})
    with pytest.raises(ValueError, match="no 'base_prompt'"):
        _load_from_file(path, [])


def test_a_snapshot_with_no_kpis_is_refused(tmp_path):
    from voice_analytics.cli.build_prompt import _load_from_file

    path = _write_snapshot(tmp_path, {"base_prompt": "BASE", "sections": []})
    with pytest.raises(ValueError, match="no KPI definitions"):
        _load_from_file(path, [])


def test_a_missing_snapshot_file_says_so(tmp_path):
    from voice_analytics.cli.build_prompt import _load_from_file

    with pytest.raises(ValueError, match="not found"):
        _load_from_file(str(tmp_path / "nope.json"), [])


def test_the_shipped_snapshot_assembles_into_a_usable_prompt():
    """The real file, through the real assembly, matched by the real parser."""
    from pathlib import Path

    from voice_analytics.analysis import build_consolidated_prompt
    from voice_analytics.cli.build_prompt import _load_from_file

    snapshot = (Path(__file__).resolve().parents[2]
                / "examples" / "prompts" / "analytics_prompts.json")
    base, resolved, skipped = _load_from_file(str(snapshot), [])

    assert len(resolved) == 20, "the snapshot should carry 20 KPIs"
    assert skipped == []

    prompt = build_consolidated_prompt(base, resolved)
    for code in ("overall_compliance_score", "empathy", "resolution_quality"):
        assert f"KPI Code : {code}" in prompt
