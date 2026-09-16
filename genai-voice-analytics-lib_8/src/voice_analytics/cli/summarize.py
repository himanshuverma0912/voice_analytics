"""``summarize`` command: a transcript or an audio file in, summaries out."""

from __future__ import annotations

import argparse
import asyncio
import logging

from voice_analytics.cli import exit_codes
from voice_analytics.cli._common import infer_mime_type, read_bytes, read_text, write_json
from voice_analytics.config import load_settings
from voice_analytics.observability import say
from voice_analytics.llm import build_llm_client, close_llm_clients
from voice_analytics.summarization import (
    summarize_audio,
    summarize_text,
    supported_formats,
)

logger = logging.getLogger("voice_analytics.cli.summarize")


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``summarize`` subcommand."""
    parser = subparsers.add_parser(
        "summarize",
        help="Summarize a transcript or an audio file.",
        description=(
            "Summarize a text transcript (--input-text) or audio directly "
            "(--input-audio). Credentials are read from the environment and "
            "never from command-line arguments."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--input-text", metavar="PATH",
        help="Transcript file to summarize. One focused call per format.",
    )
    source.add_argument(
        "--input-audio", metavar="PATH",
        help="Audio file to summarize directly, skipping transcription.",
    )
    parser.add_argument(
        "--output", default="-", metavar="PATH",
        help="Where to write the JSON result. '-' means stdout (the default).",
    )
    parser.add_argument(
        "--formats", default="short_summary", metavar="LIST",
        help=(
            "Comma-separated formats. Choices: "
            f"{', '.join(supported_formats())}. Default: short_summary."
        ),
    )
    parser.add_argument(
        "--model", default=None, metavar="NAME",
        help="Model to use. Defaults to MODEL_NAME from the environment.",
    )
    parser.add_argument(
        "--mime-type", default=None, metavar="TYPE",
        help="Override the audio MIME type. Inferred from the extension by default.",
    )
    parser.add_argument(
        "--user-instruction", default=None, metavar="TEXT",
        help="Extra instruction that takes priority over the system prompt.",
    )
    parser.set_defaults(handler=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Entry point invoked by the dispatcher. Returns a process exit code."""
    return asyncio.run(_run_async(args))


async def _run_async(args: argparse.Namespace) -> int:
    formats = [part.strip() for part in args.formats.split(",") if part.strip()]

    settings = load_settings()                      # ValidationError -> CONFIGURATION
    model_name = args.model or settings.MODEL_NAME

    say(logger,
        "Connecting to the AI service at %s (TLS verification %s).",
        settings.LLM_BASE_URL,
        "on" if settings.ssl_verify() is not False else "off",
        gateway=settings.LLM_BASE_URL, model=model_name,
        ssl_verify=settings.SSL_VERIFY,
        ssl_ca_bundle=settings.SSL_CA_BUNDLE or "none",
        source="audio" if args.input_audio else "text",
        formats=formats,
    )

    client = build_llm_client(settings)

    try:
        if args.input_audio:
            audio = read_bytes(args.input_audio)  # ValueError -> INVALID_INPUT
            if not settings.supports_transcription(model_name):
                allowed = ", ".join(sorted(settings.transcription_models()))
                raise ValueError(
                    f"Model {model_name!r} cannot process audio. Use one of: {allowed}."
                )
            result = await summarize_audio(
                client=client,
                content=audio,
                mime_type=infer_mime_type(args.input_audio, args.mime_type),
                model_name=model_name,
                formats=formats,
                user_instruction=args.user_instruction,
            )
        else:
            transcript = read_text(args.input_text)
            result = await summarize_text(
                client=client,
                text=transcript,
                model_name=model_name,
                formats=formats,
                user_instruction=args.user_instruction,
            )
    finally:
        # Always release sockets, even when the call above failed.
        await close_llm_clients()

    write_json(result.model_dump(mode="json"), args.output)

    if result.is_empty:
        # Fail the orchestrating task: an empty summary is a real problem.
        say(logger,
            "No summaries were produced, so this run is being marked as failed.",
            level=logging.ERROR, formats=formats, exit_code=8,
        )
        return exit_codes.PROCESSING_FAILED

    return exit_codes.SUCCESS
