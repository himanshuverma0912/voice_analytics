"""The shipped prompts must stay byte-for-byte what the models were tuned against.

A prompt is not source code that can be tidied. Reflowing a line, fixing a
typo or stripping a trailing space changes the tokens the model sees, and the
effect shows up as a quiet change in scores rather than as a failure.

These tests pin the catalogue's contents. A deliberate change means updating
the expected digest here, which puts the decision in the diff where a reviewer
can see it.
"""

from __future__ import annotations

import hashlib

import pytest

from voice_analytics.prompts import default_prompt_manager

#: sha256 of each prompt as shipped. Regenerate ONLY when changing a prompt on
#: purpose, and say why in the commit message.
EXPECTED_DIGESTS = {
    "direct_audio_summarization": "f4cf22a95ca25e705b28645d5f68bdf987747df659a154e249f7eff7f96c8dcb",
    "keyword_extraction":         "ac11ef58d0e3cad2c4435856087c760aa72a70a601060686b5eb07ec600fd4b4",
    "summarize_bullet_points":    "ffab0d9e63af2965d9794dc67d8da850714d7e1738c3aded8a9452274fa2d52a",
    "summarize_key_insights":     "5b8b7afb94d8af88d27789fcd9b433a0fbf600752965eda88914fe78a7d762d0",
    "summarize_short_summary":    "ba009a841a53b6b3f94f1673298cf241d1cffd11c41c7e7b48fca14f34307922",
    "transcription":              "a753e4f3540e55c7d4d36a2c0c704258f32173acb7dbece3f146d91b0b42055f",
    "translation":                "64067839f73d34c13a1560b92fe092494289eba75078039467033dd35982d200",
}


def _raw(task: str) -> str:
    """The prompt before Jinja rendering, so placeholders are still in it."""
    return default_prompt_manager()._catalogue["banking"][task]


def test_every_expected_task_is_present():
    """A task disappearing from the catalogue must fail loudly, not at runtime."""
    assert set(default_prompt_manager().tasks("banking")) == set(EXPECTED_DIGESTS)


@pytest.mark.parametrize("task", sorted(EXPECTED_DIGESTS))
def test_prompt_is_unchanged(task):
    digest = hashlib.sha256(_raw(task).encode("utf-8")).hexdigest()
    assert digest == EXPECTED_DIGESTS[task], (
        f"The '{task}' prompt changed. If that was deliberate, update its digest "
        f"in EXPECTED_DIGESTS to {digest} and say why in the commit message. "
        "If it was not, revert it -- prompt edits change model output."
    )
