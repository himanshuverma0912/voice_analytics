"""JSON recovery from the shapes models actually emit."""

from __future__ import annotations

import pytest

from voice_analytics.exceptions import LLMOutputParsingError
from voice_analytics.llm.json_repair import (
    extract_json_block,
    extract_json_from_text,
    fix_json_control_characters,
    normalize_to_dict,
    parse_agent_output,
    repair_malformed_json,
)


def test_extract_json_block_unwraps_markdown_fence():
    assert extract_json_block('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_extract_json_block_returns_none_without_a_fence():
    assert extract_json_block('{"a": 1}') is None


def test_extract_json_from_text_finds_embedded_object():
    assert extract_json_from_text('Sure! {"a": 1} Hope that helps.') == '{"a": 1}'


def test_extract_json_from_text_finds_embedded_array():
    assert extract_json_from_text("Result: [1, 2]") == "[1, 2]"


def test_extract_json_from_text_returns_none_when_absent():
    assert extract_json_from_text("no json at all") is None


def test_repair_malformed_json_closes_truncated_containers():
    assert repair_malformed_json('{"a": [1, 2') == '{"a": [1, 2]}'


def test_fix_json_control_characters_escapes_newlines_inside_strings():
    assert fix_json_control_characters('{"a": "x\ny"}') == '{"a": "x\\ny"}'


def test_fix_json_control_characters_leaves_structure_untouched():
    assert fix_json_control_characters('{\n  "a": 1\n}') == '{\n  "a": 1\n}'


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Here is the result:\n{"a": 1}',
        '{"a": 1',
    ],
)
def test_parse_agent_output_handles_common_model_habits(raw):
    assert parse_agent_output(raw) == {"a": 1}


@pytest.mark.parametrize("raw", ["", "   ", "no json here"])
def test_parse_agent_output_raises_rather_than_returning_an_error_dict(raw):
    """The original returned {"error": ...}, which callers routinely forgot to check."""
    with pytest.raises(LLMOutputParsingError):
        parse_agent_output(raw)


def test_normalize_to_dict_passes_through_a_dict():
    assert normalize_to_dict({"a": 1}) == {"a": 1}


def test_normalize_to_dict_unwraps_a_single_element_array():
    assert normalize_to_dict([{"a": 1}]) == {"a": 1}


@pytest.mark.parametrize("value", [None, [], ["string"], 42])
def test_normalize_to_dict_rejects_unusable_shapes(value):
    with pytest.raises(LLMOutputParsingError):
        normalize_to_dict(value)
