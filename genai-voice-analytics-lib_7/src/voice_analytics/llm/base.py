"""Shared plumbing for sending audio and text to an LLM gateway.

Every task-specific service (transcription today; summarization, validation and
analysis later) is a thin layer over these two functions. They are plain
module-level functions rather than static methods on a class -- there is no
state to hold, and a function is easier to import, test and mock.

Deliberate change from the original service: no HTML escaping happens here.
The old implementation escaped in this layer, again in the router, and once
more in the response schema, which double-encoded ampersands in real output.
Escaping is a presentation concern and belongs at the boundary that renders,
not in a library that returns data.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from openai import AsyncOpenAI

from voice_analytics.llm.json_repair import normalize_to_dict, parse_agent_output

logger = logging.getLogger(__name__)

#: Sent to the gateway so usage and cost can be attributed.
#:
#: The key is ``service`` and the value is unchanged from the originating
#: service on purpose: LiteLLM dashboards group by these tags, and renaming
#: either would orphan the existing history.
LLM_SERVICE_NAME = "voice-transcription-service"

DEFAULT_METADATA = {"service": LLM_SERVICE_NAME, "mode": "api"}
"""Interactive calls -- one request, a caller waiting."""

BATCH_METADATA = {"service": LLM_SERVICE_NAME, "mode": "batch"}
"""Bulk processing, so batch cost can be separated from interactive."""

_DEFAULT_AUDIO_INSTRUCTION = (
    "Please process this audio according to the system instructions."
)


def _apply_user_override(user_text: str, user_instruction: str | None) -> str:
    """Prepend a caller instruction so it outranks the system prompt.

    Note this is an intentional prompt-injection surface: callers can change
    model behaviour. Only expose it to trusted callers.
    """
    if not user_instruction:
        return user_text
    return (
        "USER OVERRIDE INSTRUCTIONS (Highest Priority):\n"
        f"{user_instruction}\n\n"
        f"{user_text}"
    )


async def process_audio_request(
    client: AsyncOpenAI,
    system_prompt: str,
    content: bytes,
    mime_type: str,
    model_name: str,
    user_instruction: str | None = None,
    llm_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send audio plus a system prompt to the gateway and return parsed JSON.

    ``content`` is raw audio bytes; they are base64-encoded for transport, which
    inflates the payload by roughly a third.
    """
    if not model_name:
        raise ValueError("model_name is required")
    if not content:
        raise ValueError("content is required")

    encoded_audio = base64.b64encode(content).decode("utf-8")
    audio_format = mime_type.split("/")[-1]

    response = await client.chat.completions.create(
        model=model_name,
        response_format={"type": "json_object"},
        metadata=llm_metadata or DEFAULT_METADATA,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _apply_user_override(
                            _DEFAULT_AUDIO_INSTRUCTION, user_instruction
                        ),
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": encoded_audio,
                            "format": audio_format,
                        },
                    },
                ],
            },
        ],
    )

    raw_content = (response.choices[0].message.content or "").strip()
    return normalize_to_dict(parse_agent_output(raw_content))


async def process_text_request(
    client: AsyncOpenAI,
    system_prompt: str,
    text_content: str,
    model_name: str,
    user_instruction: str | None = None,
    is_json: bool = True,
    llm_metadata: dict[str, str] | None = None,
) -> Any:
    """Send text plus a system prompt to the gateway.

    Returns a parsed dict when ``is_json`` is true, otherwise the raw string --
    summaries and translations are prose, not JSON.
    """
    if not model_name:
        raise ValueError("model_name is required")

    response = await client.chat.completions.create(
        model=model_name,
        response_format={"type": "json_object"} if is_json else None,
        metadata=llm_metadata or DEFAULT_METADATA,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": _apply_user_override(
                    f"Data to Analyze:\n{text_content}", user_instruction
                ),
            },
        ],
    )

    raw_content = (response.choices[0].message.content or "").strip()
    if not is_json:
        return raw_content

    return normalize_to_dict(parse_agent_output(raw_content))
