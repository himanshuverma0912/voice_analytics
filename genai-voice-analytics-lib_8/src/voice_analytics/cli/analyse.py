"""``analyse`` command: a transcript plus a prompt in, KPI scores out.

The prompt is supplied as a file rather than resolved here. A batch resolves it
once from PromptHub and reuses it for every call -- resolving per pod would mean
one registry request per file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from voice_analytics.analysis import analyse
from voice_analytics.cli import exit_codes
from voice_analytics.cli._common import read_text, write_json
from voice_analytics.config import load_settings
from voice_analytics.llm import build_llm_client, close_llm_clients
from voice_analytics.observability import say
from voice_analytics.transcription import extract_duration_sec

logger = logging.getLogger("voice_analytics.cli.analyse")


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``analyse`` subcommand."""
    parser = subparsers.add_parser(
        "analyse",
        help="Score a transcript against KPIs.",
        description=(
            "Score a call transcript against the KPIs baked into the supplied "
            "prompt. Build that prompt once per batch with "
            "voice_analytics.prompthub, then reuse it for every call. "
            "Credentials are read from the environment."
        ),
    )
    parser.add_argument(
        "--input", required=True, metavar="PATH",
        help=(
            "Transcript to score. Either plain text, or the JSON written by "
            "'transcribe', in which case its 'transcript' field is used. Use "
            "the source-language transcript -- a translation is for reviewers, "
            "not for scoring."
        ),
    )
    parser.add_argument(
        "--prompt", required=True, metavar="PATH",
        help="File holding the assembled scoring prompt (base plus KPI blocks).",
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
        "--agent-name", default=None, metavar="NAME",
        help="Agent on the call, so KPIs can refer to them by name.",
    )
    parser.set_defaults(handler=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Entry point invoked by the dispatcher. Returns a process exit code."""
    return asyncio.run(_run_async(args))


def _extract_transcript(raw: str, path: str) -> str:
    """Accept either plain text or the JSON that ``transcribe`` writes.

    Taking both means an orchestrator can hand one pod's output straight to the
    next without unpacking it in between -- and without needing this library
    installed to do the unpacking.

    Raises:
        ValueError: The file is JSON from ``transcribe`` but its transcript is
            empty. That is a failed transcription, not something to score.
    """
    stripped = raw.lstrip()
    if not stripped.startswith("{"):
        return raw

    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        # It looked like JSON and was not. Treat it as text rather than
        # refusing -- a transcript may legitimately open with a brace.
        return raw

    if not isinstance(document, dict) or "transcript" not in document:
        return raw

    transcript = document.get("transcript") or ""
    if not transcript.strip():
        say(logger,
            "%s holds a transcription result with an empty transcript, so "
            "there is nothing to score. The call may have been silent or the "
            "transcription may have failed.",
            path, level=logging.ERROR, path=path, exit_code=4,
        )
        raise ValueError(f"{path} contains no transcript to score")

    say(logger,
        "Read the transcript out of the transcription result (%d characters).",
        len(transcript), path=path, chars=len(transcript),
        language=document.get("primary_language"),
    )
    return transcript


async def _run_async(args: argparse.Namespace) -> int:
    transcript = _extract_transcript(read_text(args.input), args.input)
    prompt = read_text(args.prompt)
    settings = load_settings()                  # ValidationError -> CONFIGURATION
    model_name = args.model or settings.ANALYSIS_MODEL_NAME

    say(logger,
        "Connecting to the AI service at %s (TLS verification %s).",
        settings.LLM_BASE_URL,
        "on" if settings.ssl_verify() is not False else "off",
        gateway=settings.LLM_BASE_URL, model=model_name,
        ssl_verify=settings.SSL_VERIFY,
        ssl_ca_bundle=settings.SSL_CA_BUNDLE or "none",
    )

    client = build_llm_client(settings)
    try:
        result = await analyse(
            client=client,
            transcript=transcript,
            consolidated_prompt=prompt,
            model_name=model_name,
            agent_name=args.agent_name,
        )
    finally:
        # Always release sockets, even when the call above failed.
        await close_llm_clients()

    for rollup in result.by_objective:
        say(logger,
            "%s: %d of %d KPIs scored%s.",
            rollup.objective, rollup.scored, rollup.total,
            f", average {rollup.average}" if rollup.average is not None else "",
            objective=rollup.objective, scored=rollup.scored, total=rollup.total,
            average=rollup.average, unevidenced=rollup.unevidenced,
        )

    payload = result.model_dump(mode="json")

    # Derived here rather than at transcription time because that is where the
    # originating service put it: the value comes from the last timestamp in
    # the transcript, not from the audio. Emitting it in this envelope rather
    # than on AnalysisResult keeps it out of the library's public model while
    # still giving a caller everything one call's row needs -- without the
    # caller having to import this library to compute it.
    payload["duration_sec"] = extract_duration_sec(transcript)

    write_json(payload, args.output)
    return exit_codes.SUCCESS
