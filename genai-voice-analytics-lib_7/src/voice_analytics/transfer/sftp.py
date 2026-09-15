"""Reading recordings off an SFTP server.

``paramiko`` is imported inside the functions rather than at module scope so
the rest of the library -- and its container image -- does not carry an SSH
stack it never uses. Install it with the ``transfer`` extra.
"""

from __future__ import annotations

import fnmatch
import logging
import stat
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator

from voice_analytics.config import Settings
from voice_analytics.exceptions import ConfigurationError, TransferError
from voice_analytics.observability import human_bytes, say

if TYPE_CHECKING:  # pragma: no cover
    import paramiko

logger = logging.getLogger(__name__)


def _paramiko() -> Any:
    """Import paramiko, explaining how to get it when it is absent."""
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ConfigurationError(
            "The transfer command needs the 'transfer' extra: "
            "pip install 'genai-voice-analytics-lib[transfer]'"
        ) from exc
    return paramiko


def open_sftp(settings: Settings):
    """Connect to the configured SFTP server and return a client.

    The host key is verified against ``SFTP_KNOWN_HOSTS``. There is no
    auto-accept option: an SFTP session carries both the credential and the
    recordings, so trusting an unverified host would put both at risk on a
    first connection -- exactly when a substitution would go unnoticed.

    Raises:
        ConfigurationError: Required settings are missing, or the known_hosts
            file is unreadable.
        TransferError: The server refused the connection or the credential.
    """
    missing = settings.sftp_missing_settings()
    if missing:
        raise ConfigurationError(
            "SFTP is not fully configured. Missing: " + ", ".join(missing)
        )

    paramiko = _paramiko()
    client = paramiko.SSHClient()

    try:
        client.load_host_keys(settings.SFTP_KNOWN_HOSTS)
    except OSError as exc:
        raise ConfigurationError(
            f"SFTP_KNOWN_HOSTS could not be read ({settings.SFTP_KNOWN_HOSTS}): "
            f"{exc}. Generate it with: "
            f"ssh-keyscan -p {settings.SFTP_PORT} {settings.SFTP_HOST} > known_hosts"
        ) from exc

    # RejectPolicy, not AutoAddPolicy. An unknown host is a failure.
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    say(logger, "Connecting to the recordings server %s as %s.",
        settings.SFTP_HOST, settings.SFTP_USERNAME,
        host=settings.SFTP_HOST, port=settings.SFTP_PORT,
        username=settings.SFTP_USERNAME,
        auth="key" if settings.SFTP_PRIVATE_KEY_PATH else "password",
        known_hosts=settings.SFTP_KNOWN_HOSTS)

    try:
        client.connect(
            hostname=settings.SFTP_HOST,
            port=settings.SFTP_PORT,
            username=settings.SFTP_USERNAME,
            password=settings.SFTP_PASSWORD or None,
            key_filename=settings.SFTP_PRIVATE_KEY_PATH or None,
            passphrase=settings.SFTP_PRIVATE_KEY_PASSPHRASE or None,
            timeout=settings.SFTP_TIMEOUT_SECONDS,
            allow_agent=False,
            look_for_keys=False,
        )
    except paramiko.AuthenticationException as exc:
        raise TransferError(
            f"The recordings server rejected the credentials for "
            f"{settings.SFTP_USERNAME}@{settings.SFTP_HOST}."
        ) from exc
    except paramiko.SSHException as exc:
        raise TransferError(
            f"Could not establish an SFTP session with {settings.SFTP_HOST}: "
            f"{exc}. If the host key changed, SFTP_KNOWN_HOSTS needs updating "
            "-- do not bypass the check."
        ) from exc
    except OSError as exc:
        raise TransferError(
            f"Could not reach {settings.SFTP_HOST}:{settings.SFTP_PORT}: {exc}"
        ) from exc

    return client


def list_audio_files(
    sftp,
    remote_dir: str,
    pattern: str = "*",
    min_bytes: int = 1,
) -> Iterator[tuple[str, int, datetime | None]]:
    """Yield ``(name, size, modified)`` for each candidate file in ``remote_dir``.

    Directories are skipped rather than descended: recordings land in one
    directory per batch, and walking a tree would quietly pick up files from a
    neighbouring batch.

    Args:
        sftp: An open SFTP channel.
        remote_dir: Directory to list.
        pattern: Glob the filename must match, e.g. ``"*.wav"``.
        min_bytes: Files at or below this size are skipped. The default of 1
            drops zero-byte files, which is what a half-finished upload looks
            like.
    """
    try:
        entries = sftp.listdir_attr(remote_dir)
    except OSError as exc:
        raise TransferError(
            f"Could not list {remote_dir} on the recordings server: {exc}"
        ) from exc

    for entry in sorted(entries, key=lambda e: e.filename):
        if entry.st_mode is not None and stat.S_ISDIR(entry.st_mode):
            continue
        if not fnmatch.fnmatch(entry.filename, pattern):
            continue
        size = entry.st_size or 0
        if size < min_bytes:
            continue
        modified = (
            datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc)
            if entry.st_mtime
            else None
        )
        yield entry.filename, size, modified


def open_remote(sftp, remote_path: str, size_bytes: int):
    """Open a remote file for streaming.

    The read-ahead window matters: without it paramiko round-trips per read,
    which turns a large recording into thousands of sequential requests.
    """
    try:
        handle = sftp.open(remote_path, "rb")
    except OSError as exc:
        raise TransferError(f"Could not open {remote_path}: {exc}") from exc

    handle.prefetch(size_bytes)
    say(logger, "Reading %s (%s).", remote_path, human_bytes(size_bytes),
        path=remote_path, bytes=size_bytes, level=logging.DEBUG)
    return handle
