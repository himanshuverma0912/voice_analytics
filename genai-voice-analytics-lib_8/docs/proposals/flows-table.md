# Proposal: a `flows` table

**Status:** draft, for discussion with the API/UI owner
**Scope:** caller-side schema. Nothing here goes into the library — see ADR 0001.
**Decision needed by:** whoever writes the `INSERT INTO transcription_batches`

---

## Why

`transcription_batches` fuses configuration with run state in one row:

| Configuration (reusable) | Run state (per execution) |
| --- | --- |
| `metadata` (romanize, target_language) | `total_files`, `completed_files` |
| `usecase_id`, `usecase_name` | `processing_files`, `pending_files` |
| `batch_name`, `source` | `failed_files`, `failed_files_count` |
| `schedule_type` | `status` |

Running the same configuration twice therefore means either overwriting the
previous run's counters, or duplicating the configuration into a second row
with nothing linking the two.

`scheduled_batch_queue` cannot express a repeat either. `mark_batch_triggered`
uses `scalar_one_or_none()` on `transcription_batch_id`, and
`batch.schedule_queue_entry` is a scalar relationship, so a batch may have at
most one queue row. `triggered` is a one-way boolean; `get_due_batches` filters
on `triggered IS FALSE`, so a fired row never returns. There is no
`frequency`, `cron`, `interval` or `next_run_at` anywhere in the repository.

The wireframe's Schedule drawer asks for:

```
Once, now | Once, later | Recurring
Frequency  Every day          At  06:00 IST
Files each run  Only files new since the last successful run
                (tracked by file path and checksum, so a re-upload
                 or a rename is not processed twice)
Concurrency 8   Retries 2   Halt above 10% failed
Notify  On completion and on early stop -> ops-north@, quality-lead@
Locked until a dry run passes
```

None of those eleven values has a column today.

---

## The shape

Three objects, each with one job.

```
flows                  the definition        -- edited by a human, reused
  |
  | emits one per firing
  v
transcription_batches  one execution         -- unchanged, gains one FK
  |
  v
transcriptions         one file
```

plus

```
flow_seen_files        what has been processed, ever
```

---

## 1. `flows`

```sql
CREATE TABLE flows (
    id                  bigserial    PRIMARY KEY,
    name                varchar(255) NOT NULL,
    usecase_id          varchar(255) NOT NULL,
    usecase_name        varchar(255),

    -- Which pipeline. 'voice_analytics' is the wireframe's Flow A
    -- (transcribe -> anonymise -> score). Flow B would be 'transcript_match'.
    flow_type           varchar(32)  NOT NULL DEFAULT 'voice_analytics',

    -- Where the audio comes from. Mirrors transcription_batches.source, which
    -- is what routes the DAG today.
    source              varchar(64)  NOT NULL,
    source_config       jsonb        NOT NULL DEFAULT '{}'::jsonb,

    -- What the nodes do. Becomes transcription_batches.metadata verbatim on
    -- each firing, so the DAG's existing read is unchanged.
    --   {"romanize": false, "target_language": "English", "anonymise": true}
    pipeline_config     jsonb        NOT NULL DEFAULT '{}'::jsonb,

    -- ---- schedule ----------------------------------------------------
    schedule_mode       varchar(20)  NOT NULL,   -- run_now | once_later | recurring
    scheduled_for       timestamptz,             -- once_later only
    cron_expression     varchar(120),            -- recurring only, e.g. '0 6 * * *'
    timezone            varchar(64)  NOT NULL DEFAULT 'Asia/Kolkata',

    -- ---- file selection ----------------------------------------------
    file_selection      varchar(32)  NOT NULL DEFAULT 'new_since_last_success',
                                                 -- all | new_since_last_success

    -- ---- execution policy --------------------------------------------
    concurrency         smallint     NOT NULL DEFAULT 8,
    max_retries         smallint     NOT NULL DEFAULT 2,
    halt_above_pct      smallint,                -- NULL = never halt early

    -- ---- notification -------------------------------------------------
    notify_on           text[]       NOT NULL DEFAULT '{}',  -- completion, early_stop
    notify_emails       text[]       NOT NULL DEFAULT '{}',

    -- ---- dry-run gate --------------------------------------------------
    -- The wireframe locks scheduling until a dry run passes. Storing the
    -- result here is what makes that lock enforceable on the server rather
    -- than only in the browser.
    last_dry_run_at     timestamptz,
    last_dry_run_passed boolean,

    -- ---- lifecycle ------------------------------------------------------
    is_active           boolean      NOT NULL DEFAULT false,
    last_run_at         timestamptz,
    last_success_at     timestamptz,
    next_run_at         timestamptz,
    created_by          varchar(255),
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT ck_flows_schedule_mode
        CHECK (schedule_mode IN ('run_now', 'once_later', 'recurring')),
    CONSTRAINT ck_flows_once_later_has_time
        CHECK (schedule_mode <> 'once_later' OR scheduled_for IS NOT NULL),
    CONSTRAINT ck_flows_recurring_has_cron
        CHECK (schedule_mode <> 'recurring' OR cron_expression IS NOT NULL),
    CONSTRAINT ck_flows_file_selection
        CHECK (file_selection IN ('all', 'new_since_last_success')),
    CONSTRAINT ck_flows_halt_pct
        CHECK (halt_above_pct IS NULL OR halt_above_pct BETWEEN 1 AND 100),
    CONSTRAINT ck_flows_concurrency
        CHECK (concurrency BETWEEN 1 AND 64)
);

-- The dispatcher's only query. Partial, so it stays small however many
-- inactive or draft flows accumulate.
CREATE INDEX ix_flows_due
    ON flows (next_run_at)
    WHERE is_active AND next_run_at IS NOT NULL;

CREATE INDEX ix_flows_usecase ON flows (usecase_id);
```

### Why `cron_expression` rather than `frequency` + `at`

The drawer shows "Every day / 06:00", which two columns would hold. But the
next request is always "every weekday", then "twice a day", then "the 1st of
the month" -- each of which needs another column. A cron string holds all of
them, Airflow already speaks it, and the UI can keep showing a friendly
dropdown that maps to `0 6 * * *` behind the scenes.

### Why `next_run_at` is stored, not computed

Computing "is this flow due?" from a cron expression inside a SQL predicate is
not indexable. Storing the next firing makes the dispatcher a single indexed
range scan, and the flow itself recomputes it after each firing.

---

## 2. One FK on `transcription_batches`

```sql
ALTER TABLE transcription_batches
    ADD COLUMN flow_id bigint     NULL REFERENCES flows(id),
    ADD COLUMN run_seq integer    NULL;

CREATE INDEX ix_batches_flow ON transcription_batches (flow_id, created_at DESC);
```

Both nullable, so **every existing row and every existing code path keeps
working untouched**. A batch with `flow_id IS NULL` is an ad-hoc upload, exactly
as today. A batch with `flow_id` set was emitted by a flow, and `run_seq` gives
it a human-readable "run 14 of this flow".

This one index is also the run-history query the wireframe's flow detail page
needs:

```sql
SELECT id, run_seq, status, total_files, failed_files_count, created_at
FROM   transcription_batches
WHERE  flow_id = $1
ORDER  BY created_at DESC
LIMIT  20;
```

Nothing else about `transcription_batches` changes. Its columns keep their
current meanings, and the DAG's existing read of `metadata` / `usecase_id` /
`source` is unaffected.

---

## 3. `flow_seen_files`

This is the table the wireframe's sentence actually demands:

> Tracked by file path and checksum, so a re-upload or a rename is not
> processed twice.

```sql
CREATE TABLE flow_seen_files (
    flow_id       bigint      NOT NULL REFERENCES flows(id) ON DELETE CASCADE,
    content_hash  bpchar(32)  NOT NULL,           -- MD5, same as transcriptions.content_hash
    file_path     text        NOT NULL,
    batch_id      bigint      REFERENCES transcription_batches(id),
    first_seen_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (flow_id, content_hash)
);

CREATE INDEX ix_flow_seen_path ON flow_seen_files (flow_id, file_path);
```

The primary key on `(flow_id, content_hash)` is what makes a **rename** free:
same bytes, same hash, already seen, skipped. The secondary index on
`file_path` answers the other half -- same path, different content -- which
should be processed, because the file genuinely changed.

A timestamp watermark cannot do either. `WHERE modified_at > last_run`
reprocesses a renamed file and misses a corrected one.

`transcriptions.content_hash` already exists and is already MD5, written by
`transcript_ingest.py` for Flow B's idempotency check. This extends the same
mechanism to Flow A rather than inventing a second one.

### Retention

One row per file per flow, forever. A daily flow over 12,480 files a run
reaches roughly 4.5M rows in a year -- small for Postgres, but worth a note:
if a flow is ever reset, `DELETE FROM flow_seen_files WHERE flow_id = $1` is
the "reprocess everything" button.

---

## How a run happens

```
1. Dispatcher (Airflow DAG, every 5 min)

   SELECT id, usecase_id, source, source_config, pipeline_config,
          concurrency, max_retries, halt_above_pct, file_selection
   FROM   flows
   WHERE  is_active AND next_run_at <= now()
   ORDER  BY next_run_at
   FOR UPDATE SKIP LOCKED;

   FOR UPDATE SKIP LOCKED is what the `triggered` boolean was approximating.
   Two dispatchers can run concurrently and neither will fire the same flow.

2. For each due flow, INSERT one transcription_batches row:

     flow_id       = flows.id
     run_seq       = (SELECT coalesce(max(run_seq), 0) + 1 ...)
     usecase_id    = flows.usecase_id
     usecase_name  = flows.usecase_name
     source        = flows.source             <- the COLUMN, which routes the DAG
     metadata      = flows.pipeline_config    <- unchanged shape, DAG reads it as today
     schedule_type = 'flow'
     status        = 'pending'

3. UPDATE flows SET last_run_at = now(),
                    next_run_at = <next cron firing after now()>;

4. Trigger the pipeline DAG with the new batch id. From here on, everything
   is exactly the flow already built and documented.

5. On success: UPDATE flows SET last_success_at = now();
```

Step 4 is the handover point. **The pipeline DAG's input does not change** --
it still receives a `transcription_batch_id` and reads its config from that
row. A flow is simply a new way for that row to come into existence, alongside
the existing manual upload.

---

## What this does *not* change

- **The library.** Still pure compute, still no database access (ADR 0001).
  `concurrency` becomes the operator's `max_active_tis_per_dag`, `max_retries`
  becomes its `retries`, `halt_above_pct` becomes a short-circuit task. All
  three are orchestration settings, not library arguments.
- **`transcriptions`.** No change.
- **The DAG's read.** Still `SELECT metadata, usecase_id, source FROM
  transcription_batches WHERE id = %s`.
- **Existing rows.** Two nullable columns, no backfill, no data migration.

## What it makes possible to delete, later

`scheduled_batch_queue` becomes a special case of a flow with
`schedule_mode = 'once_later'`. **Do not remove it as part of this change** --
it works, it is in production, and coupling a schema addition to a removal
turns a safe migration into a risky one. Deprecate it once flows are proven.

---

## Open questions for the API owner

1. **Are recurring flows actually in scope for v1?** If every run will be
   kicked off by a human, none of this is needed and the Schedule drawer should
   be trimmed to "Once, now" / "Once, later". That is a legitimate and cheaper
   answer.
2. **Where do SFTP credentials live?** `source_config` should hold a
   *reference* to a secret (an Airflow connection id, or a Vault path), never
   the secret itself. The old repo has a Fernet-encrypted credential store;
   whether flows reuse it is a decision.
3. **Who owns the dispatcher?** A short Airflow DAG on a 5-minute schedule is
   the obvious home, but it could equally be a service.
4. **Is `halt_above_pct` evaluated per run or per flow?** Per run is assumed
   here. Halting a *flow* after a bad run -- setting `is_active = false` --
   is a different and arguably more useful behaviour.
