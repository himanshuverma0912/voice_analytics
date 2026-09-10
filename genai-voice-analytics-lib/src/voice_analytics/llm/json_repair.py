"""Recovering usable JSON from unreliable model output.

Extracted from the monolithic ``utils.py`` in the original service. That module
also held PromptHub authentication, which forced it to import application
settings -- and that single import was enough to couple the whole LLM chain to
application configuration. Keeping these helpers standalone is what lets the
transcription stack be config-free.

Models fail at JSON in predictable ways: wrapping it in markdown fences,
prefixing prose, truncating mid-object, or embedding raw newlines inside
strings. Each helper here handles one of those.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from json_repair import repair_json

from voice_analytics.exceptions import LLMOutputParsingError

logger = logging.getLogger(__name__)

#: Control characters that are illegal inside a JSON string literal.
ESCAPE_MAP = {
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def extract_json_block(content: str) -> str | None:
    """Pull JSON out of a markdown code fence, if there is one.

    Handles the very common ```` ```json { ... } ``` ```` wrapper.
    Returns ``None`` when the content has no fenced block.
    """
    if "```" not in content:
        return None

    for part in content.split("```"):
        block = part.strip()

        if block.startswith("json"):
            block = block[4:].strip()

        if block.startswith("{") or block.startswith("["):
            return block

    return None


def extract_json_from_text(content: str) -> str | None:
    """Find the outermost JSON object or array embedded in free text.

    For output such as ``Here is the result: {"a": 1}`` this returns
    ``{"a": 1}``. Returns ``None`` when no opening brace or bracket is present.
    """
    first_object = content.find("{")
    first_array = content.find("[")

    if first_object == -1 and first_array == -1:
        return None

    if first_object != -1 and (first_array == -1 or first_object < first_array):
        start_idx = first_object
        end_idx = content.rfind("}")
    else:
        start_idx = first_array
        end_idx = content.rfind("]")

    if end_idx != -1 and end_idx > start_idx:
        return content[start_idx : end_idx + 1]
    return content[start_idx:]


def repair_missing_object_closure(json_str: str) -> str:
    """Close array items the model left open, turning ``", {`` into ``"}, {``."""
    return json_str.replace('", {', '"}, {')


def repair_malformed_json(json_str: str) -> str:
    """Append the closing braces and brackets a truncated response is missing.

    Walks the string tracking open containers, then closes whatever is still
    open. Deliberately avoids regex -- the nesting has to be counted.
    """
    json_str = repair_missing_object_closure(json_str).strip()

    stack: list[str] = []
    for char in json_str:
        if char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in ("}", "]") and stack and stack[-1] == char:
            stack.pop()

    while stack:
        json_str += stack.pop()

    return json_str


def fix_json_control_characters(json_string: str) -> str:
    """Escape raw control characters that appear inside string literals.

    Models frequently emit a literal newline inside a quoted string, which is
    invalid JSON. This tracks whether the scanner is inside a string (honouring
    backslash escapes) and replaces control characters only where they are
    illegal.
    """
    result: list[str] = []
    in_string = False
    escape_next = False

    for char in json_string:
        if escape_next:
            result.append(char)
            escape_next = False
            continue

        if char == "\\":
            result.append(char)
            escape_next = True
            continue

        if char == '"':
            in_string = not in_string
            result.append(char)
            continue

        result.append(ESCAPE_MAP.get(char, char) if in_string else char)

    return "".join(result)


def parse_agent_output(agent_output: str) -> dict[str, Any] | list[Any]:
    """Best-effort parse of raw model output into JSON.

    Applies the repair steps in order, then parses. Raises
    ``LLMOutputParsingError`` when the content cannot be salvaged.

    Note this differs from the original implementation, which returned
    ``{"error": ...}`` on failure and left every caller to remember to check
    for it. Raising makes failure impossible to ignore.
    """
    if not agent_output or not agent_output.strip():
        raise LLMOutputParsingError("Model returned an empty response")

    content = agent_output.strip()

    json_content = extract_json_block(content) or extract_json_from_text(content)
    if not json_content:
        raise LLMOutputParsingError("No JSON object or array found in model response")

    json_content = repair_malformed_json(json_content.strip())
    json_content = fix_json_control_characters(json_content)

    try:
        return json.loads(json_content)
    except json.JSONDecodeError as exc:
        logger.warning("Direct JSON parse failed, attempting repair: %s", exc)

    try:
        return json.loads(repair_json(json_content))
    except Exception as exc:  # noqa: BLE001 - repair_json raises broadly
        logger.error(
            "Unable to parse model response. First 2000 characters:\n%s",
            content[:2000],
        )
        raise LLMOutputParsingError(
            f"Unable to parse model response as JSON: {exc}"
        ) from exc


def normalize_to_dict(parsed: Any) -> dict[str, Any]:
    """Coerce a parsed response into a dict.

    Models asked for an object sometimes return a single-element array. Rather
    than failing, unwrap it -- and reject anything that is neither shape.
    """
    if parsed is None:
        raise LLMOutputParsingError("Model returned a null response")

    if isinstance(parsed, dict):
        return parsed

    if isinstance(parsed, list):
        if not parsed:
            raise LLMOutputParsingError("Model returned an empty list")
        first = parsed[0]
        if isinstance(first, dict):
            logger.warning("Model returned an array; using its first object.")
            return first
        raise LLMOutputParsingError(
            f"Model returned a list whose first item is {type(first).__name__}, not an object"
        )

    raise LLMOutputParsingError(
        f"Unsupported model response type: {type(parsed).__name__}"
    )
