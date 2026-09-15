"""Reading a batch's configuration, and recording how it ended."""

from __future__ import annotations

import json
import logging
from typing import Any

from voice_analytics_store.connection import StoreError

logger = logging.getLogger(__name__)


def read_batch_config(conn: Any, batch_id: int) -> dict:
    """Everything one pipeline run needs from its `transcription_batches` row.

    ``source`` is read from the **column**, not from ``metadata['source']``.
    Those two disagree in production: the metadata records how the files
    arrived (``'zip_pipeline_upload'``), while the column keeps its default
    (``'audio_upload'``) and is what routes the pipeline.

    Raises:
        StoreError: No such batch.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT metadata, usecase_id, usecase_name, source, total_files, status
              FROM transcription_batches
             WHERE id = %s
            """,
            (batch_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise StoreError(f"No transcription_batches row with id={batch_id}")

    metadata, usecase_id, usecase_name, source, total_files, status = row
    metadata = metadata or {}

    # `usecase_id` is nullable in the schema but not optional in the pipeline:
    # it is what PromptHub resolves prompts and model entitlement against.
    # Without it the registry would be asked for use case "None", which
    # returns a 404 several tasks later -- long after the batch looked healthy.
    if not usecase_id:
        raise StoreError(
            f"Batch {batch_id} has no usecase_id. The pipeline cannot resolve "
            "prompts or model access without one, so it will not start. "
            "Whatever created this batch should have set it."
        )

    return {
        "batch_id": int(batch_id),
        "usecase_id": str(usecase_id) if usecase_id is not None else None,
        "usecase_name": usecase_name,
        "source": source,
        "status": status,
        "total_files": total_files,
        "target_lang": metadata.get("target_language"),
        "romanize": bool(metadata.get("romanize", False)),
        "sftp_dir": metadata.get("sftp_dir") or metadata.get("source_path"),
        "file_pattern": metadata.get("file_pattern"),
        "incremental": bool(metadata.get("incremental", False)),
    }


def selected_kpi_codes(conn: Any, batch_id: int) -> list[str]:
    """The KPI codes this batch is to be scored against.

    Resolved through the config **pinned to this batch**, never from the use
    case's current active config. ``usecase_kpi_configs`` is versioned, so
    reading the latest would score this batch against a different KPI set than
    the batch it is being compared with -- with nothing in the data to show
    why the two disagree.

    The ``ORDER BY`` is not decoration: the codes go into the prompt in this
    order, and a reordered prompt is a different prompt. The originating query
    had none, so the order varied between runs.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.kpi_code
              FROM batch_kpi_configs       bkc
              JOIN usecase_kpi_selections  sel ON sel.config_id = bkc.usecase_kpi_config_id
              JOIN kpis                    k   ON k.id = sel.kpi_id
             WHERE bkc.batch_id = %s
               AND sel.is_selected IS TRUE
               AND k.kpi_code IS NOT NULL
             ORDER BY k.display_order, k.kpi_code
            """,
            (batch_id,),
        )
        return [row[0] for row in cur.fetchall()]


def mark_batch_processing(conn: Any, batch_id: int) -> None:
    """Move the batch to 'processing' as the run starts."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE transcription_batches SET status='processing', updated_at=now() "
            "WHERE id=%s",
            (batch_id,),
        )


def record_skipped_kpis(conn: Any, batch_id: int, skipped: list[str]) -> None:
    """Record KPIs that had no approved prompt and so were not scored.

    A missing KPI is skipped rather than fatal, matching the originating
    service. But that service only logged it, which made two batches silently
    incomparable -- one scored against 22 KPIs, the next against 19, with
    nothing on the record to say so.
    """
    if not skipped:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transcription_batches
               SET metadata = coalesce(metadata, '{}'::jsonb) || %s::jsonb,
                   updated_at = now()
             WHERE id = %s
            """,
            (json.dumps({"kpi_codes_skipped": skipped}), batch_id),
        )


def clear_file_paths(conn: Any, batch_id: int) -> None:
    """Null every `file_path` after the recordings have been deleted.

    Half of what makes "0 recordings retained" true -- the other half is
    deleting the objects themselves, which is storage, not database.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE transcriptions SET file_path=NULL, updated_at=now() WHERE batch_id=%s",
            (batch_id,),
        )


def finalise_batch(conn: Any, batch_id: int) -> dict:
    """Set the batch's final status from what its rows actually say.

    Counted from `transcriptions` rather than trusting whatever the last
    successful task wrote, so a run that died midway still ends with counters
    that add up.

    Returns:
        ``{"status": ..., "completed": n, "failed": n}``.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE status='completed'),
                   count(*) FILTER (WHERE status='failed')
              FROM transcriptions WHERE batch_id=%s
            """,
            (batch_id,),
        )
        completed, failed = cur.fetchone()

        status = "completed" if failed == 0 else ("failed" if completed == 0 else "partial")
        cur.execute(
            """
            UPDATE transcription_batches
               SET status=%s, completed_files=%s, failed_files_count=%s,
                   pending_files=0, processing_files=0, updated_at=now()
             WHERE id=%s
            """,
            (status, completed, failed, batch_id),
        )

    return {"status": status, "completed": completed, "failed": failed}


def mark_pipeline_failed(
    conn: Any, batch_id: int, failed_task: str, stage: str = "transcription"
) -> None:
    """Mark the batch **and its rows** failed.

    Reproduces `POST /analytics/pipeline/mark-failed`, including the part its
    name does not suggest: it marks the pending and processing rows failed,
    not just the batch. Marking only the batch is the failure mode that looks
    fine on a dashboard -- the batch is red, every file still says it is
    waiting its turn, and the counters never add up.

    Args:
        stage: ``'analysis'`` updates `call_analyses` for the analytics batch;
            anything else updates `transcriptions`.
    """
    error_message = f"Pipeline failed at task '{failed_task}'."

    with conn.cursor() as cur:
        if stage == "analysis":
            cur.execute(
                """
                SELECT id FROM batches
                 WHERE transcription_batch_id = %s AND is_deleted IS FALSE
                 ORDER BY created_at DESC LIMIT 1
                """,
                (batch_id,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE call_analyses
                       SET status='failed', error_message=%s, updated_at=now()
                     WHERE batch_id=%s AND is_deleted IS FALSE
                       AND status IN ('pending','processing')
                    """,
                    (error_message, row[0]),
                )
                cur.execute(
                    """
                    UPDATE batches b
                       SET status='failed',
                           successful_calls = (SELECT count(*) FROM call_analyses
                                                WHERE batch_id=b.id AND status='completed'),
                           failed_calls     = (SELECT count(*) FROM call_analyses
                                                WHERE batch_id=b.id AND status='failed'),
                           pending_calls=0, processing_calls=0, updated_at=now()
                     WHERE b.id=%s
                    """,
                    (row[0],),
                )
        else:
            cur.execute(
                """
                UPDATE transcriptions
                   SET status='failed', error_message=%s, updated_at=now()
                 WHERE batch_id=%s AND status IN ('pending','processing')
                """,
                (error_message, batch_id),
            )

        cur.execute(
            """
            UPDATE transcription_batches b
               SET status     = 'failed',
                   metadata   = coalesce(metadata, '{}'::jsonb) || %s::jsonb,
                   completed_files    = (SELECT count(*) FROM transcriptions
                                          WHERE batch_id=b.id AND status='completed'),
                   failed_files_count = (SELECT count(*) FROM transcriptions
                                          WHERE batch_id=b.id AND status='failed'),
                   pending_files=0, processing_files=0, updated_at=now()
             WHERE b.id = %s
            """,
            (json.dumps({"failed_task": failed_task, "failed_stage": stage}), batch_id),
        )
