#!/usr/bin/env python3
"""Look up a use case's model entitlement and key, the way the DAG does.

The originating system never used one shared gateway key. Each use case has its
own key **per model**, held in PromptHub, and the pipeline fetches it before
doing any work:

    GET {PROMPTHUB_BASE_URL}/client/usecase/{usecase_id}
        header: INTERNAL-API-KEY

    -> { "data": { "status": "APPROVED", "disable": false,
                   "models": [ { "modelName": "gemini-2.5-flash",
                                 "llmApiKey": "sk-...",
                                 "removeTokenModelAccess": false } ] } }

A model is usable only when the use case is `APPROVED`, not `disable`d, and its
entry has an `llmApiKey` with no `removeTokenModelAccess` flag. That check is
`validate_access` in the DAG -- the only place per-use-case model entitlement is
enforced anywhere in the system.

The same key then authenticates to PromptHub for the prompts themselves, as the
`lite-llm-api-key` header, which is what `build-prompt` sends.

This script is that lookup, for a laptop. Keys are **masked** unless you ask for
them, and `--write-env` puts them straight into `.env` without printing.

    python scripts/fetch_usecase_key.py --usecase-id 1149
    python scripts/fetch_usecase_key.py --usecase-id 1149 --write-env

**Under Airflow this script is not used and no one types a use case id.** The
DAG is triggered with a batch id, reads `usecase_id` off that row, and does the
lookup itself in `validate_access` -- per run, so two batches for two use cases
in the same DAG each get their own key. The id lives on the data, never in a
configuration file:

    dag_run.conf {"transcription_batch_id": 196}
        -> read_config      SELECT usecase_id FROM transcription_batches WHERE id=196
        -> validate_access  GET /client/usecase/<that>
        -> pods             LLM_API_KEY from the entry for each model

This exists because a laptop has no batch row to read from.
"""

from __future__ import annotations

import argparse
import os
import sys

TRANSCRIPTION_MODEL = "gemini-2.5-flash"
ANALYSIS_MODEL = "qwen3-30b-a3b-instruct"


def mask(value: str) -> str:
    """Enough to recognise a key, not enough to use one."""
    if not value:
        return "(none)"
    return f"{value[:6]}...{value[-4:]}" if len(value) > 14 else "****"


def find_model_entry(usecase: dict, model_name: str) -> dict | None:
    """The use case's entry for one model, or None when it may not be used.

    Ported from `prompthub_client.find_model_entry`. The two refusals are
    distinct: `removeTokenModelAccess` means access was withdrawn, a missing
    `llmApiKey` means it was never granted.
    """
    target = model_name.strip().lower()
    for model in usecase.get("models") or []:
        if (model.get("modelName") or "").strip().lower() != target:
            continue
        if model.get("removeTokenModelAccess"):
            return None
        if not model.get("llmApiKey"):
            return None
        return model
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="fetch_usecase_key.py",
        description="Fetch a use case's per-model gateway keys from PromptHub.",
    )
    parser.add_argument(
        "--usecase-id", required=True,
        help=(
            "The PromptHub use case. Find yours in transcription_batches: "
            "SELECT DISTINCT usecase_id, usecase_name FROM transcription_batches; "
            "Examples elsewhere use 1149, which came from a single sample row "
            "and is a placeholder."
        ),
    )
    parser.add_argument("--base-url", default=os.environ.get("PROMPTHUB_BASE_URL"))
    parser.add_argument(
        "--internal-api-key",
        default=os.environ.get("PROMPTHUB_INTERNAL_API_KEY"),
        help="Read from PROMPTHUB_INTERNAL_API_KEY when omitted.",
    )
    parser.add_argument("--transcription-model", default=TRANSCRIPTION_MODEL)
    parser.add_argument("--analysis-model", default=ANALYSIS_MODEL)
    parser.add_argument("--ca-bundle", default=os.environ.get("SSL_CA_BUNDLE"),
                        help="Corporate CA PEM. Without it, TLS is not verified.")
    parser.add_argument("--show", action="store_true",
                        help="Print the keys in full. They will be in your shell history.")
    parser.add_argument("--write-env", metavar="PATH", nargs="?", const=".env",
                        help="Write LLM_API_KEY into this .env file instead of printing it.")
    args = parser.parse_args()

    if not args.base_url:
        print("ERROR: --base-url or PROMPTHUB_BASE_URL is required", file=sys.stderr)
        return 2
    if not args.internal_api_key:
        print("ERROR: --internal-api-key or PROMPTHUB_INTERNAL_API_KEY is required",
              file=sys.stderr)
        return 2

    import httpx

    url = f"{args.base_url.rstrip('/')}/client/usecase/{args.usecase_id}"
    verify: bool | str = args.ca_bundle if args.ca_bundle else False
    if not args.ca_bundle:
        print("WARNING: no CA bundle given, so this request does not verify TLS.\n"
              "         Pass --ca-bundle for anything beyond a one-off lookup.\n",
              file=sys.stderr)

    try:
        response = httpx.get(
            url,
            headers={"accept": "*/*", "INTERNAL-API-KEY": args.internal_api_key},
            timeout=60, verify=verify,
        )
    except httpx.RequestError as exc:
        print(f"ERROR: could not reach PromptHub at {url}: {exc}", file=sys.stderr)
        return 7

    if not response.is_success:
        print(f"ERROR: PromptHub returned HTTP {response.status_code}: "
              f"{response.text[:300]}", file=sys.stderr)
        return 5

    body = response.json()
    if not body.get("success"):
        print(f"ERROR: PromptHub reported failure: {body.get('message')}", file=sys.stderr)
        return 5
    usecase = body.get("data")
    if not usecase:
        print("ERROR: PromptHub response contained no 'data'", file=sys.stderr)
        return 5

    print(f"Use case {args.usecase_id}: {usecase.get('name') or '(unnamed)'}")
    print(f"  status       {usecase.get('status')}")
    print(f"  disabled     {bool(usecase.get('disable'))}")

    usable = not usecase.get("disable") and usecase.get("status") == "APPROVED"
    if not usable:
        print("\nThis use case is disabled or not approved, so the pipeline would "
              "refuse to run against it.")
        return 5

    print("\n  model                          usable  key")
    keys: dict[str, str] = {}
    for label, model_name in (("transcription", args.transcription_model),
                              ("analysis", args.analysis_model)):
        entry = find_model_entry(usecase, model_name)
        if entry:
            key = entry["llmApiKey"]
            keys[label] = key
            shown = key if args.show else mask(key)
            print(f"  {model_name:30} yes     {shown}")
        else:
            present = any(
                (m.get("modelName") or "").strip().lower() == model_name.strip().lower()
                for m in usecase.get("models") or []
            )
            reason = "access withdrawn or no key" if present else "not listed"
            print(f"  {model_name:30} NO      ({reason})")

    if "transcription" not in keys:
        print(f"\nWithout {args.transcription_model} nothing can be transcribed.")
        return 5

    if args.write_env:
        _write_env(args.write_env, keys["transcription"])
        print(f"\nLLM_API_KEY written to {args.write_env}. Not printed, so it is "
              "not in your shell history.")
    elif not args.show:
        print("\nKeys are masked. Use --write-env to put one into .env, or --show "
              "to print it in full.")

    if "analysis" not in keys:
        print(f"\nNote: {args.analysis_model} is not available to this use case. "
              "Transcription would work; scoring would not.")

    return 0


def _write_env(path: str, key: str) -> None:
    """Set LLM_API_KEY in a .env file, replacing any existing line."""
    lines: list[str] = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            lines = [l for l in handle.read().splitlines()
                     if not l.startswith("LLM_API_KEY=")]
    lines.append(f"LLM_API_KEY={key}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


if __name__ == "__main__":
    raise SystemExit(main())
