"""Supported target languages for translation.

The 22 scheduled languages of India, keyed by both ISO code and English name so
callers can pass either. Values are the canonical English name, which is what
goes into the prompt -- consistent naming produces better model output than
whatever spelling the caller happened to use.

This is an allow-list: an unrecognised value is rejected rather than passed
through to the model.
"""

from __future__ import annotations

INDIAN_LANGUAGES: dict[str, str] = {
    "hi": "Hindi", "hindi": "Hindi",
    "en": "English", "english": "English",
    "mr": "Marathi", "marathi": "Marathi",
    "bn": "Bengali", "bengali": "Bengali",
    "gu": "Gujarati", "gujarati": "Gujarati",
    "ta": "Tamil", "tamil": "Tamil",
    "te": "Telugu", "telugu": "Telugu",
    "kn": "Kannada", "kannada": "Kannada",
    "ml": "Malayalam", "malayalam": "Malayalam",
    "pa": "Punjabi", "punjabi": "Punjabi",
    "as": "Assamese", "assamese": "Assamese",
    "or": "Odia", "oriya": "Odia", "odia": "Odia",
    "ur": "Urdu", "urdu": "Urdu",
    "ks": "Kashmiri", "kashmiri": "Kashmiri",
    "sd": "Sindhi", "sindhi": "Sindhi",
    "sa": "Sanskrit", "sanskrit": "Sanskrit",
    "ne": "Nepali", "nepali": "Nepali",
    "kok": "Konkani", "konkani": "Konkani",
    "mni": "Manipuri", "manipuri": "Manipuri",
    "doi": "Dogri", "dogri": "Dogri",
    "mai": "Maithili", "maithili": "Maithili",
    "sat": "Santali", "santali": "Santali",
    "brx": "Bodo", "bodo": "Bodo",
}


def normalize_language(value: str | None) -> str | None:
    """Resolve a code or name to its canonical English language name.

    Returns ``None`` for blank input, meaning "no translation requested".
    Raises ``ValueError`` for an unrecognised language so the caller finds out
    immediately rather than receiving an untranslated result.
    """
    if value is None:
        return None

    key = value.strip().lower()
    if not key:
        return None

    if key not in INDIAN_LANGUAGES:
        raise ValueError(
            f"Unsupported language: {value!r}. "
            "Use a code such as 'hi' or a name such as 'Hindi'."
        )
    return INDIAN_LANGUAGES[key]


def supported_languages() -> list[str]:
    """The distinct canonical language names, sorted."""
    return sorted(set(INDIAN_LANGUAGES.values()))
