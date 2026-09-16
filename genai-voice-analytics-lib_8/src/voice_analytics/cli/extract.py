"""``extract`` command: a transcript in, topics and the agent's name out.

Kept separate from ``analyse`` because these are separate gateway requests with
separate failure modes. Folding them into scoring would mean a failed topic
lookup could take a full KPI result down with it -- and the originating service
was explicit that it must not: both extractions there sit in their own
try/except and leave the field empty on failure.

That behaviour is preserved here. A failure writes ``null`` for the field and
still exits 0, so an orchestrator records the call without them rather than
retrying a whole transcript for an optional value. ``--strict`` turns that off
for callers that would rather know.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from voice_analytics.cli import exit_codes
from voice_analytics.cli._common import read_text, write_json
from voice_analytics.cli.analyse import _extract_transcript
from voice_analytics.config import load_settings
from voice_analytics.exceptions import VoiceAnalyticsError
from voice_analytics.extraction import extract_agent_name, extract_topics
from voice_analytics.llm import build_llm_client, close_llm_clients
from voice_analytics.observability import say

logger = logging.getLogger("voice_analytics.cli.extract")


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``extract`` subcommand."""
    parser = subparsers.add_parser(
        "extract",
        help="Extract the call's topics and the agent's name from a transcript.",
        description=(
            "Read a transcript and report what the call was about and who the "
            "agent was. Both are best-effort: a call where the agent never "
            "says their name simply has no agent name."
        ),
    )
    parser.add_argument(
        "--input", required=True, metavar="PATH",
        help="Transcript to read. Plain text, or the JSON written by 'transcribe'.",
    )
    parser.add_argument(
        "--output", default="-", metavar="PATH",
        help="Where to write the JSON result. '-' means stdout (the default).",
    )
    parser.add_argument(
        "--model", default=None, metavar="NAME",
        help=(
            "Model to use. Defaults to ANALYSIS_MODEL_NAME from the "
            "environment -- not MODEL_NAME, which is the transcription model."
        ),
    )
    parser.add_argument(
        "--topics", action="store_true",
        help="Extract topics. Both are extracted when neither flag is given.",
    )
    parser.add_argument(
        "--agent-name", action="store_true",
        help="Extract the agent's name. Both are extracted when neither flag is given.",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help=(
            "Fail the command if an extraction fails. Without it a failure "
            "writes null for that field and still exits 0, matching the "
            "originating service -- an optional value is not worth failing a "
            "call over."
        ),
    )
    parser.set_defaults(handler=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Entry point invoked by the dispatcher. Returns a process exit code."""
    return asyncio.run(_run_async(args))


async def _run_async(args: argparse.Namespace) -> int:
    transcript = _extract_transcript(read_text(args.input), args.input)
    settings = load_settings()              # ValidationError -> CONFIGURATION
    model_name = args.model or settings.ANALYSIS_MODEL_NAME

    # Neither flag means both, which is what the pipeline wants. Naming one
    # narrows it, for a caller that needs only that field.
    want_topics = args.topics or not (args.topics or args.agent_name)
    want_agent = args.agent_name or not (args.topics or args.agent_name)

    client = build_llm_client(settings)
    payload: dict[str, object] = {"topics": None, "agent_name": None, "failed": []}

    try:
        if want_topics:
            payload["topics"] = await _attempt(
                "topics", extract_topics(client, transcript, model_name),
                args.strict, payload,
            )
        if want_agent:
            payload["agent_name"] = await _attempt(
                "agent_name", extract_agent_name(client, transcript, model_name),
                args.strict, payload,
            )
    finally:
        # Always release sockets, even when an extraction above failed.
        await close_llm_clients()

    topics = payload["topics"]
    say(logger,
        "This call was about %s. Agent: %s.",
        ", ".join(topics) if topics else "nothing that could be identified",
        payload["agent_name"] or "not stated",
        topics=topics, agent_name=payload["agent_name"],
        failed=payload["failed"] or None,
    )

    write_json(payload, args.output)
    return exit_codes.SUCCESS


async def _attempt(field: str, coro, strict: bool, payload: dict):
    """Run one extraction, tolerating failure unless ``--strict``."""
    try:
        return await coro
    except (VoiceAnalyticsError, ValueError) as exc:
        if strict:
            raise
        say(logger,
            "Could not work out the %s for this call (%s). Continuing without "
            "it -- the rest of the call's results are unaffected.",
            field.replace("_", " "), type(exc).__name__,
            level=logging.WARNING, field=field, error=str(exc)[:200],
        )
        payload["failed"].append(field)
        return None
