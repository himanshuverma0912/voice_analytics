"""The two things the voice analytics flow extracts beyond KPI scores.

Both descriptions are ported byte-for-byte from the originating service. They
read like documentation because they *are* the prompt: the rules, the negative
cases and the worked examples are all instructions to the model, and editing
them for tidiness changes the output.

Both are best-effort. A call where the agent never says their name has no agent
name, and that is a fact about the call rather than a failure -- so each
returns ``None`` rather than raising.
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

from voice_analytics.extraction.models import ExtractionResult, KeywordDefinition
from voice_analytics.extraction.service import extract_keywords
from voice_analytics.observability import say

logger = logging.getLogger(__name__)

MAX_TOPICS = 3

TOPICS_KEYWORD = KeywordDefinition(
    keyword="topics",
    description="""
    Identify the 3 most prominent discussion topics from this call transcript.

    Rules:
    - Return exactly 3 topics whenever the transcript contains at least 3 distinct discussion points.
      If fewer than 3 genuine topics exist, return only those that are actually discussed.
    - Each topic must be a short lowercase phrase (1-3 words).
    - Topics must be the exact term or a near-exact phrase used in the transcript. Do not paraphrase
      into broader concepts or invent new terminology.
    - Focus on what the call is actually about: products/services, customer requests, financial terms,
      issues/complaints, processes/actions.
    - Prefer specific topics over generic ones (e.g. "credit limit" not "banking").
    - Do not include greetings, confirmations, filler conversation, or agent actions unless they are
      the primary purpose of the call.
    - Deduplicate similar topics; keep the most specific phrasing.
    - Order topics by importance, with the primary reason for the call first.
    """,
)

AGENT_NAME_KEYWORD = KeywordDefinition(
    keyword="agent_name",
    description="""
    Extract the name of the bank agent (caller/representative) who initiates this call.

    LOOK FOR these self-introduction patterns:
    - English: "I am [Name] calling from...", "This is [Name] from...", "My name is [Name]"
    - Hindi: "मैं [Name] बात कर रही/रहा हूं", "मैं [Name] बोल रहा/रही हूं", "[Name] here from HDFC"
    - Kannada/Telugu/Tamil or other Indian languages: Agent stating their own name followed by bank name or "calling from"
    - Any pattern where the CALLER states their OWN name while identifying themselves as a bank representative

    STRICT RULES:
    - Only extract a name if the agent explicitly states it as their own name during self-introduction
    - The agent name is typically given at the very beginning of the call (first 30 seconds)
    - Do NOT extract the customer's name (the person being called)
    - Do NOT extract names mentioned mid-conversation or in third-person references
    - Do NOT infer or guess a name from greetings directed AT someone (e.g., "Hello Shireesha ma'am" — Shireesha is the CUSTOMER)
    - If the agent's name is unclear or never stated, return found=false

    EXAMPLES (from Indian bank calls):
    - "मैं HDFC बैंक से नंदी बात कर रही थी" → agent_name = "Nandi"
    - "ದೇವಿ ರಶ್ಮಿ ಬ್ಯಾಂಕಿಂದ ಕಾಲ್ ಮಾಡ್ತಾ ಇರೋದು" → agent_name = "Devi Rashmi"
    - "Hi, I'm Priya calling from HDFC" → agent_name = "Priya"
    - Agent never states their name → found=false
    """,
)


async def extract_topics(
    client: AsyncOpenAI,
    transcript: str,
    model_name: str,
    llm_metadata: dict[str, str] | None = None,
) -> list[str] | None:
    """The three things this call was about, or ``None``.

    Two response shapes are handled, because the model produces both:

    * **canonical** -- one match named ``topics`` whose ``extracted_values``
      hold the phrases;
    * **keyword-frequency** -- one match per phrase it spotted, which is
      ranked by ``count``.

    Returns:
        Up to three lowercase phrases in importance order, or ``None`` when the
        transcript yielded nothing usable.
    """
    result = await extract_keywords(
        client=client, text=transcript, keywords=[TOPICS_KEYWORD],
        model_name=model_name, llm_metadata=llm_metadata,
    )
    return topics_from(result)


def topics_from(result: ExtractionResult) -> list[str] | None:
    """Pull topics out of an extraction result. Pure, so it is testable."""
    found = result.found_matches
    if not found:
        return None

    # Canonical shape. Requires more than one distinct value: a single value
    # is usually the model echoing the keyword back rather than answering.
    for match in found:
        if match.keyword == "topics" and match.extracted_values:
            distinct = list(dict.fromkeys(
                value.strip().lower()
                for value in match.extracted_values
                if value and value.strip()
            ))
            if len(distinct) > 1:
                return distinct[:MAX_TOPICS]

    # Fallback: the model split its answer into one match per phrase.
    ranked = sorted(found, key=lambda m: m.count or 0, reverse=True)
    topics: list[str] = []
    seen: set[str] = set()
    for match in ranked:
        topic = (match.keyword or "").strip().lower()
        if topic and topic != "topics" and topic not in seen:
            seen.add(topic)
            topics.append(topic)
        if len(topics) == MAX_TOPICS:
            break

    return topics or None


async def extract_agent_name(
    client: AsyncOpenAI,
    transcript: str,
    model_name: str,
    llm_metadata: dict[str, str] | None = None,
) -> str | None:
    """The agent's name, if they introduced themselves.

    Returns:
        The name, or ``None`` when it was never stated. The prompt is written
        to prefer ``None`` over a guess -- naming the wrong agent on a call is
        worse than naming nobody, because agent-level reports aggregate on it.
    """
    result = await extract_keywords(
        client=client, text=transcript, keywords=[AGENT_NAME_KEYWORD],
        model_name=model_name, llm_metadata=llm_metadata,
    )
    name = agent_name_from(result)
    if name is None:
        say(logger,
            "The agent never introduced themselves by name on this call, so "
            "no agent name was recorded.",
            agent_name=None)
    return name


def agent_name_from(result: ExtractionResult) -> str | None:
    """Pull the agent name out of an extraction result. Pure, so it is testable."""
    for match in result.matches:
        if match.keyword == "agent_name" and match.found and match.extracted_values:
            name = match.extracted_values[0].strip()
            if name:
                return name
    return None
