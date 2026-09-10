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
    assert "event=transcribe_started" in captured.err


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
    assert "event=unexpected_error" in capsys.readouterr().err


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
        exit_codes.INTERRUPTED,
    ]
    assert len(codes) == len(set(codes))
