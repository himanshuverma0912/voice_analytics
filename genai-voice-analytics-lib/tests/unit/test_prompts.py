"""The prompt catalogue must load from the package, not the working directory."""

from __future__ import annotations

import os

import pytest

from voice_analytics.exceptions import PromptNotFoundError
from voice_analytics.prompts import PromptManager, default_prompt_manager


def test_catalogue_loads_from_package_data():
    manager = default_prompt_manager()
    assert "banking" in manager.domains()
    assert "transcription" in manager.tasks("banking")


def test_catalogue_loads_regardless_of_working_directory(tmp_path, monkeypatch):
    """The original implementation only worked from the repository root."""
    monkeypatch.chdir(tmp_path)
    assert os.getcwd() == str(tmp_path)
    assert PromptManager.from_package().get(domain="banking", task="transcription")


def test_transcription_prompt_omits_translation_rule_without_a_language():
    prompt = default_prompt_manager().get(
        domain="banking", task="transcription", target_lang=None, romanize=False
    )
    assert "TRANSLATION RULE" not in prompt
    assert "ROMANIZATION RULE" not in prompt


def test_transcription_prompt_includes_translation_rule_with_a_language():
    prompt = default_prompt_manager().get(
        domain="banking", task="transcription", target_lang="Hindi", romanize=False
    )
    assert "TRANSLATION RULE" in prompt
    assert "Hindi" in prompt


def test_transcription_prompt_includes_romanization_rule_when_requested():
    prompt = default_prompt_manager().get(
        domain="banking", task="transcription", target_lang=None, romanize=True
    )
    assert "ROMANIZATION RULE" in prompt


def test_unknown_task_raises_rather_than_returning_an_empty_prompt():
    with pytest.raises(PromptNotFoundError):
        default_prompt_manager().get(domain="banking", task="does-not-exist")


def test_unknown_domain_raises():
    with pytest.raises(PromptNotFoundError):
        default_prompt_manager().get(domain="insurance", task="transcription")


def test_from_file_rejects_a_missing_path(tmp_path):
    with pytest.raises(PromptNotFoundError):
        PromptManager.from_file(tmp_path / "absent.yaml")


def test_from_file_loads_a_caller_supplied_catalogue(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text("retail:\n  greeting: |\n    Hello {{ name }}\n", encoding="utf-8")
    manager = PromptManager.from_file(path)
    assert manager.get(domain="retail", task="greeting", name="Priya") == "Hello Priya"
