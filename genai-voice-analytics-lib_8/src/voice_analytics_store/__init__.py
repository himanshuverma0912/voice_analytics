"""Writing voice analytics results to Postgres.

**A separate package from `voice_analytics` on purpose.** The library computes
and returns JSON; it opens no database connection and never will (ADR 0001).
This package is the other half: it takes that JSON and writes rows.

Two callers use it, which is why it exists as a module rather than living
inside one of them:

* the Airflow DAG, which runs each stage as a pod and persists the results;
* `scripts/run_local.py --postgres`, which runs the same stages as
  subprocesses on a laptop.

Both write identical rows, so a local run is a real rehearsal of a production
one rather than a demonstration of a subset.

Install with the ``store`` extra::

    pip install 'genai-voice-analytics-lib[store]'

Every function takes an open connection and does **not** commit. Transaction
boundaries belong to the caller: a DAG task commits per file so progress
survives a failure, while a local run may prefer one transaction per batch.
"""

from voice_analytics_store.analysis import (
    close_analysis_batch,
    kpi_code_to_id,
    open_analysis_batch,
    persist_analysis,
)
from voice_analytics_store.batch import (
    clear_file_paths,
    finalise_batch,
    mark_batch_processing,
    mark_pipeline_failed,
    read_batch_config,
    record_skipped_kpis,
    selected_kpi_codes,
)
from voice_analytics_store.connection import connect, dsn_from_env, open_connection
from voice_analytics_store.transcripts import (
    completed_transcriptions,
    mark_transcription_failed,
    persist_transcript,
    seed_transcriptions,
    transcription_tally,
)

__all__ = [
    "clear_file_paths",
    "close_analysis_batch",
    "completed_transcriptions",
    "connect",
    "dsn_from_env",
    "finalise_batch",
    "kpi_code_to_id",
    "mark_batch_processing",
    "mark_pipeline_failed",
    "mark_transcription_failed",
    "open_analysis_batch",
    "open_connection",
    "persist_analysis",
    "persist_transcript",
    "read_batch_config",
    "record_skipped_kpis",
    "seed_transcriptions",
    "selected_kpi_codes",
    "transcription_tally",
]
