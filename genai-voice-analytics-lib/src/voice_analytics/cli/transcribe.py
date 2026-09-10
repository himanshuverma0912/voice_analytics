"""``transcribe`` command: an audio file in, a JSON transcript out."""

from __future__ import annotations

import argparse
import asyncio
import logging

from voice_analytics.cli._common import (
    infer_mime_type,
    read_bytes,
    write_json,
)
from voice_analytics.config import load_settings
from voice_analytics.llm import build_llm_client, close_llm_clients
from voice_analytics.transcription import transcribe

logger = logging.getLogger("voice_analytics.cli.transcribe")


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``transcribe`` subcommand."""
    parser = subparsers.add_parser(
        "transcribe",
        help="Transcribe an audio file, optionally translating it.",
        description=(
            "Transcribe an audio file to structured JSON. Credentials are read "
            "from the environment (LLM_BASE_URL, LLM_API_KEY) and never from "
            "command-line arguments."
        ),
    )
    parser.add_argument(
        "--input", required=True, metavar="PATH",
        help="Audio file to transcribe.",
    )
    parser.add_argument(
        "--output", default="-", metavar="PATH",
        help="Where to write the JSON result. '-' means stdout (the default).",
    )
    parser.add_argument(
        "--model", default=None, metavar="NAME",
        help="Model to use. Defaults to MODEL_NAME from the environment.",
    )
    parser.add_argument(
        "--target-lang", default=None, metavar="LANG",
        help="Translate into this language, e.g. 'hi' or 'Hindi'. Omit to skip translation.",
    )
    parser.add_argument(
        "--romanize", action="store_true",
        help="Ask for Latin-script output.",
    )
    parser.add_argument(
        "--mime-type", default=None, metavar="TYPE",
        help="Override the MIME type. Inferred from the file extension by default.",
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
    audio = read_bytes(args.input)          # ValueError -> INVALID_INPUT
    settings = load_settings()              # ValidationError -> CONFIGURATION
    model_name = args.model or settings.MODEL_NAME

    if not settings.supports_transcription(model_name):
        # Catch a text-only model here rather than paying for a failed call.
        allowed = ", ".join(sorted(settings.transcription_models()))
        raise ValueError(
            f"Model {model_name!r} cannot process audio. Use one of: {allowed}."
        )

    logger.info(
        "event=transcribe_started input=%s model=%s target_lang=%s bytes=%d",
        args.input, model_name, args.target_lang or "none", len(audio),
    )

    client = build_llm_client(settings)
    try:
        result = await transcribe(
            client=client,
            content=audio,
            mime_type=infer_mime_type(args.input, args.mime_type),
            model_name=model_name,
            target_lang=args.target_lang,
            romanize=args.romanize,
            user_instruction=args.user_instruction,
        )
    finally:
        await close_llm_clients()

    logger.info(
        "event=transcribe_completed segments=%d language=%s elapsed_ms=%.1f",
        len(result.segments),
        result.primary_language or "unknown",
        result.processing_time_ms,
    )

    if result.is_empty:
        # Exit non-zero: an empty transcript is almost always a real problem
        # (silent audio, wrong file) and should fail the Airflow task.
        logger.error("event=transcribe_empty input=%s", args.input)
        write_json(result.model_dump(mode="json"), args.output)
        from voice_analytics.cli import exit_codes

        return exit_codes.PROCESSING_FAILED

    write_json(result.model_dump(mode="json"), args.output)
    return 0
