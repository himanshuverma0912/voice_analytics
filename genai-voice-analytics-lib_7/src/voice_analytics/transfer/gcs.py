"""Writing recordings into a Google Cloud Storage bucket.

``google-cloud-storage`` is imported inside the functions, like paramiko, so
the core library stays free of a cloud SDK. Install it with the ``transfer``
extra.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, BinaryIO

from voice_analytics.exceptions import ConfigurationError, TransferError
from voice_analytics.observability import human_bytes, say

logger = logging.getLogger(__name__)

CHUNK_BYTES = 8 * 1024 * 1024
"""Stream in 8 MiB chunks. A recording is never read into memory whole, so a
pod's memory limit does not cap the file size it can move."""


def _storage() -> Any:
    try:
        from google.cloud import storage
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ConfigurationError(
            "The transfer command needs the 'transfer' extra: "
            "pip install 'genai-voice-analytics-lib[transfer]'"
        ) from exc
    return storage


def open_bucket(bucket_name: str):
    """Get a handle on the destination bucket.

    Credentials come from the environment the way every Google client library
    resolves them -- workload identity in a cluster, ``GOOGLE_APPLICATION_
    CREDENTIALS`` elsewhere. Nothing is read from an argument, so no key
    material appears in a pod spec or a task log.
    """
    if not bucket_name:
        raise ConfigurationError("A destination bucket name is required")

    storage = _storage()
    try:
        return storage.Client().bucket(bucket_name)
    except Exception as exc:  # noqa: BLE001 - the SDK raises many shapes here
        raise TransferError(
            f"Could not open the bucket {bucket_name!r}: {exc}. Check that the "
            "pod's service account has objectAdmin on it."
        ) from exc


class _HashingReader:
    """Wraps a stream, hashing every byte that passes through it.

    This is why the checksum is free: the file is read once, and the digest
    falls out of the same pass that uploads it. Hashing afterwards would mean
    downloading each recording twice.
    """

    def __init__(self, source: BinaryIO):
        self._source = source
        self._digest = hashlib.md5()  # noqa: S324 - matches transcriptions.content_hash
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(size)
        if chunk:
            self._digest.update(chunk)
            self.bytes_read += len(chunk)
        return chunk

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def upload_stream(
    bucket,
    object_name: str,
    source: BinaryIO,
    content_type: str | None = None,
) -> tuple[str, int]:
    """Stream ``source`` into the bucket, returning ``(md5_hex, bytes)``.

    Raises:
        TransferError: The upload did not complete. The object is left absent
            rather than half-written -- a resumable upload that fails is not
            finalised, so a later run sees nothing and retries cleanly.
    """
    blob = bucket.blob(object_name)
    blob.chunk_size = CHUNK_BYTES
    reader = _HashingReader(source)

    try:
        blob.upload_from_file(reader, content_type=content_type, rewind=False)
    except Exception as exc:  # noqa: BLE001 - the SDK raises many shapes here
        raise TransferError(
            f"Could not upload {object_name} to gs://{bucket.name}: {exc}"
        ) from exc

    say(logger, "Copied %s (%s).", object_name, human_bytes(reader.bytes_read),
        object_name=object_name, bytes=reader.bytes_read,
        content_hash=reader.hexdigest, level=logging.DEBUG)
    return reader.hexdigest, reader.bytes_read


def object_exists(bucket, object_name: str) -> bool:
    """Whether the object is already there, so a re-run does not re-copy it."""
    try:
        return bucket.blob(object_name).exists()
    except Exception:  # noqa: BLE001
        # Treat an unanswerable question as "not there" and let the upload
        # decide. Failing the whole transfer over a existence check would be
        # worse than copying one file twice.
        return False
