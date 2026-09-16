"""Writing what the transcription stage produced."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def seed_transcriptions(
    conn: Any, batch_id: int, files: list[dict]
) -> list[dict]:
    """Create one 'pending' row per file, before any work starts.

    Rows exist up front so the batch's file count is right from the beginning
    and a file whose pod never runs still leaves a visible row, rather than a
    silently missing one.

    Args:
        files: ``{"name": ..., "file_path": ...}`` per recording. ``file_path``
            is wherever the audio actually is -- a ``gs://`` URI from a pod, a
            local path from a laptop.

    Returns:
        The same entries with ``transcription_id`` added.
    """
    seeded: list[dict] = []
    with conn.cursor() as cur:
        for entry in files:
            cur.execute(
                """
                INSERT INTO transcriptions
                       (batch_id, filename, file_path, status, transcript_metadata,
                        created_at)
                VALUES (%s, %s, %s, 'pending', '{}'::jsonb, now())
                RETURNING id
                """,
                (batch_id, entry["name"], entry.get("file_path")),
            )
            seeded.append({**entry, "transcription_id": int(cur.fetchone()[0])})

        cur.execute(
            """
            UPDATE transcription_batches
               SET total_files=%s, pending_files=%s, completed_files=0,
                   processing_files=0, failed_files_count=0, updated_at=now()
             WHERE id=%s
            """,
            (len(seeded), len(seeded), batch_id),
        )

    return seeded


def persist_transcript(conn: Any, transcription_id: int, result: dict) -> None:
    """Write one completed transcription.

    ``result`` is the JSON the ``transcribe`` command wrote. Note the language
    is read from ``primary_language``: the originating service read
    ``detected_language``, a key nothing ever produced, so that column was NULL
    on every audio row in production.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transcriptions
               SET transcript            = %s,
                   translated_transcript = %s,
                   detected_language     = %s,
                   processing_time_ms    = %s,
                   status                = 'completed',
                   error_message         = NULL,
                   updated_at            = now()
             WHERE id = %s
            """,
            (
                result.get("transcript"),
                result.get("translated_transcript"),
                result.get("primary_language"),
                result.get("processing_time_ms"),
                transcription_id,
            ),
        )


def mark_transcription_failed(conn: Any, transcription_id: int, reason: str) -> None:
    """Record that one file could not be transcribed.

    The transcript columns are left NULL rather than written with partial
    output: a half-transcript that looks like a result is worse than none.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transcriptions
               SET status='failed', error_message=%s, updated_at=now()
             WHERE id=%s
            """,
            (reason[:1000], transcription_id),
        )


def transcription_tally(conn: Any, batch_id: int) -> dict:
    """How the batch's files ended up, and update the batch's counters to match.

    Returns:
        ``{"total": n, "completed": n, "failed": n, "failed_pct": n}``.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE status='completed'),
                   count(*) FILTER (WHERE status='failed')
              FROM transcriptions WHERE batch_id=%s
            """,
            (batch_id,),
        )
        total, completed, failed = cur.fetchone()

        cur.execute(
            """
            UPDATE transcription_batches
               SET completed_files=%s, failed_files_count=%s,
                   pending_files=0, processing_files=0, updated_at=now()
             WHERE id=%s
            """,
            (completed, failed, batch_id),
        )

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "failed_pct": round(100 * failed / total) if total else 0,
    }


def completed_transcriptions(conn: Any, batch_id: int) -> list[dict]:
    """The files that transcribed successfully, in insertion order.

    These are what the scoring stage runs over. A failed file is not scored:
    there is nothing to score.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, filename, transcript
              FROM transcriptions
             WHERE batch_id=%s AND status='completed'
             ORDER BY id
            """,
            (batch_id,),
        )
        return [
            {"transcription_id": int(r[0]), "filename": r[1], "transcript": r[2]}
            for r in cur.fetchall()
        ]
