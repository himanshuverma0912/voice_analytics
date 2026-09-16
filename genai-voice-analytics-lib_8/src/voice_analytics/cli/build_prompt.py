"""``build-prompt`` command: KPI codes in, an assembled scoring prompt out.

Run **once per batch**, before any scoring pod starts. The resulting file is
what every ``analyse`` pod is then given with ``--prompt``.

Splitting this out of ``analyse`` is what keeps PromptHub traffic proportional
to batches rather than to calls: a 12,480-file batch makes one registry
request here instead of 12,480 identical ones.

It also means an orchestrator never has to import this library. Resolving
prompts was previously the one step a caller could not do through the
container, which forced the library onto the scheduler's own machine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from voice_analytics.analysis import build_consolidated_prompt
from voice_analytics.cli import exit_codes
from voice_analytics.cli._common import write_json
from voice_analytics.config import load_settings
from voice_analytics.exceptions import PromptHubError
from voice_analytics.observability import say
from voice_analytics.prompthub import (
    build_prompthub_client,
    fetch_prompts,
    resolve_base_prompt,
    resolve_kpi_prompts,
)

logger = logging.getLogger("voice_analytics.cli.build_prompt")


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``build-prompt`` subcommand."""
    parser = subparsers.add_parser(
        "build-prompt",
        help="Assemble the KPI scoring prompt for a batch.",
        description=(
            "Fetch the approved prompts for the selected KPIs and write a "
            "single scoring prompt. Run once per batch; pass the result to "
            "'analyse --prompt'. The PromptHub credential is read from "
            "LLM_API_KEY in the environment."
        ),
    )
    parser.add_argument(
        "--kpi-codes", default=None, metavar="CODES",
        help=(
            "Comma-separated KPI codes to score, in display order, "
            "e.g. 'rpc_verified,disclosure_given'. Required when resolving "
            "from PromptHub; with --from-file, omitting it takes every KPI in "
            "the file."
        ),
    )
    parser.add_argument(
        "--from-prompts-response", default=None, metavar="PATH",
        help=(
            "Assemble from a saved PromptHub *prompts* reply, for a machine "
            "that cannot reach the registry directly. This is the reply from "
            "the prompts endpoint, not the use case one:\n"
            "  curl -sk -H 'lite-llm-api-key: <key>' \\\n"
            "    <base>/extenal/get-all-prompts > prompts.json\n"
            "Unlike --from-file this carries the real approved versions, so "
            "the assembled prompt matches what production would use today."
        ),
    )
    parser.add_argument(
        "--from-file", default=None, metavar="PATH",
        help=(
            "Assemble from a local JSON snapshot instead of PromptHub, for a "
            "machine that cannot reach the registry. Expects "
            "{'base_prompt': str, 'sections': [{'kpis': [{'kpi_code', "
            "'kpi_name', 'prompt'}]}]}. The prompt is assembled by the same "
            "code either way -- only the source of the text differs, and a "
            "snapshot goes stale silently, so PromptHub remains the "
            "authoritative path."
        ),
    )
    parser.add_argument(
        "--output", required=True, metavar="PATH",
        help="Where to write the assembled prompt. Plain text.",
    )
    parser.add_argument(
        "--report", default=None, metavar="PATH",
        help=(
            "Optional JSON file recording which KPIs resolved and which were "
            "skipped. Write it when the caller needs to record that a batch "
            "was scored against fewer KPIs than were selected."
        ),
    )
    parser.set_defaults(handler=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Entry point invoked by the dispatcher. Returns a process exit code."""
    return asyncio.run(_run_async(args))


def _parse_codes(raw: str) -> list[str]:
    """Split the comma-separated list, dropping blanks and duplicates.

    Order is preserved: the prompt presents KPIs in the order the batch
    selected them, and a reordered prompt is a different prompt.
    """
    seen: set[str] = set()
    codes: list[str] = []
    for part in raw.split(","):
        code = part.strip()
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _load_from_file(path: str, codes: list[str]) -> tuple[str, list, list[str]]:
    """Read a base prompt and KPI definitions out of a local JSON snapshot.

    Returns ``(base_prompt, resolved, skipped)`` to match
    :func:`resolve_kpi_prompts`, so the caller treats both sources alike.

    Raises:
        ValueError: The file is missing, is not JSON, or has no usable base
            prompt or KPI definitions.
    """
    from voice_analytics.analysis import KPIPrompt

    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"--from-file not found: {path}")

    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc

    base_prompt = (document or {}).get("base_prompt")
    if not base_prompt or not str(base_prompt).strip():
        raise ValueError(
            f"{path} has no 'base_prompt'. Without it the model has no output "
            "format or scoring rules to follow."
        )

    available: dict[str, KPIPrompt] = {}
    order: list[str] = []
    for section in document.get("sections") or []:
        for kpi in section.get("kpis") or []:
            code = kpi.get("kpi_code")
            instructions = kpi.get("prompt") or kpi.get("description")
            if not code or not instructions:
                continue
            available[code] = KPIPrompt(code, kpi.get("kpi_name") or code, instructions)
            order.append(code)

    if not available:
        raise ValueError(f"{path} contains no KPI definitions.")

    # No codes named means take the file's own order, which is how the
    # sections were authored.
    wanted = codes or order
    resolved = [available[code] for code in wanted if code in available]
    skipped = [code for code in wanted if code not in available]

    say(logger,
        "Read %d KPI definition(s) from %s; using %d.",
        len(available), file_path.name, len(resolved),
        path=str(file_path), available=len(available),
        used=len(resolved), skipped=len(skipped))
    return str(base_prompt), resolved, skipped


def _load_prompts_response(path: str) -> list[dict]:
    """Read the prompt list out of a saved PromptHub reply.

    Accepts the whole response body, ``data`` on its own, or a bare list, so a
    curl saved any of the obvious ways works.

    Raises:
        ValueError: The file is missing, is not JSON, or holds no prompt list.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"--from-prompts-response not found: {path}")

    try:
        document = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"{path} is not readable JSON: {exc}. Check the curl returned a "
            "body -- an HTML error page or an empty file looks like this."
        ) from exc

    if isinstance(document, list):
        prompts = document
    elif isinstance(document, dict):
        prompts = (document.get("data") or {}).get("prompts")
        if prompts is None:
            prompts = document.get("prompts")
    else:
        prompts = None

    if not isinstance(prompts, list) or not prompts:
        raise ValueError(
            f"{path} holds no prompt list. Expected the reply from "
            "/extenal/get-all-prompts, whose body is "
            "{'data': {'prompts': [...]}} -- save the whole response."
        )

    say(logger, "Read %d prompt version(s) from %s (no request made).",
        len(prompts), file_path.name, path=str(file_path), count=len(prompts))
    return prompts


async def _run_async(args: argparse.Namespace) -> int:
    codes = _parse_codes(args.kpi_codes or "")

    if args.from_prompts_response:
        prompts = _load_prompts_response(args.from_prompts_response)
        if not codes:
            # Every KPI the file defines, minus the base prompt itself.
            codes = sorted({
                str(entry.get("name")) for entry in prompts
                if entry.get("name") and entry.get("name") != "base_prompt"
            })
        base_prompt = resolve_base_prompt(prompts)
        resolved, skipped = resolve_kpi_prompts(prompts, codes)
        return _write(args, base_prompt, resolved, skipped, codes)

    if args.from_file:
        base_prompt, resolved, skipped = _load_from_file(args.from_file, codes)
        codes = codes or [item.kpi_code for item in resolved] + skipped
        return _write(args, base_prompt, resolved, skipped, codes)

    if not codes:
        # Scoring against nothing produces plausible but meaningless output,
        # so this is refused here rather than 12,480 pods later.
        raise ValueError("--kpi-codes contained no usable KPI codes")

    settings = load_settings()              # ValidationError -> CONFIGURATION
    if not settings.LLM_API_KEY:
        raise ValueError(
            "LLM_API_KEY must be set: PromptHub identifies the caller by it."
        )

    say(logger,
        "Looking up the scoring instructions for %d KPI(s) in the prompt "
        "registry at %s.",
        len(codes), settings.PROMPTHUB_BASE_URL,
        kpi_codes=codes, registry=settings.PROMPTHUB_BASE_URL,
        default_usecase=bool(settings.PROMPTHUB_DEFAULT_LITELLM_KEY),
    )

    async with build_prompthub_client(settings) as client:
        try:
            prompts = await fetch_prompts(client, settings, settings.LLM_API_KEY)
        except PromptHubError:
            # The originating service tolerated this and fell back entirely to
            # the default use case. Re-raise when there is no default, because
            # then there is nothing to fall back to.
            if not settings.PROMPTHUB_DEFAULT_LITELLM_KEY:
                raise
            say(logger,
                "Could not read this use case's prompts. Falling back to the "
                "default use case for all of them.",
                level=logging.WARNING, fallback="default_usecase")
            prompts = []

        fallback: list[dict] | None = None
        if settings.PROMPTHUB_DEFAULT_LITELLM_KEY:
            try:
                fallback = await fetch_prompts(
                    client, settings, settings.PROMPTHUB_DEFAULT_LITELLM_KEY)
            except PromptHubError as exc:
                # Not fatal on its own: a KPI missing from both sources is
                # skipped either way, and that is reported.
                say(logger,
                    "Could not read the default use case's prompts (%s). Any "
                    "KPI without a prompt of its own will be skipped.",
                    type(exc).__name__, level=logging.WARNING,
                    error=str(exc)[:200])

    base_prompt = resolve_base_prompt(prompts, fallback)
    resolved, skipped = resolve_kpi_prompts(prompts, codes, fallback)

    return _write(args, base_prompt, resolved, skipped, codes)


def _write(args, base_prompt: str, resolved: list, skipped: list[str],
           codes: list[str]) -> int:
    """Assemble, save, and report -- shared by both sources."""
    if not resolved:
        raise ValueError(
            "None of the selected KPIs has a prompt, so there is nothing to "
            f"score against. Requested: {', '.join(codes)}."
        )

    prompt = build_consolidated_prompt(base_prompt, resolved)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(prompt, encoding="utf-8")

    say(logger,
        "Scoring prompt ready: %d KPI(s), %d characters, saved to %s.",
        len(resolved), len(prompt), out_path,
        kpis_resolved=len(resolved), kpis_skipped=len(skipped),
        chars=len(prompt), path=str(out_path),
    )

    if args.report:
        write_json(
            {
                "kpi_codes_requested": codes,
                "kpi_codes_resolved": [item.kpi_code for item in resolved],
                "kpi_codes_skipped": skipped,
                "prompt_chars": len(prompt),
            },
            args.report,
        )

    return exit_codes.SUCCESS
