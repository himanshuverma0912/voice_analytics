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


# ---------------------------------------------------------------------------
# One .env file must configure the database as well as the gateway
# ---------------------------------------------------------------------------
# Half-working configuration is worse than none: the gateway connects, the run
# starts, and the failure arrives at the persistence step minutes later.

def test_the_dsn_is_read_from_a_dotenv_file(tmp_path, monkeypatch):
    from voice_analytics_store.connection import DSN_VARIABLES, dsn_from_env

    for name in DSN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("VOICE_ANALYTICS_DSN=postgresql://localhost/from_file\n")

    assert dsn_from_env(str(env_file)) == "postgresql://localhost/from_file"


def test_a_real_environment_variable_beats_the_dotenv_file(tmp_path, monkeypatch):
    """So a one-off override on the command line still works."""
    from voice_analytics_store.connection import dsn_from_env

    env_file = tmp_path / ".env"
    env_file.write_text("VOICE_ANALYTICS_DSN=postgresql://localhost/from_file\n")
    monkeypatch.setenv("VOICE_ANALYTICS_DSN", "postgresql://localhost/from_env")

    assert dsn_from_env(str(env_file)) == "postgresql://localhost/from_env"


def test_any_of_the_three_variable_names_is_accepted(tmp_path, monkeypatch):
    from voice_analytics_store.connection import DSN_VARIABLES, dsn_from_env

    for name in DSN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/via_database_url")

    assert dsn_from_env(str(tmp_path / "missing.env")).endswith("via_database_url")


def test_a_missing_dsn_names_every_variable_it_would_accept(tmp_path, monkeypatch):
    from voice_analytics_store.connection import DSN_VARIABLES, StoreError, dsn_from_env
    import pytest

    for name in DSN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(StoreError) as exc:
        dsn_from_env(str(tmp_path / "missing.env"))

    for name in DSN_VARIABLES:
        assert name in str(exc.value)
    assert ".env" in str(exc.value)


def test_run_local_finds_settings_in_a_dotenv_file(tmp_path, monkeypatch):
    """The runner's precheck must accept what its subcommands accept."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "run_local", Path(__file__).resolve().parents[2] / "scripts" / "run_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_BASE_URL=http://gateway.internal/v1\n")

    assert module._configured("LLM_BASE_URL", str(env_file)) == "http://gateway.internal/v1"
    assert module._configured("NOT_SET_ANYWHERE", str(env_file)) is None


# ---------------------------------------------------------------------------
# Model entitlement, as PromptHub expresses it
# ---------------------------------------------------------------------------
# The only place per-use-case model access is enforced anywhere in the system.
# Ported from prompthub_client.find_model_entry.

def _entitlement():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "fetch_usecase_key.py"
    spec = importlib.util.spec_from_file_location("fetch_usecase_key", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_model_with_a_key_is_usable():
    module = _entitlement()
    usecase = {"models": [{"modelName": "gemini-2.5-flash", "llmApiKey": "sk-abc"}]}
    assert module.find_model_entry(usecase, "gemini-2.5-flash")["llmApiKey"] == "sk-abc"


def test_the_model_name_is_matched_case_and_space_insensitively():
    module = _entitlement()
    usecase = {"models": [{"modelName": "  Gemini-2.5-Flash ", "llmApiKey": "sk-abc"}]}
    assert module.find_model_entry(usecase, "gemini-2.5-flash") is not None


def test_withdrawn_access_is_refused_even_though_a_key_is_present():
    """removeTokenModelAccess means access was taken away, not never granted."""
    module = _entitlement()
    usecase = {"models": [{"modelName": "gemini-2.5-flash", "llmApiKey": "sk-abc",
                           "removeTokenModelAccess": True}]}
    assert module.find_model_entry(usecase, "gemini-2.5-flash") is None


def test_a_model_entry_with_no_key_is_refused():
    module = _entitlement()
    usecase = {"models": [{"modelName": "gemini-2.5-flash", "llmApiKey": ""}]}
    assert module.find_model_entry(usecase, "gemini-2.5-flash") is None


def test_a_model_that_is_not_listed_is_refused():
    module = _entitlement()
    usecase = {"models": [{"modelName": "something-else", "llmApiKey": "sk-abc"}]}
    assert module.find_model_entry(usecase, "gemini-2.5-flash") is None


def test_no_models_at_all_is_refused_rather_than_crashing():
    module = _entitlement()
    assert module.find_model_entry({}, "gemini-2.5-flash") is None
    assert module.find_model_entry({"models": None}, "gemini-2.5-flash") is None


def test_a_key_is_masked_enough_to_recognise_but_not_to_use():
    module = _entitlement()
    assert module.mask("sk-abcdef1234567890wxyz") == "sk-abc...wxyz"
    assert module.mask("short") == "****"
    assert module.mask("") == "(none)"


def test_writing_the_key_to_env_replaces_any_existing_line(tmp_path):
    module = _entitlement()
    env = tmp_path / ".env"
    env.write_text("LLM_BASE_URL=https://x/v1\nLLM_API_KEY=sk-old\nMODEL_NAME=m\n")

    module._write_env(str(env), "sk-new")

    lines = env.read_text().splitlines()
    assert "LLM_API_KEY=sk-new" in lines
    assert "LLM_API_KEY=sk-old" not in lines
    assert "LLM_BASE_URL=https://x/v1" in lines      # other settings survive
    assert oct(env.stat().st_mode)[-3:] == "600"     # not world-readable
