"""Resolving KPI prompts from PromptHub.

A **caller-side helper**, not part of the scoring path. The analysis library
takes an already-assembled prompt; this module is what a caller uses to build
one, and it is deliberately importable on its own so a consumer that stores
prompts elsewhere never touches it.

Only ``APPROVED`` versions are ever returned. A prompt sitting in review is
ignored in favour of the last approved one -- in a regulated setting the
wording of a compliance check is a controlled artefact, and that control is
enforced here rather than assumed.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from voice_analytics.analysis.prompt import KPIPrompt
from voice_analytics.config import Settings
from voice_analytics.exceptions import ConfigurationError, PromptHubError
from voice_analytics.observability import say

logger = logging.getLogger(__name__)

BASE_PROMPT_NAME = "base_prompt"

#: Status a prompt version must carry to be usable.
APPROVED = "APPROVED"


def build_prompthub_client(settings: Settings) -> httpx.AsyncClient:
    """Create a client for PromptHub, sharing the gateway's TLS configuration.

    Raises:
        ConfigurationError: ``PROMPTHUB_BASE_URL`` is not set.
    """
    if not settings.PROMPTHUB_BASE_URL:
        raise ConfigurationError("PROMPTHUB_BASE_URL is not configured.")

    return httpx.AsyncClient(
        verify=settings.ssl_verify(),
        timeout=settings.PROMPTHUB_TIMEOUT_SECONDS,
    )


async def fetch_prompts(
    client: httpx.AsyncClient,
    settings: Settings,
    litellm_api_key: str,
) -> list[dict[str, Any]]:
    """Fetch every prompt visible to ``litellm_api_key``.

    One request per batch, not per call.

    Raises:
        ValueError: ``litellm_api_key`` is blank.
        PromptHubError: PromptHub was unreachable, rejected the request, or
            returned a response without a prompt list.
    """
    if not litellm_api_key:
        raise ValueError("litellm_api_key is required to fetch prompts")

    url = f"{settings.PROMPTHUB_BASE_URL.rstrip('/')}{settings.PROMPTHUB_PROMPTS_PATH}"

    try:
        response = await client.get(
            url, headers={"accept": "*/*", "lite-llm-api-key": litellm_api_key}
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as exc:
        raise PromptHubError(
            f"PromptHub returned HTTP {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise PromptHubError(f"PromptHub is unreachable: {exc}") from exc
    except ValueError as exc:
        raise PromptHubError("PromptHub returned a response that is not JSON") from exc

    if not body.get("success", False):
        raise PromptHubError(f"PromptHub reported failure: {body.get('message')}")

    prompts = (body.get("data") or {}).get("prompts")
    if prompts is None:
        raise PromptHubError("PromptHub response contains no prompt list")

    say(logger, "Fetched %d prompt version(s) from PromptHub.", len(prompts),
        url=url, count=len(prompts))
    return prompts


def latest_approved(prompts: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The highest approved version of one prompt, or ``None``.

    A version in review is ignored: the last approved wording continues to be
    used until someone approves the new one.
    """
    approved = [
        entry
        for entry in prompts
        if entry.get("name") == name and entry.get("status") == APPROVED
    ]
    if not approved:
        return None
    return max(approved, key=lambda entry: entry.get("version", 0))


def resolve_base_prompt(
    prompts: list[dict[str, Any]],
    fallback_prompts: list[dict[str, Any]] | None = None,
) -> str:
    """The shared instructions every KPI block is appended to.

    Args:
        prompts: Prompts for the active use case.
        fallback_prompts: A default use case's prompts. A use case may define
            only KPI prompts and rely on the default for the shared rules --
            the originating service supported exactly that, and without it
            those use cases cannot be scored at all.

    Raises:
        PromptHubError: Neither source has an approved base prompt. Fatal --
            without it the model has no output format or scoring rules to
            follow, so it would return plausible nonsense rather than fail.
    """
    entry = latest_approved(prompts, BASE_PROMPT_NAME)

    if not entry and fallback_prompts:
        entry = latest_approved(fallback_prompts, BASE_PROMPT_NAME)
        if entry:
            say(logger,
                "This use case has no base prompt of its own, so the default "
                "use case's is being used.",
                level=logging.WARNING, source="default_usecase")

    if not entry:
        raise PromptHubError(
            f"No approved '{BASE_PROMPT_NAME}' found in PromptHub"
            + (", nor in the default use case." if fallback_prompts else ".")
            + " Scoring cannot run without the shared instructions."
        )
    return entry["value"]


def resolve_kpi_prompts(
    prompts: list[dict[str, Any]],
    kpi_codes: list[str],
    fallback_prompts: list[dict[str, Any]] | None = None,
) -> tuple[list[KPIPrompt], list[str]]:
    """Resolve each KPI code to its approved prompt.

    Args:
        prompts: Prompts for the active use case.
        kpi_codes: The KPIs selected for this batch, in display order.
        fallback_prompts: A default use case's prompts, consulted when a KPI has
            no definition of its own.

    Returns:
        ``(resolved, skipped)`` -- the prompts found, and the codes that had no
        approved definition anywhere.

    Note:
        A missing KPI is **skipped, not fatal**, matching the originating
        service. The caller is given the skipped codes so it can record that the
        batch was scored against fewer KPIs than were selected -- something the
        original only wrote to a log, which made two batches silently
        incomparable.
    """
    resolved: list[KPIPrompt] = []
    skipped: list[str] = []

    for code in kpi_codes:
        entry = latest_approved(prompts, code)
        if not entry and fallback_prompts:
            entry = latest_approved(fallback_prompts, code)

        if not entry:
            skipped.append(code)
            continue

        resolved.append(
            KPIPrompt(
                kpi_code=code,
                kpi_name=entry.get("name", code),
                instructions=entry["value"],
            )
        )

    if skipped:
        say(logger,
            "%d KPI(s) have no approved prompt and will not be scored: %s",
            len(skipped), ", ".join(skipped),
            level=logging.WARNING, skipped_kpis=skipped,
        )

    return resolved, skipped
