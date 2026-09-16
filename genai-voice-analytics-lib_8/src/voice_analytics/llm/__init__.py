"""LLM gateway client and shared request plumbing."""

from voice_analytics.llm.base import (
    BATCH_METADATA,
    DEFAULT_METADATA,
    LLM_SERVICE_NAME,
    process_audio_request,
    process_text_request,
)
from voice_analytics.llm.client import build_llm_client, close_llm_clients
from voice_analytics.llm.json_repair import normalize_to_dict, parse_agent_output

__all__ = [
    "BATCH_METADATA",
    "DEFAULT_METADATA",
    "LLM_SERVICE_NAME",
    "build_llm_client",
    "close_llm_clients",
    "normalize_to_dict",
    "parse_agent_output",
    "process_audio_request",
    "process_text_request",
]
