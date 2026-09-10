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

from voice_analytics.config import Settings, load_settings
from voice_analytics.exceptions import VoiceAnalyticsError
from voice_analytics.llm import build_llm_client, close_llm_clients
from voice_analytics.schemas import TranscriptionResult, TranscriptSegment
from voice_analytics.transcription import transcribe, translate

__version__ = "0.1.0"

__all__ = [
    "Settings",
    "TranscriptSegment",
    "TranscriptionResult",
    "VoiceAnalyticsError",
    "__version__",
    "build_llm_client",
    "close_llm_clients",
    "load_settings",
    "transcribe",
    "translate",
]
