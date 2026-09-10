"""Configuration primitives. Nothing here is instantiated at import time."""

from voice_analytics.config.settings import (
    DEFAULT_MODEL_NAME,
    DEFAULT_TRANSCRIPTION_MODEL_NAMES,
    Settings,
    load_settings,
)

__all__ = [
    "DEFAULT_MODEL_NAME",
    "DEFAULT_TRANSCRIPTION_MODEL_NAMES",
    "Settings",
    "load_settings",
]
