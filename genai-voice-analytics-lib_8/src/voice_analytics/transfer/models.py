"""What a transfer moved, and what it deliberately did not."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TransferredFile(BaseModel):
    """One file that made it from the source to the destination."""

    name: str = Field(description="Base filename, e.g. call_0001.wav.")
    source_path: str = Field(description="Full path on the SFTP server.")
    destination: str = Field(description="Full gs:// URI it was written to.")
    size_bytes: int = 0
    content_hash: str = Field(
        default="",
        description=(
            "MD5 of the bytes, computed while streaming. This is what lets a "
            "caller skip a file it has already processed even after a rename."
        ),
    )
    modified_at: str | None = Field(
        default=None, description="Source mtime, ISO 8601 UTC."
    )


class SkippedFile(BaseModel):
    """One file that was not transferred, and why."""

    name: str
    reason: str


class TransferResult(BaseModel):
    """The outcome of one SFTP to GCS transfer."""

    transferred: list[TransferredFile] = Field(default_factory=list)
    skipped: list[SkippedFile] = Field(default_factory=list)
    failed: list[SkippedFile] = Field(default_factory=list)
    total_bytes: int = 0
    processing_time_ms: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.transferred
