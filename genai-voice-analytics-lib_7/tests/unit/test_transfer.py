"""SFTP to GCS transfer.

paramiko and google-cloud-storage are optional extras, so everything here runs
against fakes. That is not a compromise: the behaviour worth pinning is the
decision logic -- what gets skipped, what counts as a per-file failure versus a
run failure, and that a credential is never trusted without a known host.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from voice_analytics.config import Settings
from voice_analytics.exceptions import ConfigurationError, TransferError
from voice_analytics.transfer.gcs import _HashingReader
from voice_analytics.transfer.service import _content_type, transfer_sftp_to_gcs


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSFTP:
    def __init__(self, files: dict[str, bytes], unreadable: set[str] | None = None):
        self.files = files
        self.unreadable = unreadable or set()

    def listdir_attr(self, remote_dir):
        return [
            SimpleNamespace(filename=name, st_size=len(data), st_mode=0o100644,
                            st_mtime=1_700_000_000)
            for name, data in self.files.items()
        ]

    def open(self, path, mode="rb"):
        name = path.rsplit("/", 1)[-1]
        if name in self.unreadable:
            raise OSError("permission denied")
        handle = io.BytesIO(self.files[name])
        handle.prefetch = lambda size: None
        return handle


class FakeClient:
    def __init__(self, sftp):
        self._sftp = sftp
        self.closed = False

    def open_sftp(self):
        return self._sftp

    def close(self):
        self.closed = True


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name, self.chunk_size = bucket, name, None

    def exists(self):
        return self.name in self.bucket.objects

    def upload_from_file(self, source, content_type=None, rewind=False):
        if self.name in self.bucket.fail_on:
            raise RuntimeError("bucket rejected the upload")
        self.bucket.objects[self.name] = source.read()


class FakeBucket:
    def __init__(self, name="recordings", existing=(), fail_on=()):
        self.name = name
        self.objects = {key: b"" for key in existing}
        self.fail_on = set(fail_on)

    def blob(self, name):
        return FakeBlob(self, name)


def _settings(**over) -> Settings:
    base = dict(
        LLM_BASE_URL="https://llm.internal/v1",
        SFTP_HOST="sftp.internal", SFTP_USERNAME="svc",
        SFTP_PASSWORD="secret", SFTP_KNOWN_HOSTS="/etc/known_hosts",
    )
    base.update(over)
    return Settings(**base)


def _run(files, bucket=None, **kwargs):
    bucket = bucket or FakeBucket()
    client = FakeClient(FakeSFTP(files, kwargs.pop("unreadable", None)))
    with patch("voice_analytics.transfer.service.open_bucket", return_value=bucket), \
         patch("voice_analytics.transfer.service.open_sftp", return_value=client):
        result = transfer_sftp_to_gcs(
            settings=_settings(), remote_dir="/in", bucket_name=bucket.name,
            prefix="batches/196/audio/", **kwargs)
    return result, bucket, client


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_every_matching_file_is_copied_with_its_checksum():
    result, bucket, _ = _run({"a.wav": b"one", "b.wav": b"two"})

    assert len(result.transferred) == 2
    assert bucket.objects["batches/196/audio/a.wav"] == b"one"
    assert result.total_bytes == 6
    # md5("one")
    assert result.transferred[0].content_hash == "f97c5d29941bfb1b2fdab0874906ab82"
    assert result.transferred[0].destination == "gs://recordings/batches/196/audio/a.wav"


def test_the_source_modification_time_is_carried_through():
    result, _, _ = _run({"a.wav": b"one"})
    assert result.transferred[0].modified_at == datetime.fromtimestamp(
        1_700_000_000, tz=timezone.utc).isoformat()


def test_the_session_is_closed_even_when_the_listing_fails():
    client = FakeClient(SimpleNamespace(
        listdir_attr=lambda d: (_ for _ in ()).throw(OSError("no such directory"))))
    with patch("voice_analytics.transfer.service.open_bucket", return_value=FakeBucket()), \
         patch("voice_analytics.transfer.service.open_sftp", return_value=client), \
         pytest.raises(TransferError, match="Could not list"):
        transfer_sftp_to_gcs(settings=_settings(), remote_dir="/in",
                             bucket_name="recordings", prefix="p/")
    assert client.closed, "the SFTP session was left open"


# ---------------------------------------------------------------------------
# What gets skipped, and why
# ---------------------------------------------------------------------------

def test_a_file_a_previous_run_processed_is_skipped():
    result, bucket, _ = _run({"a.wav": b"one", "b.wav": b"two"},
                             already_seen={"a.wav"})
    assert [f.name for f in result.transferred] == ["b.wav"]
    assert result.skipped[0].reason == "already processed by a previous run"
    assert "batches/196/audio/a.wav" not in bucket.objects


def test_a_file_already_at_the_destination_is_not_copied_again():
    bucket = FakeBucket(existing=["batches/196/audio/a.wav"])
    result, _, _ = _run({"a.wav": b"one", "b.wav": b"two"}, bucket=bucket)
    assert [f.name for f in result.transferred] == ["b.wav"]
    assert result.skipped[0].reason == "already present at the destination"


def test_overwrite_copies_it_anyway():
    bucket = FakeBucket(existing=["batches/196/audio/a.wav"])
    result, bucket, _ = _run({"a.wav": b"one"}, bucket=bucket, overwrite=True)
    assert [f.name for f in result.transferred] == ["a.wav"]
    assert bucket.objects["batches/196/audio/a.wav"] == b"one"


def test_the_pattern_filters_by_filename():
    result, _, _ = _run({"a.wav": b"one", "notes.txt": b"x"}, pattern="*.wav")
    assert [f.name for f in result.transferred] == ["a.wav"]


def test_zero_byte_files_are_skipped_as_half_finished_uploads():
    result, _, _ = _run({"a.wav": b"", "b.wav": b"two"})
    assert [f.name for f in result.transferred] == ["b.wav"]


def test_limit_stops_after_n_files():
    result, _, _ = _run({f"{i}.wav": b"x" for i in range(5)}, limit=2)
    assert len(result.transferred) == 2
    assert all("--limit" in s.reason for s in result.skipped)


# ---------------------------------------------------------------------------
# One bad file must not abandon the rest
# ---------------------------------------------------------------------------

def test_an_unreadable_source_file_is_recorded_and_the_run_continues():
    result, bucket, _ = _run({"bad.wav": b"x", "good.wav": b"y"},
                             unreadable={"bad.wav"})
    assert [f.name for f in result.transferred] == ["good.wav"]
    assert [f.name for f in result.failed] == ["bad.wav"]
    assert "batches/196/audio/good.wav" in bucket.objects


def test_a_rejected_upload_is_recorded_and_the_run_continues():
    bucket = FakeBucket(fail_on=["batches/196/audio/bad.wav"])
    result, _, _ = _run({"bad.wav": b"x", "good.wav": b"y"}, bucket=bucket)
    assert [f.name for f in result.transferred] == ["good.wav"]
    assert [f.name for f in result.failed] == ["bad.wav"]


# ---------------------------------------------------------------------------
# Configuration and safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ({"SFTP_HOST": ""}, "SFTP_HOST"),
        ({"SFTP_USERNAME": ""}, "SFTP_USERNAME"),
        ({"SFTP_KNOWN_HOSTS": ""}, "SFTP_KNOWN_HOSTS"),
        ({"SFTP_PASSWORD": ""}, "SFTP_PASSWORD or SFTP_PRIVATE_KEY_PATH"),
    ],
)
def test_incomplete_sftp_configuration_is_reported_before_connecting(missing, expected):
    from voice_analytics.transfer.sftp import open_sftp

    settings = _settings(**missing)
    with pytest.raises(ConfigurationError, match=expected):
        open_sftp(settings)


def test_a_private_key_satisfies_the_credential_requirement():
    settings = _settings(SFTP_PASSWORD="", SFTP_PRIVATE_KEY_PATH="/keys/id_ed25519")
    assert settings.sftp_missing_settings() == []


def test_every_missing_setting_is_reported_at_once():
    """One failed run should tell you everything to fix, not the first thing."""
    settings = Settings(LLM_BASE_URL="https://llm.internal/v1")
    assert settings.sftp_missing_settings() == [
        "SFTP_HOST", "SFTP_USERNAME", "SFTP_KNOWN_HOSTS",
        "SFTP_PASSWORD or SFTP_PRIVATE_KEY_PATH",
    ]


# ---------------------------------------------------------------------------
# Streaming and hashing
# ---------------------------------------------------------------------------

def test_the_hash_is_computed_over_the_whole_stream_in_chunks():
    reader = _HashingReader(io.BytesIO(b"hello world"))
    assert reader.read(5) == b"hello"
    assert reader.read() == b" world"
    assert reader.bytes_read == 11
    assert reader.hexdigest == "5eb63bbbe01eeed093cb22bb8f5acdc3"


def test_an_empty_stream_hashes_to_the_empty_digest():
    reader = _HashingReader(io.BytesIO(b""))
    assert reader.read() == b""
    assert reader.hexdigest == "d41d8cd98f00b204e9800998ecf8427e"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.wav", "audio/x-wav"), ("a.mp3", "audio/mpeg"), ("a.zzz", "application/octet-stream")],
)
def test_content_type_is_guessed_from_the_extension(name, expected):
    assert _content_type(name) in (expected, "audio/wav")


# ---------------------------------------------------------------------------
# The skip list the caller supplies
# ---------------------------------------------------------------------------

def test_a_missing_already_seen_file_means_a_first_run_not_an_error(tmp_path):
    from voice_analytics.cli.transfer import _load_already_seen

    assert _load_already_seen(str(tmp_path / "nope.json")) == set()
    assert _load_already_seen(None) == set()


def test_already_seen_accepts_a_list_or_a_wrapped_object(tmp_path):
    from voice_analytics.cli.transfer import _load_already_seen

    plain = tmp_path / "a.json"
    plain.write_text(json.dumps(["a.wav", "b.wav"]))
    assert _load_already_seen(str(plain)) == {"a.wav", "b.wav"}

    wrapped = tmp_path / "b.json"
    wrapped.write_text(json.dumps({"names": ["c.wav"]}))
    assert _load_already_seen(str(wrapped)) == {"c.wav"}


def test_a_malformed_already_seen_file_is_rejected_clearly(tmp_path):
    from voice_analytics.cli.transfer import _load_already_seen

    bad = tmp_path / "bad.json"
    bad.write_text("not json at all")
    with pytest.raises(ValueError, match="Could not read"):
        _load_already_seen(str(bad))

    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps("a string"))
    with pytest.raises(ValueError, match="JSON list"):
        _load_already_seen(str(wrong))


# ---------------------------------------------------------------------------
# The connection itself, against a stub paramiko
# ---------------------------------------------------------------------------
# This is the security-critical path: an SFTP session carries both the
# credential and the recordings, so the host must be proven before either is
# offered. paramiko is an optional extra, so it is stubbed rather than installed.

import sys                                        # noqa: E402
import types                                       # noqa: E402


class _AuthenticationException(Exception):
    pass


class _SSHException(Exception):
    pass


class _RejectPolicy:
    pass


def _stub_paramiko(connect=None, load_host_keys=None):
    """A paramiko stand-in recording what the real one would have been told."""
    calls = {}

    class SSHClient:
        def load_host_keys(self, path):
            calls["known_hosts"] = path
            if load_host_keys:
                load_host_keys(path)

        def set_missing_host_key_policy(self, policy):
            calls["policy"] = type(policy).__name__

        def connect(self, **kwargs):
            calls["connect"] = kwargs
            if connect:
                connect(**kwargs)

        def close(self):
            calls["closed"] = True

    module = types.SimpleNamespace(
        SSHClient=SSHClient,
        RejectPolicy=_RejectPolicy,
        AuthenticationException=_AuthenticationException,
        SSHException=_SSHException,
    )
    return module, calls


def _with_paramiko(module):
    return patch.dict(sys.modules, {"paramiko": module})


def test_an_unknown_host_is_rejected_rather_than_auto_added():
    """AutoAddPolicy would trust whatever answers on a first connection."""
    from voice_analytics.transfer.sftp import open_sftp

    module, calls = _stub_paramiko()
    with _with_paramiko(module):
        open_sftp(_settings())

    assert calls["policy"] == "_RejectPolicy"
    assert calls["known_hosts"] == "/etc/known_hosts"


def test_the_ssh_agent_and_default_keys_are_not_consulted():
    """Only the configured credential is offered -- never an ambient one."""
    from voice_analytics.transfer.sftp import open_sftp

    module, calls = _stub_paramiko()
    with _with_paramiko(module):
        open_sftp(_settings())

    assert calls["connect"]["allow_agent"] is False
    assert calls["connect"]["look_for_keys"] is False


def test_a_private_key_is_passed_instead_of_a_password():
    from voice_analytics.transfer.sftp import open_sftp

    module, calls = _stub_paramiko()
    with _with_paramiko(module):
        open_sftp(_settings(SFTP_PASSWORD="", SFTP_PRIVATE_KEY_PATH="/keys/id"))

    assert calls["connect"]["key_filename"] == "/keys/id"
    assert calls["connect"]["password"] is None


def test_an_unreadable_known_hosts_file_explains_how_to_make_one():
    from voice_analytics.transfer.sftp import open_sftp

    def boom(path):
        raise OSError("no such file")

    module, _ = _stub_paramiko(load_host_keys=boom)
    with _with_paramiko(module), pytest.raises(ConfigurationError, match="ssh-keyscan"):
        open_sftp(_settings())


def test_a_rejected_credential_says_so_plainly():
    from voice_analytics.transfer.sftp import open_sftp

    def boom(**kwargs):
        raise _AuthenticationException("no")

    module, _ = _stub_paramiko(connect=boom)
    with _with_paramiko(module), pytest.raises(TransferError, match="rejected the credentials"):
        open_sftp(_settings())


def test_a_changed_host_key_is_not_presented_as_something_to_bypass():
    from voice_analytics.transfer.sftp import open_sftp

    def boom(**kwargs):
        raise _SSHException("host key mismatch")

    module, _ = _stub_paramiko(connect=boom)
    with _with_paramiko(module), pytest.raises(TransferError, match="do not bypass"):
        open_sftp(_settings())


def test_an_unreachable_server_reports_host_and_port():
    from voice_analytics.transfer.sftp import open_sftp

    def boom(**kwargs):
        raise OSError("connection refused")

    module, _ = _stub_paramiko(connect=boom)
    with _with_paramiko(module), pytest.raises(TransferError, match="sftp.internal:22"):
        open_sftp(_settings())


def test_a_missing_extra_explains_how_to_install_it():
    from voice_analytics.transfer.sftp import _paramiko

    with patch.dict(sys.modules, {"paramiko": None}), \
         pytest.raises(ConfigurationError, match=r"\[transfer\]"):
        _paramiko()
