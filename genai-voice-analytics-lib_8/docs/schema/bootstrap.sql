-- Create the nine tables the pipeline uses, for a throwaway local database.
--
-- Built from docs/schema/live-schema.sql (exported from the real database),
-- with the primary keys, foreign keys, defaults and constraints that the
-- export omitted added back from the originating ORM.
--
--   createdb voice_analytics_local
--   psql voice_analytics_local -f docs/schema/bootstrap.sql
--
-- This is for local testing. Do not run it against a real environment -- there
-- the tables already exist, and this would not match them exactly.

-- Minimum Postgres 13. Nothing here needs anything newer:
--
--   gen_random_uuid()      core from 13   (before that, the pgcrypto extension)
--   FILTER (WHERE ...)     9.4
--   jsonb, GIN on jsonb    9.4
--   bigserial              ancient
--
-- Verified against 16. Refuse early rather than fail halfway through with an
-- error that does not name the cause.
DO $$
BEGIN
    IF current_setting('server_version_num')::int < 130000 THEN
        RAISE EXCEPTION
            'This schema needs Postgres 13 or newer (found %). Older versions '
            'have no built-in gen_random_uuid(); install the pgcrypto extension '
            'and remove this check if you must use one.',
            current_setting('server_version');
    END IF;
END
$$;

-- ---------------------------------------------------------------- config ---

CREATE TABLE IF NOT EXISTS kpi_sections (
    id            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    name          varchar(255),
    description   text,
    weight        numeric(5,2) DEFAULT 1.00,
    display_order integer      NOT NULL DEFAULT 0,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kpis (
    id            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id    uuid         NOT NULL REFERENCES kpi_sections(id) ON DELETE CASCADE,
    name          varchar(255),
    description   text,
    weight        numeric(5,2) DEFAULT 1.00,
    display_order integer      NOT NULL DEFAULT 0,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now(),
    kpi_code      varchar(255)
);

CREATE TABLE IF NOT EXISTS usecase_kpi_configs (
    id           uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    usecase_id   varchar(255),
    usecase_name varchar(255),
    version      integer      NOT NULL,
    description  text,
    is_active    boolean      NOT NULL DEFAULT true,
    created_at   timestamptz  NOT NULL DEFAULT now(),
    updated_at   timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usecase_kpi_selections (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    config_id   uuid        NOT NULL REFERENCES usecase_kpi_configs(id) ON DELETE CASCADE,
    kpi_id      uuid        NOT NULL REFERENCES kpis(id) ON DELETE CASCADE,
    is_selected boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_usecase_kpi_selections_config_kpi UNIQUE (config_id, kpi_id)
);

-- ------------------------------------------------------------- execution ---

CREATE TABLE IF NOT EXISTS transcription_batches (
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

-- Pins ONE config version to ONE batch. batch_id is UNIQUE: one batch, one
-- pinned config, forever -- which is what keeps two runs comparable.
CREATE TABLE IF NOT EXISTS batch_kpi_configs (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id              bigint      NOT NULL UNIQUE
                                      REFERENCES transcription_batches(id) ON DELETE CASCADE,
    usecase_kpi_config_id uuid        NOT NULL
                                      REFERENCES usecase_kpi_configs(id) ON DELETE RESTRICT,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transcriptions (
    id                    bigserial   PRIMARY KEY,
    batch_id              bigint      NOT NULL REFERENCES transcription_batches(id),
    filename              text,
    transcript            text,
    translated_transcript text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    status                varchar(50) DEFAULT 'pending',
    error_message         text,
    processing_time_ms    double precision,
    updated_at            timestamptz,
    file_path             varchar(512),
    detected_language     varchar(20),
    canonical_transcript  jsonb,
    external_id           varchar(256),
    content_hash          char(32),
    transcript_metadata   jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- --------------------------------------------------------------- results ---

CREATE TABLE IF NOT EXISTS batches (
    id                     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    usecase_id             varchar(255),
    name                   varchar(255),
    status                 varchar(50),
    total_calls            integer     NOT NULL DEFAULT 0,
    pending_calls          integer     NOT NULL DEFAULT 0,
    processing_calls       integer     NOT NULL DEFAULT 0,
    successful_calls       integer     NOT NULL DEFAULT 0,
    failed_calls           integer     NOT NULL DEFAULT 0,
    uploaded_at            timestamp   NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    uploaded_by            uuid,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    deleted_at             timestamptz,
    is_deleted             boolean     NOT NULL DEFAULT false,
    transcription_batch_id bigint      REFERENCES transcription_batches(id) ON DELETE SET NULL,
    usecase_name           varchar(255),
    CONSTRAINT ck_batch_counts CHECK (
        total_calls >= 0 AND pending_calls >= 0 AND processing_calls >= 0
        AND successful_calls >= 0 AND failed_calls >= 0)
);

CREATE TABLE IF NOT EXISTS calls (
    id               uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id         uuid         NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    filename         varchar(500),
    file_url         text,
    duration_sec     integer,
    status           varchar(50),
    transcript       text,
    metadata         jsonb,
    uploaded_at      timestamp    NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    created_at       timestamptz  NOT NULL DEFAULT now(),
    updated_at       timestamptz  NOT NULL DEFAULT now(),
    deleted_at       timestamptz,
    is_deleted       boolean      NOT NULL DEFAULT false,
    external_call_id varchar(255)
);
CREATE INDEX IF NOT EXISTS ix_calls_metadata ON calls USING gin (metadata);

CREATE TABLE IF NOT EXISTS call_analyses (
    id                          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id                     uuid         NOT NULL UNIQUE REFERENCES calls(id) ON DELETE CASCADE,
    batch_id                    uuid         NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    analyzed_at                 timestamp    NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    status                      varchar(50),
    overall_score               numeric(5,2),
    error_message               text,
    created_at                  timestamptz  NOT NULL DEFAULT now(),
    updated_at                  timestamptz  NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    is_deleted                  boolean      NOT NULL DEFAULT false,
    high_risk_call              boolean      NOT NULL DEFAULT false,
    manual_review_required      boolean      NOT NULL DEFAULT false,
    compliance_violation        boolean      NOT NULL DEFAULT false,
    privacy_violation           boolean      NOT NULL DEFAULT false,
    mis_selling_alert           boolean      NOT NULL DEFAULT false,
    risk_detail                 jsonb,
    topics                      jsonb,
    customer_experience_drivers jsonb,
    call_impact_level           varchar(10),
    call_impact_reason          text,
    CONSTRAINT ck_call_analyses_impact_level
        CHECK (call_impact_level IN ('High','Low') OR call_impact_level IS NULL)
);

CREATE TABLE IF NOT EXISTS analysis_kpi_results (
    id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_id     uuid         NOT NULL REFERENCES call_analyses(id) ON DELETE CASCADE,
    kpi_id          uuid         NOT NULL REFERENCES kpis(id) ON DELETE CASCADE,
    raw_score       varchar(500),
    score           numeric(5,2),
    confidence      numeric(3,2),
    rationale       text,
    "references"    jsonb,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    positive_impact text,
    negative_impact text,
    observable      boolean      NOT NULL DEFAULT false,
    applicable      boolean      NOT NULL DEFAULT false,
    attempted       boolean      NOT NULL DEFAULT false,
    CONSTRAINT uq_analysis_kpi UNIQUE (analysis_id, kpi_id)
);
