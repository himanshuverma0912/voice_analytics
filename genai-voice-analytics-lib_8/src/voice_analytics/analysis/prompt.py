"""Assembling the scoring prompt from a base prompt and per-KPI instructions.

Pure: takes prompt text, returns prompt text. Fetching those prompts from
PromptHub is the caller's job -- see :mod:`voice_analytics.prompthub`.

That split matches the originating service, where ``pipeline.py`` resolves the
prompts once per batch and passes the assembled result down to every worker.
Resolving per call would mean 12,480 PromptHub requests for a batch that needs
one.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from jinja2 import Environment, FunctionLoader, select_autoescape

_PACKAGE_DATA = "voice_analytics.analysis.data"
_TEMPLATE_NAME = "kpi_prompt_template.j2"


@dataclass(frozen=True, slots=True)
class KPIPrompt:
    """One KPI's scoring instructions.

    Attributes:
        kpi_code: Stable identifier, e.g. ``"rpc_verified"``. This is what the
            model must echo back so scores can be matched to definitions.
        kpi_name: Human-readable title shown in the prompt.
        instructions: The prompt text, typically resolved from PromptHub.
    """

    kpi_code: str
    kpi_name: str
    instructions: str


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """Jinja environment loading the template from package data.

    ``FunctionLoader`` rather than ``FileSystemLoader`` so the template resolves
    from inside the wheel, from any working directory.
    """

    def load(_name: str) -> str:
        return (
            resources.files(_PACKAGE_DATA)
            .joinpath(_TEMPLATE_NAME)
            .read_text(encoding="utf-8")
        )

    return Environment(
        loader=FunctionLoader(load),
        autoescape=select_autoescape(disabled_extensions=("j2",)),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build_consolidated_prompt(
    base_prompt: str,
    kpi_prompts: list[KPIPrompt],
) -> str:
    """Combine the base instructions with one block per KPI.

    The result is a single system prompt: the base rules -- output format,
    scoring scale, evidence requirement -- followed by a delimited block for
    each KPI being scored.

    Args:
        base_prompt: Shared instructions covering output shape and rules.
        kpi_prompts: The KPIs to score, in the order they should appear.

    Returns:
        The assembled system prompt.

    Raises:
        ValueError: ``base_prompt`` is blank, or ``kpi_prompts`` is empty.
            Scoring against nothing would produce plausible but meaningless
            output, so it is refused rather than allowed to proceed.
    """
    if not base_prompt or not base_prompt.strip():
        raise ValueError("base_prompt is required")
    if not kpi_prompts:
        raise ValueError(
            "At least one KPI is required. Scoring with no KPIs would return "
            "an empty result that looks like a successful run."
        )

    template = _environment().get_template(_TEMPLATE_NAME)
    return template.render(
        input_data=base_prompt,
        kpis=[
            {
                "enabled": True,
                "title": prompt.kpi_name or prompt.kpi_code,
                "instructions": prompt.instructions,
                "data": {"kpi_code": prompt.kpi_code},
            }
            for prompt in kpi_prompts
        ],
    )
