"""Command dispatcher.

This is the public interface of the container image. Its subcommands and flags
are a versioned contract -- an Airflow DAG that pins
``image: voice-analytics:0.1.0`` depends on them.

Every failure is mapped to a specific exit code so an orchestrator can tell a
misconfiguration apart from a transient outage without parsing logs.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
import time

from pydantic import ValidationError

from voice_analytics import __version__
from voice_analytics.cli import (
    analyse,
    build_prompt,
    exit_codes,
    extract,
    summarize,
    transcribe,
    transfer,
)
from voice_analytics.observability import (
    configure_logging,
    human_duration,
    new_run_id,
    say,
)
from voice_analytics.exceptions import (
    AnalysisError,
    AnonymizationError,
    ConfigurationError,
    ExtractionError,
    LLMAuthenticationError,
    LLMOutputParsingError,
    LLMRateLimitError,
    LLMServiceError,
    PromptHubError,
    PromptNotFoundError,
    SummarizationError,
    TranscriptionError,
    TransferError,
    VoiceAnalyticsError,
)

logger = logging.getLogger("voice_analytics.cli")

#: Library exception -> process exit code. Order matters only in that each
#: entry must be a concrete class; lookup is by exact type then by isinstance.
_EXIT_CODE_BY_EXCEPTION: dict[type[BaseException], int] = {
    ConfigurationError: exit_codes.CONFIGURATION,
    PromptNotFoundError: exit_codes.CONFIGURATION,
    PromptHubError: exit_codes.CONFIGURATION,
    LLMAuthenticationError: exit_codes.AUTHENTICATION,
    LLMRateLimitError: exit_codes.RATE_LIMITED,
    LLMServiceError: exit_codes.UPSTREAM_UNAVAILABLE,
    TranscriptionError: exit_codes.PROCESSING_FAILED,
    SummarizationError: exit_codes.PROCESSING_FAILED,
    LLMOutputParsingError: exit_codes.PROCESSING_FAILED,
    AnalysisError: exit_codes.PROCESSING_FAILED,
    ExtractionError: exit_codes.PROCESSING_FAILED,
    AnonymizationError: exit_codes.ANONYMIZATION_FAILED,
    TransferError: exit_codes.UPSTREAM_UNAVAILABLE,
}


def build_parser() -> argparse.ArgumentParser:
    """Assemble the top-level parser and every subcommand."""
    parser = argparse.ArgumentParser(
        prog="voice-analytics",
        description=(
            "Voice analytics worker commands. Configuration and credentials are "
            "read from the environment; see env.sample."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help=(
            "Hide the bracketed technical detail, leaving only plain-English "
            "sentences. For a log shown to a non-technical audience."
        ),
    )
    parser.add_argument(
        "--log-format",
        choices=("text", "json"),
        default="text",
        help=(
            "Log output format. 'text' is key=value, which reads well in the "
            "Airflow UI. 'json' emits one object per line for log aggregation."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    transfer.add_parser(subparsers)
    transcribe.add_parser(subparsers)
    summarize.add_parser(subparsers)
    build_prompt.add_parser(subparsers)
    analyse.add_parser(subparsers)
    extract.add_parser(subparsers)
    # Later libraries register their subcommand here.

    return parser


def _exit_code_for(exc: Exception) -> int:
    """Map an exception to its exit code, honouring subclasses."""
    specific = _EXIT_CODE_BY_EXCEPTION.get(type(exc))
    if specific is not None:
        return specific
    for exc_type, code in _EXIT_CODE_BY_EXCEPTION.items():
        if isinstance(exc, exc_type):
            return code
    return exit_codes.UNEXPECTED


def main(argv: list[str] | None = None) -> int:
    """Run a command and return its exit code.

    Returns rather than raising so this is directly testable; ``__main__``
    passes the result to :func:`sys.exit`.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help(sys.stderr)
        return exit_codes.USAGE

    configure_logging(
        verbose=args.verbose,
        log_format=args.log_format,
        show_detail=not args.plain,
    )

    run_id = new_run_id()
    started = time.perf_counter()

    say(logger,
        "Starting '%s' (voice-analytics %s, run %s, host %s).",
        args.command, __version__, run_id, platform.node(),
        command=args.command, version=__version__, run_id=run_id,
        python=platform.python_version(), host=platform.node(), pid=os.getpid(),
    )

    code = exit_codes.UNEXPECTED
    try:
        code = args.handler(args)
        return code

    except ValidationError as exc:
        # Pydantic rejected the environment, e.g. LLM_BASE_URL is missing.
        code = exit_codes.CONFIGURATION
        missing = ", ".join(
            ".".join(str(part) for part in err.get("loc", ()))
            for err in exc.errors()
        )
        say(logger,
            "Configuration problem: these settings are missing or invalid: %s. "
            "See env.sample for what each one means.", missing,
            level=logging.ERROR, missing=missing, exit_code=code,
        )
        return code

    except ValueError as exc:
        # Caller mistakes: unreadable file, unsupported language, wrong model.
        code = exit_codes.INVALID_INPUT
        say(logger, "Cannot continue: %s", exc,
            level=logging.ERROR, error_type=type(exc).__name__, exit_code=code)
        return code

    except VoiceAnalyticsError as exc:
        code = _exit_code_for(exc)
        say(logger, "Failed: %s", exc.message,
            level=logging.ERROR, error_type=type(exc).__name__, exit_code=code,
            **(exc.details or {}))
        return code

    except KeyboardInterrupt:
        code = exit_codes.INTERRUPTED
        say(logger, "Stopped before finishing (interrupted).",
            level=logging.WARNING, exit_code=code)
        return code

    except Exception as exc:  # noqa: BLE001 - the process boundary must not leak a traceback
        code = exit_codes.UNEXPECTED
        # exc_info here: an unexpected error is the one case where the full
        # traceback is worth more than a tidy line.
        logger.exception(
            "Unexpected error (%s): %s. The full technical details follow.",
            type(exc).__name__, exc,
        )
        return code

    finally:
        elapsed = human_duration((time.perf_counter() - started) * 1000)
        if code == exit_codes.SUCCESS:
            say(logger, "Finished successfully in %s.", elapsed,
                command=args.command, exit_code=code,
                total_elapsed_ms=(time.perf_counter() - started) * 1000)
        else:
            say(logger, "Finished with errors after %s (exit code %d).", elapsed, code,
                level=logging.ERROR, command=args.command, exit_code=code,
                total_elapsed_ms=(time.perf_counter() - started) * 1000)
