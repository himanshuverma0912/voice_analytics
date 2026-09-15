# Where the pipeline's data lives

**The library stores nothing.** It takes bytes in and returns JSON out; every
row in this document is written by the caller -- the Airflow DAG in
`examples/airflow/`. That split is ADR 0001, and it is why the same container
image works against Postgres, BigQuery, or a filesystem.

This document exists because the DAG's SQL has to match a real schema, and
"look at the ORM" is not a schema.

**Which database.** The originating service runs two: `speech_analytics_db`
holds the jobs workflow, and **`ai_analytics_db` holds all nine tables below**.
`routers/pipeline.py` makes the choice on its first line --
`from src.database import get_analytics_db as get_db` -- so every query in the
pipeline goes to `ai_analytics_db`. Point `VOICE_ANALYTICS_DSN` at that one.
`speech_analytics_db` is a different application and is not part of this
pipeline.

Twelve tables, **all twelve of which already exist** -- they are the
originating service's, read from `src/models/analytics.py`. The pipeline needs
no change to any of them. The DAG reads or writes nine directly; the remaining
three (`kpi_sections`, `usecase_kpi_configs`, `scheduled_batch_queue`) it
reaches only through joins or not at all, but they are here because a mismatch
in them breaks a query that does run.

`flows` and `flow_seen_files` are *proposed additions*, not part of this count
-- see `docs/proposals/flows-table.md`.

---

## The shape of it

```
                      CONFIGURATION (written by the API/UI, read by the DAG)
                      ─────────────────────────────────────────────────────
  kpi_sections ──< kpis ──< usecase_kpi_selections >── usecase_kpi_configs
                     │                                          │
                     │                              batch_kpi_configs
                     │                                          │
                     │                                          v
                     │        ┌──────────────── transcription_batches ─────┐
                     │        │                    (one execution)         │
                     │        │                                            │
                     │        v                                            v
                     │   transcriptions                              scheduled_batch_queue
                     │   (one per file)
                     │        │
                     │        │   RESULTS (written by the DAG)
                     │        v
                     │     batches ──< calls ──< call_analyses ──< analysis_kpi_results
                     └────────────────────────────────────────────────┘
                                          kpi_id
```

Two things surprise everyone the first time:

1. **There are two tables called "batch".** `transcription_batches` (bigint id)
   is one pipeline execution. `batches` (UUID id) is the *analysis* batch that
   scoring results hang from. They are linked by
   `batches.transcription_batch_id`.
2. **`transcriptions` and `calls` both hold a transcript.** `transcriptions` is
   the transcription stage's output; `calls` is the analysis stage's copy of it.
   The DAG writes the transcript into both, which is how the originating
   service worked.

---

## What each task reads and writes

| DAG task | Reads | Writes |
| --- | --- | --- |
| `read_config` | `transcription_batches`, `batch_kpi_configs`, `usecase_kpi_selections`, `kpis` | `transcription_batches.status` |
| `validate_access` | PromptHub (HTTP) | — |
| `transfer` | SFTP | GCS objects |
| `build_prompt` | PromptHub (HTTP) | GCS object |
| `read_prompt_report` | GCS object | `transcription_batches.metadata` |
| `list_audio_files` | GCS manifest | `transcriptions` (one row per file), `transcription_batches` counters |
| `transcribe` (pod) | GCS audio | GCS transcript JSON |
| `persist_transcripts` | GCS transcript JSON | `transcriptions`, `transcription_batches` counters |
| `halt_gate` | `transcriptions` | — |
| `analyse` (pod) | GCS transcript JSON | GCS analysis JSON |
| `extract` (pod) | GCS transcript JSON | GCS extraction JSON |
| `open_analysis_batch` | — | `batches` |
| `persist_analysis` | GCS analysis + extraction JSON, `transcriptions`, `kpis` | `calls`, `call_analyses`, `analysis_kpi_results`, `batches` |
| `cleanup` | — | deletes GCS audio, nulls `transcriptions.file_path` |
| `finalise` | `transcriptions` | `transcription_batches` |

**Nothing in the library appears in that table.** Every pod reads a file and
writes a file; the rows are the DAG's doing.

---

## Resolving which KPIs to score

This is the one query worth reading twice, because getting it wrong is silent.

```sql
SELECT k.kpi_code
  FROM batch_kpi_configs       bkc
  JOIN usecase_kpi_selections  sel ON sel.config_id = bkc.usecase_kpi_config_id
  JOIN kpis                    k   ON k.id = sel.kpi_id
 WHERE bkc.batch_id = :transcription_batch_id
   AND sel.is_selected IS TRUE
 ORDER BY k.display_order, k.kpi_code;
```

`usecase_kpi_configs` is **versioned**, and `batch_kpi_configs` pins one version
to one batch at creation time. Reading the use case's current active config
instead would score this batch against a different KPI set than the batch it is
being compared with -- and nothing in the data would show why the two disagree.

The originating repository is explicit about it:

> Analysis MUST use this -- resolves via Batch -> BatchKPIConfig ->
> UsecaseKPIConfig -> Selections, never the 'latest usecase config' directly.

`batch_kpi_configs.batch_id` is `UNIQUE`: one batch, one pinned config, forever.

---

## The DDL

**The authoritative version is `live-schema.sql`**, exported from the database
itself. `tests/unit/test_dag_sql_matches_schema.py` checks every column the DAG
writes against it, so a schema change that breaks the pipeline fails a test
instead of a batch.

That export carries column names, types and nullability only -- DBeaver's
simplified DDL omits `DEFAULT` clauses, primary keys, foreign keys, unique
constraints and indexes. The DDL below adds those back from the ORM, so treat
it as the intended shape and `live-schema.sql` as the observed one.

### Where the live schema differs from the ORM

| Table | Difference | Does it matter? |
| --- | --- | --- |
| `analysis_kpi_results` | Has `confidence numeric(3,2)`, which no ORM model declares | No -- nullable, and nothing reads it. An orphan from an earlier design. |
| `kpis` | `kpi_code` is **nullable**, with no unique constraint | **Yes.** Results reference a KPI by `id`, so the DAG must map code to id. A NULL code or a duplicated one would attach a score to the wrong KPI, so the DAG filters NULLs and fails loudly on duplicates. |
| `calls` | `external_call_id` is **nullable** (the ORM says `NOT NULL`) | No -- the DAG supplies it regardless. |
| `calls`, `call_analyses`, `batches` | `uploaded_at` / `analyzed_at` are `timestamp` **without** time zone, while `created_at` / `updated_at` are `timestamptz` | **Yes, quietly.** Inserting `now()` into them converts using the session's `TimeZone`, so the stored value depends on server configuration. The DAG writes `(now() AT TIME ZONE \'UTC\')` to make it explicit. |
| `kpi_sections`, `kpis` | `name` is nullable (the ORM says `NOT NULL`) | No. |
| `usecase_kpi_configs` | `usecase_id` is nullable (the ORM says `NOT NULL`) | No -- but a config with no use case cannot be resolved to a batch. |

### One thing the export cannot tell us

`DEFAULT` clauses are absent from it, so nothing here proves that
`id uuid NOT NULL` really defaults to `gen_random_uuid()`, or that
`batches.total_calls` defaults to `0`. The DAG no longer relies on either:
every `NOT NULL` column it knows about is supplied explicitly. If you can run

```sql
SELECT table_name, column_name, column_default
FROM   information_schema.columns
WHERE  table_schema = \'public\' AND column_default IS NOT NULL
ORDER  BY table_name, ordinal_position;
```

the `id` question can be settled for good -- it is the one remaining place the
DAG trusts a default it has not seen.

### Configuration

```sql
CREATE TABLE kpi_sections (
    id            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    name          varchar(255) NOT NULL UNIQUE,
    description   text,
    weight        numeric(5,2) NOT NULL DEFAULT 1.00,
    display_order integer      NOT NULL DEFAULT 0,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX ix_kpi_sections_name          ON kpi_sections (name);
CREATE INDEX ix_kpi_sections_display_order ON kpi_sections (display_order);

-- One KPI. `kpi_code` is what the model echoes back and what the prompt is
-- keyed on; `id` is what results reference.
CREATE TABLE kpis (
    id            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id    uuid         NOT NULL REFERENCES kpi_sections(id) ON DELETE CASCADE,
    name          varchar(255) NOT NULL,
    kpi_code      varchar(255) NOT NULL,
    description   text,
    weight        numeric(5,2) DEFAULT 1.00,
    display_order integer      NOT NULL DEFAULT 0,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now()
);

-- A versioned set of KPIs for one use case. Never edited in place: a change
-- means a new version, so batches scored under the old one stay explicable.
CREATE TABLE usecase_kpi_configs (
    id           uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    usecase_id   varchar(255) NOT NULL,
    usecase_name varchar(255) NOT NULL,
    version      integer      NOT NULL,
    description  text,
    is_active    boolean      NOT NULL DEFAULT true,
    created_at   timestamptz  NOT NULL DEFAULT now(),
    updated_at   timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_usecase_kpi_configs_usecase_version UNIQUE (usecase_id, version)
);
CREATE INDEX ix_usecase_kpi_configs_usecase_active
    ON usecase_kpi_configs (usecase_id, is_active);

-- Which KPIs that version includes. is_selected=false keeps a KPI on the
-- record as deliberately excluded rather than deleting the row.
CREATE TABLE usecase_kpi_selections (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    config_id   uuid        NOT NULL REFERENCES usecase_kpi_configs(id) ON DELETE CASCADE,
    kpi_id      uuid        NOT NULL REFERENCES kpis(id) ON DELETE CASCADE,
    is_selected boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_usecase_kpi_selections_config_kpi UNIQUE (config_id, kpi_id)
);
CREATE INDEX ix_usecase_kpi_selections_config_selected
    ON usecase_kpi_selections (config_id, is_selected);

-- Pins ONE config version to ONE batch. ON DELETE RESTRICT on the config:
-- a config a batch was scored against must not be deletable.
CREATE TABLE batch_kpi_configs (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id              bigint      NOT NULL UNIQUE
                                      REFERENCES transcription_batches(id) ON DELETE CASCADE,
    usecase_kpi_config_id uuid        NOT NULL
                                      REFERENCES usecase_kpi_configs(id) ON DELETE RESTRICT,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_batch_kpi_configs_usecase_config_id
    ON batch_kpi_configs (usecase_kpi_config_id);
```

### Execution

```sql
-- One pipeline execution. `metadata` carries the pipeline config the DAG reads.
CREATE TABLE transcription_batches (
    id                 bigserial   PRIMARY KEY,
    batch_name         varchar(255),
    usecase_id         varchar(255),
    usecase_name       varchar(255),
    metadata           jsonb,
    total_files        integer,
    completed_files    integer,
    processing_files   integer,
    pending_files      integer,
    failed_files       jsonb,
    failed_files_count integer,
    status             varchar(50),
    schedule_type      varchar(20),
    "source"           varchar(64) NOT NULL DEFAULT 'audio_upload',
    metric             varchar(255),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz
);
CREATE INDEX ix_transcription_batches_source ON transcription_batches ("source");
```

> ⚠️ **`source` exists twice with different values.** The `source` COLUMN routes
> the DAG (`trigger_dag_for_batch` reads it) and keeps its ORM default,
> `'audio_upload'`. `metadata->>'source'` records how the files actually
> arrived, e.g. `'zip_pipeline_upload'`. In production these disagree. **Read
> the column.**

> ⚠️ **`metric` is written and never read.** All three upload endpoints set it;
> nothing branches on it.

```sql
-- One file. Written by the DAG's transcription tasks.
CREATE TABLE transcriptions (
    id                    bigserial   PRIMARY KEY,
    batch_id              bigint      NOT NULL REFERENCES transcription_batches(id),
    filename              text,
    transcript            text,               -- anonymised
    translated_transcript text,               -- translated FROM the anonymised text
    detected_language     varchar(20),
    file_path             varchar(512),       -- nulled by cleanup: "0 recordings retained"
    status                varchar(50) DEFAULT 'pending',
    error_message         text,
    processing_time_ms    float8,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz,
    -- Flow B only. The audio pipeline leaves these NULL / '{}'.
    canonical_transcript  jsonb,
    external_id           varchar(256),
    content_hash          bpchar(32),
    transcript_metadata   jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX uq_transcriptions_external_id
    ON transcriptions (external_id) WHERE external_id IS NOT NULL;

-- Fires once. See docs/proposals/flows-table.md for why this cannot express
-- a recurring schedule.
CREATE TABLE scheduled_batch_queue (
    id                     serial      PRIMARY KEY,
    transcription_batch_id bigint      NOT NULL REFERENCES transcription_batches(id),
    scheduled_for          timestamptz NOT NULL,
    triggered              boolean     NOT NULL DEFAULT false,
    triggered_at           timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now()
);
```

### Results

```sql
-- The ANALYSIS batch. Note the UUID id -- not the same thing as
-- transcription_batches.id, which is a bigint.
CREATE TABLE batches (
    id                     uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    usecase_id             varchar(255),
    usecase_name           varchar(255),
    name                   varchar(255),
    status                 varchar(50)  NOT NULL DEFAULT 'pending',
    total_calls            integer      NOT NULL DEFAULT 0,
    pending_calls          integer      NOT NULL DEFAULT 0,
    processing_calls       integer      NOT NULL DEFAULT 0,
    successful_calls       integer      NOT NULL DEFAULT 0,
    failed_calls           integer      NOT NULL DEFAULT 0,
    transcription_batch_id bigint       REFERENCES transcription_batches(id) ON DELETE SET NULL,
    uploaded_at            timestamptz  NOT NULL DEFAULT now(),
    uploaded_by            uuid,
    is_deleted             boolean      DEFAULT false,
    deleted_at             timestamptz,
    created_at             timestamptz  NOT NULL DEFAULT now(),
    updated_at             timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ck_batch_counts CHECK (
        total_calls >= 0 AND pending_calls >= 0 AND processing_calls >= 0
        AND successful_calls >= 0 AND failed_calls >= 0)
);

-- One scored call. `metadata.agent_name` is what the Agent Communication
-- Report aggregates on, which is why the GIN index is there.
CREATE TABLE calls (
    id               uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id         uuid         NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    filename         varchar(500) NOT NULL,
    file_url         text,
    duration_sec     integer,                 -- from the transcript's last timestamp
    status           varchar(50)  NOT NULL DEFAULT 'pending',
    transcript       text,
    external_call_id varchar(255) NOT NULL,   -- the DAG sets this to transcriptions.id
    metadata         jsonb,
    uploaded_at      timestamptz  NOT NULL DEFAULT now(),
    is_deleted       boolean      DEFAULT false,
    deleted_at       timestamptz,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    updated_at       timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX ix_calls_batch_id_status ON calls (batch_id, status, uploaded_at);
CREATE INDEX ix_calls_metadata        ON calls USING gin (metadata);

-- One per call. Risk flags are indexed because dashboards filter on them.
CREATE TABLE call_analyses (
    id                          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id                     uuid         NOT NULL UNIQUE REFERENCES calls(id) ON DELETE CASCADE,
    batch_id                    uuid         NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    status                      varchar(50)  NOT NULL DEFAULT 'pending',
    overall_score               numeric(5,2),
    error_message               text,
    analyzed_at                 timestamptz  NOT NULL DEFAULT now(),
    high_risk_call              boolean      NOT NULL DEFAULT false,
    manual_review_required      boolean      NOT NULL DEFAULT false,
    compliance_violation        boolean      NOT NULL DEFAULT false,
    privacy_violation           boolean      NOT NULL DEFAULT false,
    mis_selling_alert           boolean      NOT NULL DEFAULT false,
    risk_detail                 jsonb,
    topics                      jsonb,
    call_impact_level           varchar(10),
    call_impact_reason          text,
    customer_experience_drivers jsonb,
    is_deleted                  boolean      DEFAULT false,
    deleted_at                  timestamptz,
    created_at                  timestamptz  NOT NULL DEFAULT now(),
    updated_at                  timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ck_call_analyses_impact_level
        CHECK (call_impact_level IN ('High','Low') OR call_impact_level IS NULL)
);
CREATE INDEX ix_call_analyses_batch_status_score
    ON call_analyses (batch_id, status, overall_score);

-- One row per (call, KPI). `references` is a reserved word -- quote it.
CREATE TABLE analysis_kpi_results (
    id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id     uuid         NOT NULL REFERENCES call_analyses(id) ON DELETE CASCADE,
    kpi_id          uuid         NOT NULL REFERENCES kpis(id) ON DELETE CASCADE,
    raw_score       varchar(500),
    score           numeric(5,2),
    rationale       text,
    positive_impact text,
    negative_impact text,
    observable      boolean      NOT NULL DEFAULT false,
    applicable      boolean      NOT NULL DEFAULT false,
    attempted       boolean      NOT NULL DEFAULT false,
    "references"    jsonb,       -- [{turn_index, speaker, quote}]
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_analysis_kpi UNIQUE (analysis_id, kpi_id)
);
CREATE INDEX ix_akr_kpi_score  ON analysis_kpi_results (kpi_id, score);
CREATE INDEX ix_akr_references ON analysis_kpi_results USING gin ("references");
```

---

## Things this schema cannot currently record

Worth deciding on before go-live, not after:

| Missing | Consequence |
| --- | --- |
| **Anonymisation status** | Nothing distinguishes a scrubbed transcript from an unscrubbed one. If the claim is "PII is removed", the database cannot evidence it. An `anonymised_at timestamptz` on `transcriptions` costs nothing. |
| **Token usage / cost** | The wireframe's `₹1.90 per call` and `₹28,150 per run` cannot be computed. Neither repo captures `response.usage`. |
| **A recurring schedule** | `scheduled_batch_queue.triggered` is a one-way boolean. See `docs/proposals/flows-table.md`. |
| **Which KPI codes had no prompt** | The DAG writes them into `transcription_batches.metadata`, which is a workaround, not a column. |

## A note on `overall_score`

The library deliberately produces no overall score -- the wireframe shows KPIs
grouped by objective and never a single headline number, and a shared library
is the wrong place to fix a weighting policy still being decided.

The column exists and dashboards read it, so the DAG computes one: an
**unweighted mean of the scored, applicable KPIs**, which is exactly what
`save_analysis_results` did. Note that `kpis.weight` and `kpi_sections.weight`
both exist and are ignored by that calculation. Reproducing the existing
behaviour keeps old and new rows comparable; changing it is a product decision,
and should be made once rather than drifting.
