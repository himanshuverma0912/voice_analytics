"""The persistence layer, against a real Postgres.

Covers what a mock cannot: that the SQL parses, the columns exist, the
constraints hold, and a full run leaves rows that add up.
"""

from __future__ import annotations

import json

import pytest

from voice_analytics_store import (
    close_analysis_batch,
    completed_transcriptions,
    finalise_batch,
    kpi_code_to_id,
    mark_batch_processing,
    mark_pipeline_failed,
    mark_transcription_failed,
    open_analysis_batch,
    persist_analysis,
    persist_transcript,
    read_batch_config,
    record_skipped_kpis,
    seed_transcriptions,
    selected_kpi_codes,
    transcription_tally,
)
from voice_analytics_store.connection import StoreError


TRANSCRIPT_JSON = {
    "transcript": "[00:02] [Agent]: Namaste.\n[00:47] [Customer]: Haan ji.",
    "translated_transcript": "[00:02] Greetings.\n[00:47] Yes.",
    "primary_language": "Hindi",
    "processing_time_ms": 1234.5,
}

ANALYSIS_JSON = {
    "kpis": [
        {"kpi_code": "rpc_verified", "section": "Compliance", "score": 8.0,
         "raw_score": "Yes", "rationale": "Verified.", "applicable": True,
         "observable": True, "attempted": True,
         "evidence": [{"turn_index": 1, "speaker": "Agent", "quote": "Namaste."}]},
        {"kpi_code": "disclosure_given", "section": "Compliance", "score": 6.0,
         "rationale": "Partly.", "applicable": True, "observable": True,
         "attempted": True, "evidence": []},
    ],
    "by_objective": [{"objective": "Compliance", "total": 2, "scored": 2,
                      "not_applicable": 0, "unevidenced": 0, "average": 7.0}],
    "risk": {"high_risk_call": True, "manual_review_required": False,
             "compliance_violation": False, "privacy_violation": False,
             "mis_selling_alert": False, "risk_reason": {"x": "y"},
             "risk_evidence": {}},
    "call_impact": {"level": "High", "reason": "Escalation risk."},
    "customer_experience_drivers": ["long hold"],
    "unevidenced_kpis": [],
    "duration_sec": 47,
}

EXTRACTED_JSON = {"topics": ["credit limit", "locker"], "agent_name": "Priya"}


# ---------------------------------------------------------------- config ---

def test_batch_config_reads_the_source_column_not_the_metadata_key(conn, batch):
    """They disagree in production, and the column is the one that routes."""
    config = read_batch_config(conn, batch["batch_id"])
    assert config["source"] == "audio_upload"
    assert config["target_lang"] == "English"
    assert config["romanize"] is False
    assert config["usecase_id"] == "1149"


def test_reading_a_batch_that_does_not_exist_says_so(conn):
    with pytest.raises(StoreError, match="No transcription_batches row"):
        read_batch_config(conn, 999_999)


def test_kpi_codes_come_from_the_config_pinned_to_the_batch(conn, batch):
    assert selected_kpi_codes(conn, batch["batch_id"]) == [
        "rpc_verified", "disclosure_given"
    ]


def test_a_deselected_kpi_is_excluded(conn, batch):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE usecase_kpi_selections SET is_selected=false WHERE kpi_id=%s",
            (batch["kpi_ids"]["disclosure_given"],),
        )
    assert selected_kpi_codes(conn, batch["batch_id"]) == ["rpc_verified"]


def test_a_newer_config_version_does_not_leak_into_a_pinned_batch(conn, batch):
    """The whole reason batch_kpi_configs exists.

    Adding a KPI to a *new* version of the use case's config must not change
    what an already-pinned batch is scored against.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO usecase_kpi_configs (usecase_id, usecase_name, version) "
            "VALUES ('1149', 'Voice-Analytics', 2) RETURNING id"
        )
        newer = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO kpis (section_id, name, kpi_code, display_order) "
            "VALUES (%s, 'Brand new', 'brand_new', 9) RETURNING id",
            (batch["section_id"],),
        )
        new_kpi = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO usecase_kpi_selections (config_id, kpi_id, is_selected) "
            "VALUES (%s, %s, true)", (newer, new_kpi),
        )

    assert "brand_new" not in selected_kpi_codes(conn, batch["batch_id"])


def test_a_batch_can_only_pin_one_config(conn, batch):
    """batch_kpi_configs.batch_id is UNIQUE -- the pinning guarantee."""
    import psycopg

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO usecase_kpi_configs (usecase_id, usecase_name, version) "
            "VALUES ('1149', 'Voice-Analytics', 3) RETURNING id"
        )
        other = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO batch_kpi_configs (batch_id, usecase_kpi_config_id) "
                "VALUES (%s, %s)", (batch["batch_id"], other),
            )


def test_skipped_kpis_are_recorded_on_the_batch(conn, batch):
    record_skipped_kpis(conn, batch["batch_id"], ["no_prompt_a", "no_prompt_b"])
    config = read_batch_config(conn, batch["batch_id"])
    with conn.cursor() as cur:
        cur.execute("SELECT metadata FROM transcription_batches WHERE id=%s",
                    (batch["batch_id"],))
        metadata = cur.fetchone()[0]
    assert metadata["kpi_codes_skipped"] == ["no_prompt_a", "no_prompt_b"]
    # The merge must not discard what was already there.
    assert metadata["target_language"] == "English"
    assert config["target_lang"] == "English"


def test_recording_no_skipped_kpis_changes_nothing(conn, batch):
    record_skipped_kpis(conn, batch["batch_id"], [])
    with conn.cursor() as cur:
        cur.execute("SELECT metadata FROM transcription_batches WHERE id=%s",
                    (batch["batch_id"],))
        assert "kpi_codes_skipped" not in cur.fetchone()[0]


# ----------------------------------------------------------- transcripts ---

def test_seeding_creates_a_pending_row_per_file_and_sets_the_counters(conn, batch):
    seeded = seed_transcriptions(conn, batch["batch_id"], [
        {"name": "a.wav", "file_path": "gs://bucket/a.wav"},
        {"name": "b.wav", "file_path": "gs://bucket/b.wav"},
    ])
    assert [s["transcription_id"] for s in seeded] == sorted(
        s["transcription_id"] for s in seeded)

    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM transcriptions WHERE batch_id=%s "
                    "GROUP BY status", (batch["batch_id"],))
        assert cur.fetchall() == [("pending", 2)]
        cur.execute("SELECT total_files, pending_files FROM transcription_batches "
                    "WHERE id=%s", (batch["batch_id"],))
        assert cur.fetchone() == (2, 2)


def test_a_transcript_is_stored_with_the_language_the_model_returned(conn, batch):
    """primary_language, not detected_language -- the key the prompt produces."""
    seeded = seed_transcriptions(conn, batch["batch_id"],
                                 [{"name": "a.wav", "file_path": "/tmp/a.wav"}])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT transcript, translated_transcript, detected_language, "
            "processing_time_ms, status FROM transcriptions WHERE id=%s",
            (seeded[0]["transcription_id"],),
        )
        transcript, translated, language, elapsed, status = cur.fetchone()

    assert language == "Hindi"
    assert status == "completed"
    assert translated.startswith("[00:02] Greetings.")
    assert float(elapsed) == pytest.approx(1234.5)


def test_a_failed_file_keeps_its_transcript_columns_null(conn, batch):
    """A half-transcript that looks like a result is worse than none."""
    seeded = seed_transcriptions(conn, batch["batch_id"],
                                 [{"name": "a.wav", "file_path": "/tmp/a.wav"}])
    mark_transcription_failed(conn, seeded[0]["transcription_id"], "audio unreadable")

    with conn.cursor() as cur:
        cur.execute("SELECT status, error_message, transcript FROM transcriptions "
                    "WHERE id=%s", (seeded[0]["transcription_id"],))
        status, error, transcript = cur.fetchone()
    assert (status, transcript) == ("failed", None)
    assert error == "audio unreadable"


def test_the_tally_counts_rows_and_updates_the_batch(conn, batch):
    seeded = seed_transcriptions(conn, batch["batch_id"], [
        {"name": f"{i}.wav", "file_path": f"/tmp/{i}.wav"} for i in range(4)
    ])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)
    persist_transcript(conn, seeded[1]["transcription_id"], TRANSCRIPT_JSON)
    persist_transcript(conn, seeded[2]["transcription_id"], TRANSCRIPT_JSON)
    mark_transcription_failed(conn, seeded[3]["transcription_id"], "boom")

    tally = transcription_tally(conn, batch["batch_id"])
    assert tally == {"total": 4, "completed": 3, "failed": 1, "failed_pct": 25}

    with conn.cursor() as cur:
        cur.execute("SELECT completed_files, failed_files_count, pending_files "
                    "FROM transcription_batches WHERE id=%s", (batch["batch_id"],))
        assert cur.fetchone() == (3, 1, 0)


def test_only_completed_files_are_offered_for_scoring(conn, batch):
    seeded = seed_transcriptions(conn, batch["batch_id"], [
        {"name": "good.wav", "file_path": "/tmp/g.wav"},
        {"name": "bad.wav", "file_path": "/tmp/b.wav"},
    ])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)
    mark_transcription_failed(conn, seeded[1]["transcription_id"], "boom")

    rows = completed_transcriptions(conn, batch["batch_id"])
    assert [r["filename"] for r in rows] == ["good.wav"]


# -------------------------------------------------------------- analysis ---

def test_kpi_code_map_covers_the_configured_kpis(conn, batch):
    mapping = kpi_code_to_id(conn)
    assert set(mapping) == {"rpc_verified", "disclosure_given"}


def test_a_null_kpi_code_is_ignored_rather_than_keying_the_map_on_none(conn, batch):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO kpis (section_id, name, display_order) "
                    "VALUES (%s, 'No code', 9)", (batch["section_id"],))
    assert None not in kpi_code_to_id(conn)


def test_a_duplicated_kpi_code_fails_loudly(conn, batch):
    """Guessing would attach a score to the wrong KPI, which is worse than
    losing it."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO kpis (section_id, name, kpi_code, display_order) "
                    "VALUES (%s, 'Copy', 'rpc_verified', 9)", (batch["section_id"],))
    with pytest.raises(StoreError, match="more than one row"):
        kpi_code_to_id(conn)


def test_a_full_analysis_writes_all_four_tables(conn, batch):
    seeded = seed_transcriptions(conn, batch["batch_id"],
                                 [{"name": "a.wav", "file_path": "/tmp/a.wav"}])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)

    analysis_batch_id = open_analysis_batch(
        conn, batch["batch_id"], "1149", "Voice-Analytics")
    result = persist_analysis(
        conn,
        analysis_batch_id=analysis_batch_id,
        transcription_id=seeded[0]["transcription_id"],
        filename="a.wav",
        transcript=TRANSCRIPT_JSON["transcript"],
        analysis=ANALYSIS_JSON,
        extracted=EXTRACTED_JSON,
        kpi_ids=kpi_code_to_id(conn),
        usecase_id="1149",
        usecase_name="Voice-Analytics",
        transcription_batch_id=batch["batch_id"],
    )

    assert result["kpis_stored"] == 2
    assert result["unknown_codes"] == []

    with conn.cursor() as cur:
        cur.execute("SELECT duration_sec, metadata, external_call_id FROM calls "
                    "WHERE id=%s", (result["call_id"],))
        duration, metadata, external = cur.fetchone()
        assert duration == 47
        assert metadata["agent_name"] == "Priya"
        assert external == str(seeded[0]["transcription_id"])

        cur.execute("SELECT overall_score, high_risk_call, topics, "
                    "call_impact_level, customer_experience_drivers "
                    "FROM call_analyses WHERE id=%s", (result["analysis_id"],))
        overall, high_risk, topics, impact, drivers = cur.fetchone()
        assert float(overall) == 7.0          # unweighted mean of 8 and 6
        assert high_risk is True
        assert topics == ["credit limit", "locker"]
        assert impact == "High"
        assert drivers == ["long hold"]

        cur.execute('SELECT "references" FROM analysis_kpi_results '
                    "WHERE analysis_id=%s ORDER BY score DESC",
                    (result["analysis_id"],))
        references = [r[0] for r in cur.fetchall()]
    assert references[0][0]["quote"] == "Namaste."
    assert references[1] == []


def test_a_kpi_code_with_no_row_is_reported_rather_than_dropped(conn, batch):
    """The originating service skipped these silently."""
    seeded = seed_transcriptions(conn, batch["batch_id"],
                                 [{"name": "a.wav", "file_path": "/tmp/a.wav"}])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)
    analysis_batch_id = open_analysis_batch(conn, batch["batch_id"], "1149", "V")

    analysis = json.loads(json.dumps(ANALYSIS_JSON))
    analysis["kpis"].append({"kpi_code": "never_configured", "section": "X",
                             "score": 5.0, "applicable": True})

    result = persist_analysis(
        conn, analysis_batch_id=analysis_batch_id,
        transcription_id=seeded[0]["transcription_id"], filename="a.wav",
        transcript="t", analysis=analysis, extracted=None,
        kpi_ids=kpi_code_to_id(conn),
    )
    assert result["unknown_codes"] == ["never_configured"]
    assert result["kpis_stored"] == 2


def test_analysis_without_extraction_still_writes_the_agent_name_key(conn, batch):
    """Agent reports aggregate on metadata->>'agent_name'; the key must exist."""
    seeded = seed_transcriptions(conn, batch["batch_id"],
                                 [{"name": "a.wav", "file_path": "/tmp/a.wav"}])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)
    analysis_batch_id = open_analysis_batch(conn, batch["batch_id"], "1149", "V")

    result = persist_analysis(
        conn, analysis_batch_id=analysis_batch_id,
        transcription_id=seeded[0]["transcription_id"], filename="a.wav",
        transcript="t", analysis=ANALYSIS_JSON, extracted=None,
        kpi_ids=kpi_code_to_id(conn),
    )
    with conn.cursor() as cur:
        cur.execute("SELECT metadata, (SELECT topics FROM call_analyses WHERE id=%s) "
                    "FROM calls WHERE id=%s",
                    (result["analysis_id"], result["call_id"]))
        metadata, topics = cur.fetchone()
    assert "agent_name" in metadata and metadata["agent_name"] is None
    assert topics is None


def test_an_impact_level_outside_high_low_is_refused_by_the_database(conn, batch):
    import psycopg

    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO batches (usecase_id, name, status) VALUES ('1','x','p') "
            "RETURNING id")
        bid = cur.fetchone()[0]
        cur.execute("INSERT INTO calls (batch_id, filename) VALUES (%s,'a') RETURNING id",
                    (bid,))
        cid = cur.fetchone()[0]
        cur.execute("INSERT INTO call_analyses (call_id, batch_id, call_impact_level) "
                    "VALUES (%s,%s,'Medium')", (cid, bid))


def test_the_same_kpi_cannot_be_scored_twice_for_one_analysis(conn, batch):
    """uq_analysis_kpi -- the constraint the DAG's ON CONFLICT names."""
    import psycopg

    seeded = seed_transcriptions(conn, batch["batch_id"],
                                 [{"name": "a.wav", "file_path": "/tmp/a.wav"}])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)
    analysis_batch_id = open_analysis_batch(conn, batch["batch_id"], "1149", "V")
    result = persist_analysis(
        conn, analysis_batch_id=analysis_batch_id,
        transcription_id=seeded[0]["transcription_id"], filename="a.wav",
        transcript="t", analysis=ANALYSIS_JSON, extracted=None,
        kpi_ids=kpi_code_to_id(conn),
    )
    with conn.cursor() as cur, pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO analysis_kpi_results (analysis_id, kpi_id) VALUES (%s,%s)",
            (result["analysis_id"], batch["kpi_ids"]["rpc_verified"]),
        )


def test_closing_the_analysis_batch_counts_what_was_written(conn, batch):
    seeded = seed_transcriptions(conn, batch["batch_id"], [
        {"name": f"{i}.wav", "file_path": f"/tmp/{i}.wav"} for i in range(2)])
    for s in seeded:
        persist_transcript(conn, s["transcription_id"], TRANSCRIPT_JSON)

    analysis_batch_id = open_analysis_batch(conn, batch["batch_id"], "1149", "V")
    kpi_ids = kpi_code_to_id(conn)
    for s in seeded:
        persist_analysis(
            conn, analysis_batch_id=analysis_batch_id,
            transcription_id=s["transcription_id"], filename=s["name"],
            transcript="t", analysis=ANALYSIS_JSON, extracted=None, kpi_ids=kpi_ids)

    assert close_analysis_batch(conn, analysis_batch_id) == {
        "total": 2, "completed": 2, "failed": 0}


# --------------------------------------------------------------- failure ---

def test_finalise_reports_partial_when_some_files_failed(conn, batch):
    seeded = seed_transcriptions(conn, batch["batch_id"], [
        {"name": f"{i}.wav", "file_path": f"/tmp/{i}.wav"} for i in range(3)])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)
    mark_transcription_failed(conn, seeded[1]["transcription_id"], "boom")
    mark_transcription_failed(conn, seeded[2]["transcription_id"], "boom")

    assert finalise_batch(conn, batch["batch_id"]) == {
        "status": "partial", "completed": 1, "failed": 2}


def test_finalise_reports_completed_when_nothing_failed(conn, batch):
    seeded = seed_transcriptions(conn, batch["batch_id"],
                                 [{"name": "a.wav", "file_path": "/tmp/a.wav"}])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)
    assert finalise_batch(conn, batch["batch_id"])["status"] == "completed"


def test_a_pipeline_failure_marks_the_rows_not_only_the_batch(conn, batch):
    """Marking only the batch leaves every file reading 'pending' forever."""
    seed_transcriptions(conn, batch["batch_id"], [
        {"name": f"{i}.wav", "file_path": f"/tmp/{i}.wav"} for i in range(3)])
    mark_batch_processing(conn, batch["batch_id"])

    mark_pipeline_failed(conn, batch["batch_id"], "transcribe", "transcription")

    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM transcriptions WHERE batch_id=%s "
                    "GROUP BY status", (batch["batch_id"],))
        assert cur.fetchall() == [("failed", 3)]

        cur.execute("SELECT status, metadata, failed_files_count, pending_files "
                    "FROM transcription_batches WHERE id=%s", (batch["batch_id"],))
        status, metadata, failed, pending = cur.fetchone()
    assert status == "failed"
    assert metadata["failed_task"] == "transcribe"
    assert (failed, pending) == (3, 0)


def test_a_failure_does_not_reopen_files_that_already_finished(conn, batch):
    seeded = seed_transcriptions(conn, batch["batch_id"], [
        {"name": "done.wav", "file_path": "/tmp/a.wav"},
        {"name": "waiting.wav", "file_path": "/tmp/b.wav"}])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)

    mark_pipeline_failed(conn, batch["batch_id"], "transcribe", "transcription")

    with conn.cursor() as cur:
        cur.execute("SELECT filename, status FROM transcriptions WHERE batch_id=%s "
                    "ORDER BY filename", (batch["batch_id"],))
        assert cur.fetchall() == [("done.wav", "completed"), ("waiting.wav", "failed")]


def test_an_analysis_stage_failure_marks_the_analysis_batch(conn, batch):
    seeded = seed_transcriptions(conn, batch["batch_id"],
                                 [{"name": "a.wav", "file_path": "/tmp/a.wav"}])
    persist_transcript(conn, seeded[0]["transcription_id"], TRANSCRIPT_JSON)
    analysis_batch_id = open_analysis_batch(conn, batch["batch_id"], "1149", "V")

    mark_pipeline_failed(conn, batch["batch_id"], "analyse", "analysis")

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM batches WHERE id=%s", (analysis_batch_id,))
        assert cur.fetchone()[0] == "failed"


def test_a_batch_with_no_usecase_id_is_refused_before_anything_runs(conn, batch):
    """usecase_id is nullable in the schema but not optional in the pipeline.

    It is what PromptHub resolves prompts and model entitlement against, so a
    NULL would surface as a 404 for use case "None" several tasks later --
    after the batch had already looked healthy.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE transcription_batches SET usecase_id=NULL WHERE id=%s",
                    (batch["batch_id"],))

    with pytest.raises(StoreError, match="no usecase_id"):
        read_batch_config(conn, batch["batch_id"])


def test_a_batch_with_an_empty_usecase_id_is_refused_too(conn, batch):
    with conn.cursor() as cur:
        cur.execute("UPDATE transcription_batches SET usecase_id='' WHERE id=%s",
                    (batch["batch_id"],))

    with pytest.raises(StoreError, match="no usecase_id"):
        read_batch_config(conn, batch["batch_id"])
