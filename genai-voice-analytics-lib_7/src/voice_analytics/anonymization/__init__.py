"""Removing personal information from text.

Enforces the ordering that keeps PII out of storage and out of downstream
services: transcribe -> **anonymize** -> translate.
"""

from voice_analytics.anonymization.client import build_anonymization_client
from voice_analytics.anonymization.service import anonymize, extract_anonymized_text

__all__ = ["anonymize", "build_anonymization_client", "extract_anonymized_text"]
