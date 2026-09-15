"""Writing what the scoring and extraction stages produced.

Four tables, in order, because each depends on the last::

    batches -> calls -> call_analyses -> analysis_kpi_results

`batches` is the **analysis** batch, and is not `transcription_batches`. Both
are called "batch", both carry counters, and their ids are different types --
`batches.id` is a UUID, `transcription_batches.id` a bigint. They are joined by
`batches.transcription_batch_id`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from voice_analytics_store.connection import StoreError

logger = logging.getLogger(__name__)


def open_analysis_batch(
    conn: Any,
    transcription_batch_id: int,
    usecase_id: str | None,
    usecase_name: str | None,
    name: str | None = None,
) -> str:
    """Create the `batches` row that this run's scoring results hang from.

    Every ``NOT NULL`` column is supplied explicitly rather than left to a
    default. The schema export that this was written against carries no
    ``DEFAULT`` clauses, so relying on one would be relying on something
    unverified.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO batches (usecase_id, usecase_name, name, status,
                                 transcription_batch_id,
                                 total_calls, pending_calls, processing_calls,
                                 successful_calls, failed_calls,
                                 uploaded_at, is_deleted)
            VALUES (%s, %s, %s, 'processing', %s,
                    0, 0, 0, 0, 0,
                    -- uploaded_at is `timestamp` without a zone while
                    -- created_at is `timestamptz`. Converting explicitly keeps
                    -- the value UTC whatever the session's TimeZone is.
                    (now() AT TIME ZONE 'UTC'), false)
            RETURNING id
            """,
            (
                usecase_id,
                usecase_name,
                name or f"Analysis - batch {transcription_batch_id}",
                transcription_batch_id,
            ),
        )
        return str(cur.fetchone()[0])


def kpi_code_to_id(conn: Any) -> dict[str, Any]:
    """Map every KPI code to its row id.

    `analysis_kpi_results` references a KPI by UUID, not by code, so a score
    cannot be stored without this.

    ``kpis.kpi_code`` is nullable and carries no unique constraint, so neither
    can be assumed. A NULL would key the map on ``None``; a duplicate would
    resolve to whichever row was read last, attaching a score to the wrong KPI
    -- which is worse than losing it.

    Raises:
        StoreError: A code appears on more than one row.
    """
    mapping: dict[str, Any] = {}
    duplicates: set[str] = set()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT kpi_code, id FROM kpis WHERE kpi_code IS NOT NULL ORDER BY created_at"
        )
        for code, kpi_id in cur.fetchall():
            if code in mapping:
                duplicates.add(code)
            mapping[code] = kpi_id

    if duplicates:
        raise StoreError(
            f"{len(duplicates)} KPI code(s) appear on more than one row in the "
            f"kpis table: {', '.join(sorted(duplicates))}. Scores cannot be "
            "attributed until that is resolved, and guessing would attach them "
            "to the wrong KPI."
        )
    return mapping


def overall_score(kpis: list[dict]) -> float | None:
    """The mean of the scored, applicable KPIs -- or ``None`` if none were.

    An **unweighted** mean, which is what the originating service computed.
    ``kpis.weight`` and ``kpi_sections.weight`` both exist and are ignored by
    it. Reproducing that keeps old and new rows comparable; changing it is a
    product decision, and should be made once rather than drifting.

    The library itself produces no overall score: the wireframe shows KPIs
    grouped by objective and never a single headline number, and a shared
    library is the wrong place to fix a weighting policy still being decided.
    """
    scored = [
        k["score"] for k in kpis
        if k.get("score") is not None and k.get("applicable", True)
    ]
    return round(sum(scored) / len(scored), 2) if scored else None


def persist_analysis(
    conn: Any,
    analysis_batch_id: str,
    transcription_id: int,
    filename: str,
    transcript: str | None,
    analysis: dict,
    extracted: dict | None,
    kpi_ids: dict[str, Any],
    usecase_id: str | None = None,
    usecase_name: str | None = None,
    transcription_batch_id: int | None = None,
) -> dict:
    """Write one scored call: its `calls`, `call_analyses` and KPI rows.

    Args:
        analysis: the JSON the ``analyse`` command wrote.
        extracted: the JSON the ``extract`` command wrote, or ``None``. Genuinely
            optional -- an extraction failure costs two fields, not a call.
        kpi_ids: from :func:`kpi_code_to_id`.

    Returns:
        ``{"call_id": ..., "analysis_id": ..., "kpis_stored": n,
           "unknown_codes": [...]}``.
    """
    extracted = extracted or {}
    impact = analysis.get("call_impact") or {}
    risk = analysis.get("risk") or {}
    kpis = analysis.get("kpis") or []

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO calls (batch_id, filename, transcript, duration_sec,
                               status, external_call_id, metadata,
                               uploaded_at, is_deleted)
            VALUES (%s, %s, %s, %s, 'completed', %s, %s::jsonb,
                    (now() AT TIME ZONE 'UTC'), false)
            RETURNING id
            """,
            (
                analysis_batch_id, filename, transcript,
                analysis.get("duration_sec"), str(transcription_id),
                json.dumps({
                    "transcription_id": transcription_id,
                    "transcription_batch_id": transcription_batch_id,
                    "usecase_id": usecase_id,
                    "usecase_name": usecase_name,
                    # Agent-level reports aggregate on this key, so it is
                    # written whether or not a name was found.
                    "agent_name": extracted.get("agent_name"),
                }),
            ),
        )
        call_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO call_analyses (
                call_id, batch_id, status, overall_score,
                high_risk_call, manual_review_required, compliance_violation,
                privacy_violation, mis_selling_alert, risk_detail,
                topics, call_impact_level, call_impact_reason,
                customer_experience_drivers, is_deleted, analyzed_at
            ) VALUES (%s,%s,'completed',%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,
                      false, (now() AT TIME ZONE 'UTC'))
            RETURNING id
            """,
            (
                call_id, analysis_batch_id, overall_score(kpis),
                risk.get("high_risk_call", False),
                risk.get("manual_review_required", False),
                risk.get("compliance_violation", False),
                risk.get("privacy_violation", False),
                risk.get("mis_selling_alert", False),
                json.dumps({
                    "risk_reason": risk.get("risk_reason", {}),
                    "risk_evidence": risk.get("risk_evidence", {}),
                }) if risk else None,
                json.dumps(extracted["topics"]) if extracted.get("topics") else None,
                # Constrained to High/Low by a check constraint; the library
                # already normalises anything else to None.
                impact.get("level"),
                impact.get("reason"),
                json.dumps(analysis.get("customer_experience_drivers") or []),
            ),
        )
        analysis_id = cur.fetchone()[0]

        stored, unknown = 0, []
        for kpi in kpis:
            code = kpi.get("kpi_code")
            if code not in kpi_ids:
                # The originating service skipped these silently, so a KPI the
                # model scored could vanish with nothing on the record.
                unknown.append(code)
                continue
            cur.execute(
                """
                -- "references" is quoted because REFERENCES is a reserved word
                -- in SQL; unquoted, this statement is a syntax error.
                INSERT INTO analysis_kpi_results (
                    analysis_id, kpi_id, raw_score, score, rationale,
                    positive_impact, negative_impact,
                    observable, applicable, attempted, "references"
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    analysis_id, kpi_ids[code], kpi.get("raw_score"),
                    kpi.get("score"), kpi.get("rationale"),
                    kpi.get("positive_impact"), kpi.get("negative_impact"),
                    kpi.get("observable", True), kpi.get("applicable", True),
                    kpi.get("attempted", False),
                    json.dumps([
                        {"turn_index": e.get("turn_index"),
                         "speaker": e.get("speaker"),
                         "quote": e.get("quote")}
                        for e in (kpi.get("evidence") or [])
                    ]),
                ),
            )
            stored += 1

    return {
        "call_id": str(call_id),
        "analysis_id": str(analysis_id),
        "kpis_stored": stored,
        "unknown_codes": unknown,
    }


def close_analysis_batch(conn: Any, analysis_batch_id: str) -> dict:
    """Set the analysis batch's counters from the rows that were written."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE status='completed'),
                   count(*) FILTER (WHERE status='failed')
              FROM call_analyses WHERE batch_id=%s AND is_deleted IS FALSE
            """,
            (analysis_batch_id,),
        )
        total, completed, failed = cur.fetchone()

        cur.execute(
            """
            UPDATE batches
               SET status=%s, total_calls=%s, successful_calls=%s, failed_calls=%s,
                   pending_calls=0, processing_calls=0, updated_at=now()
             WHERE id=%s
            """,
            (
                "completed" if failed == 0 else ("failed" if completed == 0 else "partial"),
                total, completed, failed, analysis_batch_id,
            ),
        )

    return {"total": total, "completed": completed, "failed": failed}
