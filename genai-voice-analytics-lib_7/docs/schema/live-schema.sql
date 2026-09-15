-- Live schema, exported from the database with DBeaver.
-- Source of truth for the SQL in examples/airflow/.
--
-- NOTE: this export carries column names, types and nullability only.
-- DEFAULT clauses, primary keys, foreign keys, unique constraints and
-- indexes are NOT included -- DBeaver's simplified DDL omits them. Where
-- the DAG depends on one, it is called out in README.md.

CREATE TABLE calls (
    id uuid NOT NULL,
    batch_id uuid NOT NULL,
    filename varchar(500) NULL,
    file_url text NULL,
    duration_sec int4 NULL,
    status varchar(50) NULL,
    transcript text NULL,
    metadata jsonb NULL,
    uploaded_at timestamp NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz NULL,
    is_deleted bool NOT NULL,
    external_call_id varchar(255) NULL
);

CREATE TABLE call_analyses (
    id uuid NOT NULL,
    call_id uuid NOT NULL,
    batch_id uuid NOT NULL,
    analyzed_at timestamp NOT NULL,
    status varchar(50) NULL,
    overall_score numeric(5, 2) NULL,
    error_message text NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz NULL,
    is_deleted bool NOT NULL,
    high_risk_call bool NOT NULL,
    manual_review_required bool NOT NULL,
    compliance_violation bool NOT NULL,
    privacy_violation bool NOT NULL,
    mis_selling_alert bool NOT NULL,
    risk_detail jsonb NULL,
    topics jsonb NULL,
    customer_experience_drivers jsonb NULL,
    call_impact_level varchar(10) NULL,
    call_impact_reason text NULL
);

CREATE TABLE analysis_kpi_results (
    id uuid NOT NULL,
    analysis_id uuid NOT NULL,
    kpi_id uuid NOT NULL,
    raw_score varchar(500) NULL,
    score numeric(5, 2) NULL,
    confidence numeric(3, 2) NULL,
    rationale text NULL,
    "references" jsonb NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    positive_impact text NULL,
    negative_impact text NULL,
    observable bool NOT NULL,
    applicable bool NOT NULL,
    attempted bool NOT NULL
);

CREATE TABLE batches (
    id uuid NOT NULL,
    usecase_id varchar(255) NULL,
    "name" varchar(255) NULL,
    status varchar(50) NULL,
    total_calls int4 NOT NULL,
    pending_calls int4 NOT NULL,
    processing_calls int4 NOT NULL,
    successful_calls int4 NOT NULL,
    failed_calls int4 NOT NULL,
    uploaded_at timestamp NOT NULL,
    uploaded_by uuid NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz NULL,
    is_deleted bool NOT NULL,
    transcription_batch_id int8 NULL,
    usecase_name varchar(255) NULL
);

CREATE TABLE batch_kpi_configs (
    id uuid NOT NULL,
    batch_id int8 NOT NULL,
    usecase_kpi_config_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE usecase_kpi_selections (
    id uuid NOT NULL,
    config_id uuid NOT NULL,
    kpi_id uuid NOT NULL,
    is_selected bool NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE kpis (
    id uuid NOT NULL,
    section_id uuid NOT NULL,
    "name" varchar(255) NULL,
    description text NULL,
    weight numeric(5, 2) NULL,
    display_order int4 NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    kpi_code varchar(255) NULL
);

CREATE TABLE kpi_sections (
    id uuid NOT NULL,
    "name" varchar(255) NULL,
    description text NULL,
    weight numeric(5, 2) NULL,
    display_order int4 NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE usecase_kpi_configs (
    id uuid NOT NULL,
    usecase_id varchar(255) NULL,
    usecase_name varchar(255) NULL,
    "version" int4 NOT NULL,
    description text NULL,
    is_active bool NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
