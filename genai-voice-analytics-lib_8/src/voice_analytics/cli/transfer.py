"""``transfer`` command: recordings from SFTP into a GCS bucket.

The pipeline's first step. Nothing downstream can start until the audio is in
object storage where the worker pods can reach it.

Needs the ``transfer`` extra. A container that only transcribes or scores does
not need it, which is why it is not a core dependency.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from voice_analytics.cli import exit_codes
from voice_analytics.cli._common import write_json
from voice_analytics.config import load_settings
from voice_analytics.observability import say
from voice_analytics.transfer import transfer_sftp_to_gcs

logger = logging.getLogger("voice_analytics.cli.transfer")


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``transfer`` subcommand."""
    parser = subparsers.add_parser(
        "transfer",
        help="Copy recordings from the SFTP server into a GCS bucket.",
        description=(
            "Stream every matching recording from an SFTP directory into a "
            "bucket, hashing as it goes. Connection settings and credentials "
            "are read from the environment; see env.sample."
        ),
    )
    parser.add_argument(
        "--remote-dir", required=True, metavar="PATH",
        help="Directory on the SFTP server holding the recordings.",
    )
    parser.add_argument(
        "--bucket", required=True, metavar="NAME",
        help="Destination bucket, without the gs:// prefix.",
    )
    parser.add_argument(
        "--prefix", required=True, metavar="PATH",
        help="Object-name prefix, e.g. 'batches/196/audio/'.",
    )
    parser.add_argument(
        "--pattern", default="*", metavar="GLOB",
        help="Filename glob, e.g. '*.wav'. Everything by default.",
    )
    parser.add_argument(
        "--output", default="-", metavar="PATH",
        help="Where to write the JSON manifest. '-' means stdout (the default).",
    )
    parser.add_argument(
        "--already-seen", default=None, metavar="PATH",
        help=(
            "JSON file holding a list of filenames already processed. Those "
            "are skipped. This is how 'only files new since the last "
            "successful run' is implemented -- the caller owns the list, "
            "because the library owns no database."
        ),
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Copy a file even when the destination object already exists.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Stop after N files. For sampling a large source.",
    )
    parser.add_argument(
        "--fail-on-empty", action="store_true",
        help=(
            "Exit 4 when nothing was transferred. For a scheduled run where an "
            "empty source means the upstream job did not deliver, rather than "
            "that there was genuinely nothing new."
        ),
    )
    parser.set_defaults(handler=run)
    return parser


def _load_already_seen(path: str | None) -> set[str]:
    """Read the skip list, tolerating a file that is not there yet.

    A first run has no list, and that must not be an error -- otherwise every
    new flow needs a placeholder file created by hand before it can run once.
    """
    if not path:
        return set()

    file_path = Path(path)
    if not file_path.is_file():
        say(logger,
            "No record of previous runs at %s, so every recording found will "
            "be treated as new.",
            path, path=path)
        return set()

    try:
        loaded = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read --already-seen file {path}: {exc}") from exc

    if isinstance(loaded, dict):
        loaded = loaded.get("names") or loaded.get("filenames") or []
    if not isinstance(loaded, list):
        raise ValueError(
            f"--already-seen file {path} must hold a JSON list of filenames"
        )

    names = {str(item) for item in loaded}
    say(logger, "%d recording(s) were processed by previous runs and will be "
        "skipped.", len(names), already_seen=len(names))
    return names


def run(args: argparse.Namespace) -> int:
    """Entry point invoked by the dispatcher. Returns a process exit code."""
    settings = load_settings()              # ValidationError -> CONFIGURATION
    already_seen = _load_already_seen(args.already_seen)

    result = transfer_sftp_to_gcs(
        settings=settings,
        remote_dir=args.remote_dir,
        bucket_name=args.bucket,
        prefix=args.prefix,
        pattern=args.pattern,
        already_seen=already_seen,
        overwrite=args.overwrite,
        limit=args.limit,
    )

    write_json(result.model_dump(mode="json"), args.output)

    if result.failed:
        # Report, do not fail. The files that did move are usable, and the
        # manifest names the ones that did not so the caller can decide.
        say(logger,
            "%d recording(s) could not be copied and were left on the server: "
            "%s",
            len(result.failed), ", ".join(f.name for f in result.failed),
            level=logging.WARNING, failed=[f.name for f in result.failed])

    if result.is_empty and args.fail_on_empty:
        say(logger,
            "Nothing was transferred from %s, and --fail-on-empty was set.",
            args.remote_dir, level=logging.ERROR,
            remote_dir=args.remote_dir, exit_code=4)
        return exit_codes.INVALID_INPUT

    return exit_codes.SUCCESS
