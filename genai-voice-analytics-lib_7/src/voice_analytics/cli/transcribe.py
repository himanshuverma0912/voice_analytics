"""``transcribe`` command: an audio file in, a JSON transcript out."""

from __future__ import annotations

import argparse
import asyncio
import logging

from voice_analytics.cli import exit_codes
from voice_analytics.cli._common import (
    infer_mime_type,
    read_bytes,
    write_json,
)
from voice_analytics.config import load_settings
from voice_analytics.observability import say
from voice_analytics.anonymization import anonymize, build_anonymization_client
from voice_analytics.llm import build_llm_client, close_llm_clients
from voice_analytics.transcription import transcribe, translate

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
        "--anonymize", action="store_true",
        help=(
            "Remove personal information before the transcript is saved. "
            "With --target-lang this also forces the correct order: "
            "transcribe, then anonymize, then translate -- so no personal "
            "information reaches storage or the translation model. "
            "Requires ANONYMIZATION_URL."
        ),
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

    say(logger,
        "Connecting to the AI service at %s (TLS verification %s).",
        settings.LLM_BASE_URL,
        "on" if settings.ssl_verify() is not False else "off",
        gateway=settings.LLM_BASE_URL, model=model_name,
        ssl_verify=settings.SSL_VERIFY,
        ssl_ca_bundle=settings.SSL_CA_BUNDLE or "none",
        sdk_max_retries=settings.MAX_RETRIES,
        timeout_s=settings.REQUEST_TIMEOUT_SECONDS,
    )

    if not settings.supports_transcription(model_name):
        # Catch a text-only model here rather than paying for a failed call.
        allowed = ", ".join(sorted(settings.transcription_models()))
        raise ValueError(
            f"Model {model_name!r} cannot process audio. Use one of: {allowed}."
        )

    client = build_llm_client(settings)

    try:
        if args.anonymize:
            result = await _transcribe_anonymize_translate(
                client=client, settings=settings, args=args,
                audio=audio, model_name=model_name,
            )
        else:
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
        # Always release sockets, even when the call above failed.
        await close_llm_clients()

    if result.is_empty:
        # Exit non-zero: an empty transcript is almost always a real problem
        # (silent audio, wrong file) and should fail the Airflow task.
        say(logger,
            "No transcript was produced from %s, so this run is being marked "
            "as failed.", args.input,
            level=logging.ERROR, path=args.input, exit_code=8,
        )
        write_json(result.model_dump(mode="json"), args.output)
        return exit_codes.PROCESSING_FAILED

    write_json(result.model_dump(mode="json"), args.output)
    return exit_codes.SUCCESS


async def _transcribe_anonymize_translate(client, settings, args, audio, model_name):
    """Run the three steps in the order compliance requires.

    Transcribe **without** translating, remove personal information, then
    translate the cleaned text. Doing it in one transcribe-and-translate call
    would send raw personal information to the translation model and store it.

    Exposing this as a single flag rather than three commands is deliberate:
    the ordering *is* the control, and three separate steps could be wired up
    in the wrong order or have the middle one omitted.

    If anonymization fails, the whole run fails. There is no fallback to the
    original transcript.
    """
    result = await transcribe(
        client=client,
        content=audio,
        mime_type=infer_mime_type(args.input, args.mime_type),
        model_name=model_name,
        target_lang=None,            # deliberately not translating yet
        romanize=args.romanize,
        user_instruction=args.user_instruction,
    )

    if result.is_empty:
        return result                # nothing to anonymize

    async with build_anonymization_client(settings) as anon_client:
        clean = await anonymize(anon_client, settings, result.transcript)

    result.transcript = clean
    result.segments = []             # the per-segment text still holds PII

    if args.target_lang:
        say(logger, "Translating the anonymized transcript to %s...",
            args.target_lang, target_lang=args.target_lang, model=model_name)
        result.translated_transcript = await translate(
            client=client,
            text=clean,
            target_lang=args.target_lang,
            model_name=model_name,
            user_instruction=args.user_instruction,
        )

    return result
