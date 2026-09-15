"""genai-voice-analytics-lib -- reusable voice analytics building blocks.

Typical use::

    from voice_analytics import build_llm_client, load_settings, transcribe

    settings = load_settings()
    client = build_llm_client(settings, api_key="sk-...")

    result = await transcribe(
        client=client,
        content=audio_bytes,
        mime_type="audio/wav",
        model_name="gemini-2.5-flash-transcription",
        target_lang="Hindi",
    )
    print(result.transcript)

Nothing in this package reads configuration at import time. Build a ``Settings``
object explicitly and pass it in.
"""

from voice_analytics.analysis import (
    AnalysisResult,
    KPIPrompt,
    analyse,
    build_consolidated_prompt,
)
from voice_analytics.anonymization import anonymize, build_anonymization_client
from voice_analytics.config import Settings, load_settings
from voice_analytics.exceptions import VoiceAnalyticsError
from voice_analytics.extraction import (
    ExtractionResult,
    KeywordDefinition,
    extract_agent_name,
    extract_keywords,
    extract_topics,
)
from voice_analytics.llm import build_llm_client, close_llm_clients
from voice_analytics.schemas import (
    SummaryResult,
    TranscriptionResult,
    TranscriptSegment,
)
from voice_analytics.summarization import SummaryFormat, summarize_audio, summarize_text
from voice_analytics.transcription import transcribe, translate

__version__ = "0.12.3"

__all__ = [
    "AnalysisResult",
    "ExtractionResult",
    "KPIPrompt",
    "KeywordDefinition",
    "Settings",
    "SummaryFormat",
    "SummaryResult",
    "TranscriptSegment",
    "TranscriptionResult",
    "VoiceAnalyticsError",
    "__version__",
    "analyse",
    "anonymize",
    "build_anonymization_client",
    "build_consolidated_prompt",
    "build_llm_client",
    "close_llm_clients",
    "extract_agent_name",
    "extract_agent_name",
    "extract_keywords",
    "extract_keywords",
    "extract_topics",
    "extract_topics",
    "load_settings",
    "summarize_audio",
    "summarize_text",
    "transcribe",
    "translate",
]
