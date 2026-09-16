# Old DAG vs new DAG: is it the same thing?

The old DAG called seven HTTP endpoints. The new one runs pods. This is the
comparison, endpoint by endpoint, with the differences that are deliberate
separated from the ones that would be bugs.

Verified against `src/routers/pipeline.py` and
`src/utils/analytics_repository.py` in the originating repository.

---

## Summary

| Old call | New equivalent | Same result? |
| --- | --- | --- |
| `GET /selected-kpis` | `read_config` | ✅ identical query |
| PromptHub `GET /client/usecase/{id}` | `validate_access` | ✅ same checks, run earlier |
| `POST /transcribe` | `transfer` + `list_audio_files` + `transcribe` pods + `persist_transcripts` | ⚠️ same per-file work, different ingest |
| `GET /transcribe-status/{id}` | pod exit codes | ✅ equivalent, no polling |
| `POST /analyse` | `analyse` + `extract` pods + `persist_analysis` | ✅ same writes |
| `POST /cleanup` | `cleanup` | ⚠️ same intent, GCS instead of local disk |
| `POST /mark-failed` | `_mark_batch_failed` | ✅ same rows updated |

**Per-call business logic is identical.** The prompts are byte-for-byte (locked
by `test_prompt_parity.py`), the retry policy matches, the anonymisation
response search is proven equal (`test_anonymization_parity.py`), and the rows
written match the live schema (`test_dag_sql_matches_schema.py`).

The differences are all at the edges: where files come from, how work is
distributed, and how failure is reported.

---

## Identical

### Selecting which KPIs to score

The endpoint calls `get_selected_kpi_codes_for_transcription_batch`, which
joins `kpis -> usecase_kpi_selections -> batch_kpi_configs` filtered on
`is_selected`. `read_config` issues that same join.

One addition: the DAG's query ends `ORDER BY k.display_order, k.kpi_code`. The
original has no `ORDER BY`, so the KPI order in the assembled prompt was
whatever Postgres returned that day. Since a reordered prompt is a different
prompt, the new one is deterministic.

### Per-file transcription

`_transcribe_single_row` and the `transcribe` pod do the same four things in
the same order: transcribe without translating, anonymise, translate the
anonymised text, record the duration. Same model, same prompt, same retry
policy, same speaker-label handling.

### Scoring

`run_batch_item_analysis` and the `analyse` pod send the same consolidated
prompt to the same model and parse the same response, including `risk_summary`,
`call_impact` and `customer_experience_drivers`.

### Topics and agent name

Ported unchanged, prompts verbatim, same best-effort semantics: a failure
leaves the field empty rather than failing the call.

### Failure reporting

`POST /mark-failed` marks pending/processing **rows** failed -- `transcriptions`
for a transcription-stage failure, `call_analyses` for an analysis-stage one --
then recalculates counters. `_mark_batch_failed` now does the same.

> An earlier version of the DAG updated only `transcription_batches.status`.
> That is the failure mode that looks fine on a dashboard: the batch is red,
> every file still says it is pending, and the counters never add up.

---

## Deliberately different

### Where the audio comes from

| | Old | New |
| --- | --- | --- |
| Arrival | HTTP upload to the API | SFTP, pulled by the pipeline |
| Storage | local temp file | GCS object |
| `transcriptions` rows | created by the upload endpoint | created by `list_audio_files` |
| `file_path` | `/tmp/...` | `gs://...` |

The old `/transcribe` endpoint **did not ingest anything**. It selected rows
already at `status='pending'`, created earlier by an upload endpoint that had
written the audio to local disk. The DAG could not run without a human having
uploaded first.

The task breakdown makes the SFTP connector the pipeline's first stage, so that
step now exists and the DAG creates its own rows.

### How the work is distributed

| | Old | New |
| --- | --- | --- |
| Unit | one `asyncio` task in the API process | one Kubernetes pod |
| Concurrency | `asyncio.Semaphore(10)`, hardcoded | `max_active_tis_per_dag=8`, configurable |
| Isolation | none -- shared process | one pod per file |
| Progress | DAG polls every 15s for up to an hour | pods report their own completion |

Three consequences:

* **A worker slot is not held for an hour.** The old `transcribe` task did
  `time.sleep(15)` in a loop up to 3600 seconds while an Airflow worker slot
  sat idle.
* **A crash costs one file, not the batch.** Every file ran inside one API
  process; an unhandled error there took the rest with it.
* **A restart does not strand a batch.** `/transcribe` returned `202` and ran
  the work in a FastAPI `BackgroundTasks`. If the API restarted mid-batch that
  task vanished, the batch stayed at `processing` forever, and the DAG polled
  until its timeout.

### Cleanup

Same intent -- delete the recordings, null `file_path`, so "0 recordings
retained" is true. The old one removed local files with `os.remove`; the new
one deletes GCS objects. Both report failures rather than aborting.

### Error signalling

Old: HTTP status. `429` and `5xx` retried, everything else failed fast.
New: process exit codes, 0-9. The orchestrator can tell a bad credential (5)
from a busy gateway (6) from an anonymisation failure (9) -- which HTTP status
could not express, because the API returned `500` for all three.

### TLS

Every `requests` call in the old DAG passed `verify=False`, including the ones
carrying `INTERNAL-API-KEY`. Inside a corporate network that accepts any
certificate presented. The new DAG verifies against the internal CA bundle.

---

## Fixed along the way

Bugs in the old path that the new one does not reproduce. Each is worth
reporting upstream, because the old service still has them.

| Bug | Effect | Where |
| --- | --- | --- |
| `result.get("detected_language")` | The transcription prompt returns `primary_language`. No code anywhere produces `detected_language`, so **the column is NULL on every audio row in production**. | `pipeline.py:315` |
| Analysis model checked after transcription | A use case without Qwen access transcribed the whole batch, paid for it, then failed. | old DAG task order |
| Module-level `Variable.get()` x5 | Five database round trips every 30-second DAG parse, forever. | old DAG top level |
| KPI codes with no `kpis` row | `if kpi_orm is None: continue` -- the model scores it, the write drops it, nothing is logged. | `analytics_repository.py:495` |
| No `ORDER BY` on KPI selection | Prompt ordering non-deterministic between runs. | `analytics_repository.py:1088` |

The new DAG logs the unknown KPI codes rather than dropping them silently, and
fails loudly if `kpis.kpi_code` is duplicated -- attaching a score to the wrong
KPI is worse than losing it.

---

## Not carried over

* **`_format_segments_multi_key`** (`pipeline.py:417`) -- dead code, called
  from nowhere.
* **`metric`** on `transcription_batches` -- written by three endpoints, read
  by none.
* **`transcriptions.transcript_metadata` / `canonical_transcript` /
  `external_id` / `content_hash`** -- Flow B columns. The audio pipeline leaves
  them `NULL` / `'{}'`, exactly as it does today.
* **HTML escaping on responses** -- an API concern. There is no API.

## Still missing from both

* **Token usage.** Neither captures `response.usage`, so cost per call cannot
  be computed from the data.
* **Anonymisation status.** Nothing records that PII removal ran, so the claim
  cannot be evidenced from the database.
