"""Resolving KPI prompts from the prompt registry.

Caller-side. The analysis library takes an assembled prompt and never reaches
for PromptHub itself.
"""

from voice_analytics.prompthub.client import (
    build_prompthub_client,
    fetch_prompts,
    latest_approved,
    resolve_base_prompt,
    resolve_kpi_prompts,
)

__all__ = [
    "build_prompthub_client",
    "fetch_prompts",
    "latest_approved",
    "resolve_base_prompt",
    "resolve_kpi_prompts",
]
