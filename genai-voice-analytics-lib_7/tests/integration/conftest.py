"""Fixtures for tests that need a real Postgres.

These are **not** mocked. Mocking a database connection tests that the code
calls the methods the mock expects, which is a different thing from testing
that the SQL is valid, the column names exist, the constraints hold, and the
types line up -- exactly the mistakes worth catching here.

Point them at a throwaway database::

    createdb voice_analytics_test
    export VOICE_ANALYTICS_TEST_DSN=postgresql://localhost/voice_analytics_test
    uv run pytest tests/integration -v

Without that variable the whole directory is skipped, so `pytest` still passes
on a machine with no Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

DSN_VARIABLE = "VOICE_ANALYTICS_TEST_DSN"
_BOOTSTRAP = Path(__file__).resolve().parents[2] / "docs" / "schema" / "bootstrap.sql"


def _dsn() -> str | None:
    return os.environ.get(DSN_VARIABLE)


pytestmark = pytest.mark.skipif(
    not _dsn(),
    reason=f"{DSN_VARIABLE} is not set; see tests/integration/conftest.py",
)


@pytest.fixture(scope="session")
def dsn() -> str:
    value = _dsn()
    if not value:
        pytest.skip(f"{DSN_VARIABLE} is not set")
    return value


@pytest.fixture(scope="session")
def schema(dsn):
    """Create the tables once per session, from the shipped bootstrap DDL."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(_BOOTSTRAP.read_text())
    return dsn


@pytest.fixture()
def conn(schema):
    """A connection whose work is rolled back afterwards.

    Every test starts from the same empty tables without the cost of
    recreating them, and a failing test leaves no debris for the next one.
    """
    import psycopg

    connection = psycopg.connect(schema)
    try:
        yield connection
    finally:
        connection.rollback()
        # Truncate rather than relying on the rollback alone: a test that
        # commits deliberately must not leak into the next one.
        with connection.cursor() as cur:
            cur.execute(
                "TRUNCATE analysis_kpi_results, call_analyses, calls, batches, "
                "transcriptions, batch_kpi_configs, usecase_kpi_selections, "
                "usecase_kpi_configs, kpis, kpi_sections, transcription_batches "
                "RESTART IDENTITY CASCADE"
            )
        connection.commit()
        connection.close()


@pytest.fixture()
def batch(conn):
    """A transcription batch with two KPIs pinned to it, ready to run.

    Builds the whole configuration chain the pipeline resolves through:
    kpi_sections -> kpis -> usecase_kpi_configs -> usecase_kpi_selections
    -> batch_kpi_configs -> transcription_batches.
    """
    import json

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO kpi_sections (name, display_order) VALUES ('Compliance', 1) "
            "RETURNING id"
        )
        section_id = cur.fetchone()[0]

        kpi_ids = {}
        for order, code in enumerate(("rpc_verified", "disclosure_given")):
            cur.execute(
                "INSERT INTO kpis (section_id, name, kpi_code, display_order) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (section_id, code.replace("_", " ").title(), code, order),
            )
            kpi_ids[code] = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO usecase_kpi_configs (usecase_id, usecase_name, version) "
            "VALUES ('1149', 'Voice-Analytics', 1) RETURNING id"
        )
        config_id = cur.fetchone()[0]

        for kpi_id in kpi_ids.values():
            cur.execute(
                "INSERT INTO usecase_kpi_selections (config_id, kpi_id, is_selected) "
                "VALUES (%s, %s, true)",
                (config_id, kpi_id),
            )

        cur.execute(
            """
            INSERT INTO transcription_batches
                   (batch_name, usecase_id, usecase_name, metadata, status,
                    schedule_type, source)
            VALUES ('local test', '1149', 'Voice-Analytics', %s::jsonb, 'pending',
                    'run_now', 'audio_upload')
            RETURNING id
            """,
            (json.dumps({"target_language": "English", "romanize": False,
                         "source": "zip_pipeline_upload"}),),
        )
        batch_id = int(cur.fetchone()[0])

        cur.execute(
            "INSERT INTO batch_kpi_configs (batch_id, usecase_kpi_config_id) "
            "VALUES (%s, %s)",
            (batch_id, config_id),
        )
    conn.commit()

    return {"batch_id": batch_id, "config_id": config_id, "kpi_ids": kpi_ids,
            "section_id": section_id}
