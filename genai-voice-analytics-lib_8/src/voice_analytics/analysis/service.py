"""Scoring a transcript against a set of KPIs.

The public entry point is :func:`analyse`. It takes an already-assembled
prompt, sends the transcript to the gateway, and returns validated scores
grouped by objective.

The prompt is assembled once per batch by the caller -- see
:func:`voice_analytics.analysis.build_consolidated_prompt`. Passing it in
rather than resolving it here means one PromptHub request per batch instead of
one per call.
"""

from __future__ import annotations

import asyncio
import logging
import time

from openai import (
    APIConnectionError,
    APIError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from voice_analytics.analysis.models import AnalysisResult
from voice_analytics.analysis.scoring import (
    apply_evidence_rule,
    parse_call_impact,
    parse_experience_drivers,
    parse_kpis,
    parse_risk,
    roll_up_by_objective,
)
from voice_analytics.exceptions import (
    AnalysisError,
    LLMAuthenticationError,
    LLMOutputParsingError,
    LLMRateLimitError,
    LLMServiceError,
)
from voice_analytics.llm.base import process_text_request
from voice_analytics.observability import human_duration, say

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
"""Retries after the first attempt, so three attempts in total."""

BASE_RETRY_DELAY_SECONDS = 5
"""Backoff doubles each attempt: 5s, then 10s."""


def _retry_delay(attempt: int) -> int:
    return BASE_RETRY_DELAY_SECONDS * (2**attempt)


def _user_content(transcript: str, agent_name: str | None) -> str:
    """Frame the transcript for the model, naming the agent when known."""
    prefix = f"Agent Name: {agent_name}\n\n" if agent_name else ""
    return f"{prefix}Analyze this call transcript using the system instructions:\n\n{transcript}"


async def analyse(
    client: AsyncOpenAI,
    transcript: str,
    consolidated_prompt: str,
    model_name: str,
    agent_name: str | None = None,
    llm_metadata: dict[str, str] | None = None,
) -> AnalysisResult:
    """Score a transcript against the KPIs baked into ``consolidated_prompt``.

    Args:
        client: Gateway client from :func:`voice_analytics.llm.build_llm_client`.
        transcript: The call transcript. Scoring reads the **source-language**
            transcript; a translation is for reviewers, not for scoring.
        consolidated_prompt: Base instructions plus one block per KPI, from
            :func:`build_consolidated_prompt`.
        model_name: Model to use. Unlike the originating service, this is not
            hardcoded -- that version ignored both its own parameter and the
            configured model name.
        agent_name: Included in the prompt when known, so KPIs can refer to the
            agent by name.
        llm_metadata: Tags for usage attribution at the gateway.

    Returns:
        An :class:`AnalysisResult` with per-KPI scores and a per-objective
        rollup. **There is no overall score** -- see the class docstring.

    Raises:
        ValueError: ``transcript``, ``consolidated_prompt`` or ``model_name``
            is blank.
        LLMAuthenticationError: The gateway rejected the credential.
        LLMRateLimitError: Still rate-limited after all retries.
        LLMServiceError: Gateway unreachable or failing after all retries.
        AnalysisError: The response could not be parsed into scores.
    """
    started = time.perf_counter()

    if not model_name:
        raise ValueError("model_name is required")
    if not transcript or not transcript.strip():
        raise ValueError("transcript is required")
    if not consolidated_prompt or not consolidated_prompt.strip():
        raise ValueError("consolidated_prompt is required")

    say(logger,
        "Scoring %d characters of transcript against the configured KPIs "
        "using model '%s'...",
        len(transcript), model_name,
        model=model_name, transcript_chars=len(transcript),
        prompt_chars=len(consolidated_prompt), agent_name=agent_name,
    )

    raw = await _call_gateway_with_retries(
        client=client,
        system_prompt=consolidated_prompt,
        user_content=_user_content(transcript, agent_name),
        model_name=model_name,
        llm_metadata=llm_metadata,
    )

    kpis = parse_kpis(raw)
    if not kpis:
        # An empty result looks like a successful run, which is worse than a
        # failure -- a call would be recorded as scored when nothing was judged.
        say(logger,
            "The AI returned no KPI scores at all.",
            level=logging.ERROR,
            response_keys=sorted(raw) if isinstance(raw, dict) else "non-dict",
        )
        raise AnalysisError(
            "The model returned no KPI scores. The prompt may not match the "
            "expected output format."
        )

    unevidenced = apply_evidence_rule(kpis)
    rollups = roll_up_by_objective(kpis, unevidenced)

    block = raw if isinstance(raw, dict) else {}
    result = AnalysisResult(
        kpis=kpis,
        by_objective=rollups,
        risk=parse_risk(block.get("risk_summary")),
        call_impact=parse_call_impact(block.get("call_impact")),
        customer_experience_drivers=parse_experience_drivers(
            block.get("customer_experience_drivers")
        ),
        unevidenced_kpis=unevidenced,
        processing_time_ms=round((time.perf_counter() - started) * 1000, 2),
    )

    scored = sum(row.scored for row in rollups)
    say(logger,
        "Scored %d of %d KPIs across %d objective(s) in %s.",
        scored, len(kpis), len(rollups),
        human_duration(result.processing_time_ms),
        kpis_total=len(kpis), kpis_scored=scored,
        objectives=[row.objective for row in rollups],
        unevidenced=len(unevidenced),
        elapsed_ms=result.processing_time_ms,
    )

    if unevidenced:
        say(logger,
            "%d KPI(s) gave a score without quoting the transcript, so those "
            "scores were not counted: %s",
            len(unevidenced), ", ".join(unevidenced),
            level=logging.WARNING,
            unevidenced_kpis=unevidenced,
        )

    return result


async def _call_gateway_with_retries(
    client: AsyncOpenAI,
    system_prompt: str,
    user_content: str,
    model_name: str,
    llm_metadata: dict[str, str] | None,
) -> dict:
    """Send the scoring request, retrying transient failures with backoff."""
    total_attempts = MAX_RETRIES + 1

    for attempt in range(total_attempts):
        attempt_started = time.perf_counter()

        try:
            raw = await process_text_request(
                client=client,
                system_prompt=system_prompt,
                text_content=user_content,
                model_name=model_name,
                is_json=True,
                llm_metadata=llm_metadata,
            )
            say(logger,
                "AI service responded in %s.",
                human_duration((time.perf_counter() - attempt_started) * 1000),
                attempt=attempt + 1, of=total_attempts,
                elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                response_keys=sorted(raw) if isinstance(raw, dict) else "non-dict",
            )
            return raw

        except AuthenticationError as exc:
            say(logger,
                "The AI service rejected our credentials. Check that "
                "LLM_API_KEY is set correctly for this environment. "
                "Not retrying -- a rejected key will not start working.",
                level=logging.ERROR,
                error_type=type(exc).__name__, retryable=False, exit_code=5,
            )
            raise LLMAuthenticationError(
                "Invalid API key or gateway credential"
            ) from exc

        except RateLimitError as exc:
            if attempt < MAX_RETRIES:
                delay = _retry_delay(attempt)
                say(logger,
                    "The AI service is busy (too many requests). Waiting %d "
                    "seconds, then trying again (attempt %d of %d).",
                    delay, attempt + 2, total_attempts,
                    level=logging.WARNING,
                    attempt=attempt + 1, of=total_attempts,
                    retry_in_seconds=delay, retryable=True,
                )
                await asyncio.sleep(delay)
                continue

            say(logger,
                "The AI service is still too busy after %d attempts. Try again "
                "later.", total_attempts,
                level=logging.ERROR, attempts=total_attempts, exit_code=6,
            )
            raise LLMRateLimitError(
                "Gateway rate limit exceeded after all retries"
            ) from exc

        except (APIError, APIConnectionError) as exc:
            if attempt < MAX_RETRIES:
                delay = _retry_delay(attempt)
                say(logger,
                    "The AI service did not respond properly (%s). Waiting %d "
                    "seconds, then trying again (attempt %d of %d).",
                    type(exc).__name__, delay, attempt + 2, total_attempts,
                    level=logging.WARNING,
                    error_type=type(exc).__name__, error=str(exc)[:200],
                    attempt=attempt + 1, of=total_attempts,
                    retry_in_seconds=delay, retryable=True,
                )
                await asyncio.sleep(delay)
                continue

            say(logger,
                "The AI service failed after %d attempts.", total_attempts,
                level=logging.ERROR,
                error_type=type(exc).__name__, error=str(exc)[:300],
                attempts=total_attempts, exit_code=7,
            )
            raise LLMServiceError("Gateway failed after all retries") from exc

        except LLMOutputParsingError as exc:
            say(logger,
                "The AI's reply could not be understood. Not retrying.",
                level=logging.ERROR,
                error=exc.message[:300], retryable=False, exit_code=8,
            )
            raise AnalysisError(
                f"Could not parse the scoring response: {exc.message}"
            ) from exc

    raise LLMServiceError("Scoring exhausted all retries")
