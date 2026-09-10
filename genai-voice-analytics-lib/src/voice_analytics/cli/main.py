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
import sys

from pydantic import ValidationError

from voice_analytics import __version__
from voice_analytics.cli import exit_codes, transcribe
from voice_analytics.cli._common import configure_logging
from voice_analytics.exceptions import (
    ConfigurationError,
    LLMAuthenticationError,
    LLMOutputParsingError,
    LLMRateLimitError,
    LLMServiceError,
    PromptNotFoundError,
    TranscriptionError,
    VoiceAnalyticsError,
)

logger = logging.getLogger("voice_analytics.cli")

#: Library exception -> process exit code. Order matters only in that each
#: entry must be a concrete class; lookup is by exact type then by isinstance.
_EXIT_CODE_BY_EXCEPTION: dict[type[BaseException], int] = {
    ConfigurationError: exit_codes.CONFIGURATION,
    PromptNotFoundError: exit_codes.CONFIGURATION,
    LLMAuthenticationError: exit_codes.AUTHENTICATION,
    LLMRateLimitError: exit_codes.RATE_LIMITED,
    LLMServiceError: exit_codes.UPSTREAM_UNAVAILABLE,
    TranscriptionError: exit_codes.PROCESSING_FAILED,
    LLMOutputParsingError: exit_codes.PROCESSING_FAILED,
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

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    transcribe.add_parser(subparsers)
    # Later libraries register here: summarize.add_parser(subparsers), etc.

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

    configure_logging(verbose=args.verbose)

    try:
        return args.handler(args)

    except ValidationError as exc:
        # Pydantic rejected the environment, e.g. LLM_BASE_URL is missing.
        logger.error("event=configuration_invalid detail=%s", exc)
        return exit_codes.CONFIGURATION

    except ValueError as exc:
        # Caller mistakes: unreadable file, unsupported language, wrong model.
        logger.error("event=invalid_input detail=%s", exc)
        return exit_codes.INVALID_INPUT

    except VoiceAnalyticsError as exc:
        code = _exit_code_for(exc)
        logger.error(
            "event=command_failed command=%s error=%s exit_code=%d detail=%s",
            args.command, type(exc).__name__, code, exc.message,
        )
        return code

    except KeyboardInterrupt:
        logger.warning("event=interrupted command=%s", args.command)
        return exit_codes.INTERRUPTED

    except Exception:  # noqa: BLE001 - the process boundary must not leak a traceback
        logger.exception("event=unexpected_error command=%s", args.command)
        return exit_codes.UNEXPECTED
