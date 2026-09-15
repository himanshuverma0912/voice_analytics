"""Moving recordings from an SFTP server into object storage.

Needs the ``transfer`` extra: ``pip install 'genai-voice-analytics-lib[transfer]'``.
The SSH and cloud-storage libraries are imported lazily so the core library --
and every pod that only transcribes or scores -- stays free of them.
"""

from voice_analytics.transfer.models import (
    SkippedFile,
    TransferredFile,
    TransferResult,
)
from voice_analytics.transfer.service import transfer_sftp_to_gcs

__all__ = [
    "SkippedFile",
    "TransferResult",
    "TransferredFile",
    "transfer_sftp_to_gcs",
]
