"""Proof that response parsing matches the originating service exactly.

The anonymization response has no stable shape, so the originating service
searches for the anonymized user message rather than reading a fixed path.
That search is a compliance control: if it fails to find the text, the pipeline
must stop rather than continue with the original.

Reimplementing a search like that from reading it is how subtle differences get
introduced -- a key checked in a different order, a recursion that stops one
level early. So instead of trusting the reimplementation, the original is
reproduced verbatim below and both are run over the same inputs, including the
awkward shapes that separate a faithful port from an approximate one.

Source: `_extract_anonymized_content`, src/routers/pipeline.py:74-106.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from voice_analytics.anonymization.service import extract_anonymized_text
from voice_analytics.exceptions import AnonymizationError


# ---------------------------------------------------------------------------
# The original, copied verbatim. Do not tidy it -- its value is being identical.
# ---------------------------------------------------------------------------

def _original(response_body: Any) -> str:
    """Return the anonymized user message from the auxiliary-service response."""
    def content_from_messages(messages: Any) -> Optional[str]:
        if not isinstance(messages, list):
            return None
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
        return None

    def find_messages(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            for key in ("messages", "anonymized_messages", "anonymizedMessages"):
                content = content_from_messages(value.get(key))
                if content:
                    return content
            for nested_value in value.values():
                content = find_messages(nested_value)
                if content:
                    return content
        elif isinstance(value, list):
            return content_from_messages(value)
        return None

    content = find_messages(response_body)
    if content:
        return content
    raise ValueError("Anonymization service response contains no anonymized user message")


CLEAN = "The customer called about account [REDACTED]."

#: Shapes chosen so a plausible-but-wrong reimplementation fails at least one.
BODIES = [
    pytest.param({"messages": [{"role": "user", "content": CLEAN}]}, id="top-level"),
    pytest.param({"anonymized_messages": [{"role": "user", "content": CLEAN}]}, id="snake-case"),
    pytest.param({"anonymizedMessages": [{"role": "user", "content": CLEAN}]}, id="camel-case"),
    pytest.param([{"role": "user", "content": CLEAN}], id="bare-list"),
    pytest.param({"data": {"messages": [{"role": "user", "content": CLEAN}]}}, id="nested-once"),
    pytest.param({"a": {"b": {"c": {"messages": [{"role": "user", "content": CLEAN}]}}}},
                 id="nested-deeply"),
    pytest.param({"result": [{"role": "user", "content": CLEAN}]}, id="list-under-unknown-key"),
    pytest.param(
        {"messages": [
            {"role": "system", "content": "ignore me"},
            {"role": "assistant", "content": "ignore me too"},
            {"role": "user", "content": CLEAN},
        ]},
        id="user-is-last",
    ),
    pytest.param(
        {"messages": [
            {"role": "user", "content": "   "},
            {"role": "user", "content": CLEAN},
        ]},
        id="first-user-message-is-blank",
    ),
    pytest.param({"messages": ["junk", {"role": "user", "content": CLEAN}]},
                 id="non-dict-entry-in-list"),
    pytest.param({"messages": [{"role": "user", "content": 42}],
                  "other": [{"role": "user", "content": CLEAN}]},
                 id="non-string-content-then-a-real-one"),
    # Order matters: both keys present, and the original checks "messages" first.
    pytest.param(
        {"anonymizedMessages": [{"role": "user", "content": "WRONG"}],
         "messages": [{"role": "user", "content": CLEAN}]},
        id="key-precedence",
    ),
]

EMPTY_BODIES = [
    pytest.param({}, id="empty-object"),
    pytest.param([], id="empty-list"),
    pytest.param(None, id="null"),
    pytest.param("a string", id="string"),
    pytest.param({"messages": []}, id="no-messages"),
    pytest.param({"messages": [{"role": "assistant", "content": CLEAN}]}, id="no-user-role"),
    pytest.param({"messages": [{"role": "user", "content": "   "}]}, id="blank-content"),
    pytest.param({"messages": [{"role": "user"}]}, id="content-missing"),
    pytest.param({"messages": [{"role": "user", "content": None}]}, id="content-null"),
    pytest.param({"messages": "not a list"}, id="messages-not-a-list"),
]


@pytest.mark.parametrize("body", BODIES)
def test_finds_the_same_text_as_the_originating_service(body):
    assert extract_anonymized_text(body) == _original(body) == CLEAN


@pytest.mark.parametrize("body", EMPTY_BODIES)
def test_both_refuse_the_same_unusable_responses(body):
    """Neither may fall back to the original text -- that is the whole control."""
    with pytest.raises(ValueError):
        _original(body)
    with pytest.raises(AnonymizationError):
        extract_anonymized_text(body)


def test_the_error_names_the_risk_rather_than_the_mechanics():
    """The message is read by whoever is paged at 2am, not by its author."""
    with pytest.raises(AnonymizationError, match="personal information"):
        extract_anonymized_text({})
