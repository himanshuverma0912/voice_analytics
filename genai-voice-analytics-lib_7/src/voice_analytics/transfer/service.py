"""Moving recordings from SFTP to GCS: the pipeline's first step.

Streaming, one file at a time, hashing as it goes. The result names every file
that moved with its checksum, which is what lets a caller implement "only files
new since the last successful run" -- tracked by path *and* checksum, so a
rename is not reprocessed and a corrected file is.

Deliberately not parallel. The bottleneck is the SFTP server, which is usually
a shared corporate box that will throttle or drop connections under a fan-out,
and a transfer that finishes slowly beats one that trips a rate limit halfway.
"""

from __future__ import annotations

import logging
import mimetypes
import time

from voice_analytics.config import Settings
from voice_analytics.exceptions import TransferError
from voice_analytics.observability import human_bytes, human_duration, say
from voice_analytics.transfer.gcs import object_exists, open_bucket, upload_stream
from voice_analytics.transfer.sftp import list_audio_files, open_remote, open_sftp
from voice_analytics.transfer.models import (
    SkippedFile,
    TransferredFile,
    TransferResult,
)

logger = logging.getLogger(__name__)


def _content_type(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def transfer_sftp_to_gcs(
    settings: Settings,
    remote_dir: str,
    bucket_name: str,
    prefix: str,
    pattern: str = "*",
    already_seen: set[str] | None = None,
    overwrite: bool = False,
    limit: int | None = None,
) -> TransferResult:
    """Copy every matching recording from ``remote_dir`` into the bucket.

    Args:
        settings: Carries the SFTP connection settings.
        remote_dir: Directory on the SFTP server holding the recordings.
        bucket_name: Destination bucket.
        prefix: Object-name prefix, e.g. ``"batches/196/audio/"``.
        pattern: Glob the filename must match, e.g. ``"*.wav"``.
        already_seen: Filenames the caller has processed before. Anything
            listed here is skipped. The caller owns this set -- the library has
            no database.
        overwrite: Copy a file even when the destination object already exists.
        limit: Stop after this many files. For a dry run over a large source.

    Returns:
        A :class:`TransferResult` naming what moved, what was skipped and what
        failed.

    Raises:
        ConfigurationError: SFTP settings are incomplete or the known_hosts
            file is unreadable.
        TransferError: The server could not be reached, or the directory could
            not be listed. A failure on an *individual* file is recorded in
            ``result.failed`` instead -- one unreadable recording must not
            abandon the other 12,479.
    """
    started = time.perf_counter()
    already_seen = already_seen or set()
    prefix = prefix.rstrip("/") + "/" if prefix else ""

    result = TransferResult()
    bucket = open_bucket(bucket_name)
    client = open_sftp(settings)

    try:
        sftp = client.open_sftp()
        candidates = list(list_audio_files(sftp, remote_dir, pattern=pattern))

        say(logger,
            "Found %d recording(s) in %s matching '%s'.",
            len(candidates), remote_dir, pattern,
            found=len(candidates), remote_dir=remote_dir, pattern=pattern,
            already_seen=len(already_seen))

        for name, size, modified in candidates:
            if limit is not None and len(result.transferred) >= limit:
                result.skipped.append(SkippedFile(
                    name=name, reason=f"stopped after the --limit of {limit} files"))
                continue

            if name in already_seen:
                result.skipped.append(SkippedFile(
                    name=name, reason="already processed by a previous run"))
                continue

            object_name = f"{prefix}{name}"
            if not overwrite and object_exists(bucket, object_name):
                result.skipped.append(SkippedFile(
                    name=name, reason="already present at the destination"))
                continue

            try:
                handle = open_remote(sftp, f"{remote_dir.rstrip('/')}/{name}", size)
                try:
                    digest, written = upload_stream(
                        bucket, object_name, handle, _content_type(name))
                finally:
                    handle.close()
            except (TransferError, OSError) as exc:
                # One bad file, not a bad run.
                say(logger,
                    "Could not copy %s, so it was left behind (%s). The rest of "
                    "the transfer continued.",
                    name, type(exc).__name__,
                    level=logging.WARNING, name=name, error=str(exc)[:200])
                result.failed.append(SkippedFile(name=name, reason=str(exc)[:300]))
                continue

            result.transferred.append(TransferredFile(
                name=name,
                source_path=f"{remote_dir.rstrip('/')}/{name}",
                destination=f"gs://{bucket_name}/{object_name}",
                size_bytes=written,
                content_hash=digest,
                modified_at=modified.isoformat() if modified else None,
            ))
            result.total_bytes += written
    finally:
        # Close the session even when listing or a hard failure aborted the run.
        client.close()

    result.processing_time_ms = round((time.perf_counter() - started) * 1000, 2)

    say(logger,
        "Moved %d recording(s) totalling %s in %s. Skipped %d, failed %d.",
        len(result.transferred), human_bytes(result.total_bytes),
        human_duration(result.processing_time_ms),
        len(result.skipped), len(result.failed),
        transferred=len(result.transferred), total_bytes=result.total_bytes,
        skipped=len(result.skipped), failed=len(result.failed),
        elapsed_ms=result.processing_time_ms)

    return result
