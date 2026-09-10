"""TLS configuration.

The corporate environment presents certificates signed by an internal CA, so
verification cannot rely on the system trust store alone -- a CA bundle has to
be supplied. These tests use a real, self-signed certificate so that
``ssl.create_default_context`` is genuinely exercised rather than mocked.
"""

from __future__ import annotations

import ssl
import subprocess

import httpx
import pytest

from voice_analytics.config import Settings
from voice_analytics.exceptions import ConfigurationError

BASE = {"_env_file": None, "LLM_BASE_URL": "https://llm.internal/v1"}


@pytest.fixture(scope="module")
def ca_bundle(tmp_path_factory):
    """A genuine self-signed CA certificate, so PEM parsing is really tested."""
    directory = tmp_path_factory.mktemp("tls")
    cert = directory / "corp-ca.pem"
    key = directory / "corp-ca.key"

    result = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-nodes", "-subj", "/CN=Test Corporate CA",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip("openssl is unavailable, so a real CA cannot be generated")

    return cert


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_verification_off_by_default_matching_the_existing_service():
    assert Settings(**BASE).ssl_verify() is False


def test_verification_can_be_enabled_without_a_bundle():
    """Valid when the gateway's CA is already in the system trust store."""
    assert Settings(**BASE, SSL_VERIFY=True).ssl_verify() is True


def test_ca_bundle_produces_an_ssl_context(ca_bundle):
    """A context, not a path: verify=<str> is deprecated in httpx."""
    verify = Settings(**BASE, SSL_VERIFY=True, SSL_CA_BUNDLE=str(ca_bundle)).ssl_verify()

    assert isinstance(verify, ssl.SSLContext)
    assert verify.verify_mode == ssl.CERT_REQUIRED
    assert len(verify.get_ca_certs()) == 1


def test_ca_bundle_implies_verification_even_when_the_flag_is_false(ca_bundle):
    """Supplying a CA is an explicit intent to verify."""
    verify = Settings(
        **BASE, SSL_VERIFY=False, SSL_CA_BUNDLE=str(ca_bundle)
    ).ssl_verify()
    assert isinstance(verify, ssl.SSLContext)


def test_blank_bundle_is_treated_as_unset():
    assert Settings(**BASE, SSL_VERIFY=True, SSL_CA_BUNDLE="").ssl_verify() is True


# ---------------------------------------------------------------------------
# Failure modes -- these must surface at configuration time, not mid-request
# ---------------------------------------------------------------------------


def test_missing_bundle_fails_before_any_request_is_made():
    settings = Settings(**BASE, SSL_VERIFY=True, SSL_CA_BUNDLE="/no/such/ca.pem")
    with pytest.raises(ConfigurationError, match="SSL_CA_BUNDLE does not exist"):
        settings.ssl_verify()


def test_malformed_bundle_reports_a_usable_error(tmp_path):
    bad = tmp_path / "not-a-cert.pem"
    bad.write_text("this is not a certificate", encoding="utf-8")

    settings = Settings(**BASE, SSL_VERIFY=True, SSL_CA_BUNDLE=str(bad))
    with pytest.raises(ConfigurationError, match="not a readable PEM bundle"):
        settings.ssl_verify()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _spy_on_http_client(monkeypatch) -> dict:
    """Capture the kwargs build_llm_client passes to httpx.

    A subclass rather than a function: the OpenAI SDK runs ``isinstance`` on the
    http client, so the replacement has to be a real type.
    """
    captured: dict = {}
    real_client_cls = httpx.AsyncClient

    class _SpyClient(real_client_cls):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("voice_analytics.llm.client.httpx.AsyncClient", _SpyClient)
    return captured


def test_client_passes_the_resolved_context_to_httpx(ca_bundle, monkeypatch):
    captured = _spy_on_http_client(monkeypatch)

    from voice_analytics.llm import build_llm_client

    build_llm_client(
        Settings(
            **BASE, LLM_API_KEY="sk-x", SSL_VERIFY=True, SSL_CA_BUNDLE=str(ca_bundle)
        )
    )

    assert isinstance(captured["verify"], ssl.SSLContext)


def test_client_passes_a_plain_bool_when_no_bundle_is_configured(monkeypatch):
    captured = _spy_on_http_client(monkeypatch)

    from voice_analytics.llm import build_llm_client

    build_llm_client(Settings(**BASE, LLM_API_KEY="sk-x", SSL_VERIFY=False))

    assert captured["verify"] is False
