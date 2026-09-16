"""The persistence layer's SQL must match the live schema.

A wrong column name is not caught by anything until it runs -- and it runs
mid-batch, after the transcription has already been paid for. The linter cannot
see inside a string, and the integration tests need a Postgres that not every
machine has.

So the live schema is committed as a fixture (`docs/schema/live-schema.sql`,
exported from the database) and every column written by `voice_analytics_store`
is checked against it here, with no database required. The check is cheap and
it has already caught three real mistakes: `batch_kpi_configs.kpi_code` (no
such column), an unquoted `references` (reserved word), and a `calls` insert
missing `NOT NULL` columns.

`tests/integration/` covers what this cannot -- that the SQL parses, the
constraints hold, and a full run leaves rows that add up -- but it needs a real
Postgres. This one runs everywhere.

When the database changes, re-export the fixture. If a column the pipeline
depends on has gone, this test says which one before a batch does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA = _ROOT / "docs" / "schema" / "live-schema.sql"
_STORE = _ROOT / "src" / "voice_analytics_store"
_DAGS = sorted((_ROOT / "examples" / "airflow").glob("*.py"))

#: Tables whose DDL was exported separately, before the full dump. Their
#: columns are listed here so the DAG's SQL against them is checked too.
_EXTRA_TABLES = {
    "transcription_batches": {
        "id", "batch_name", "usecase_id", "metadata", "total_files",
        "failed_files", "created_at", "status", "completed_files",
        "processing_files", "pending_files", "updated_at",
        "failed_files_count", "usecase_name", "schedule_type", "source",
        "metric",
    },
    "transcriptions": {
        "id", "batch_id", "filename", "transcript", "translated_transcript",
        "created_at", "status", "error_message", "processing_time_ms",
        "updated_at", "file_path", "detected_language", "canonical_transcript",
        "external_id", "content_hash", "transcript_metadata",
    },
}


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _live_schema() -> dict[str, set[str]]:
    """Parse the exported DDL into ``{table: {column, ...}}``."""
    text = _SCHEMA.read_text()
    tables: dict[str, set[str]] = dict(_EXTRA_TABLES)
    for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", text, re.S):
        name, body = match.group(1), match.group(2)
        columns = set()
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            column = re.match(r'"?([A-Za-z_][A-Za-z0-9_]*)"?', line)
            if column:
                columns.add(column.group(1))
        tables[name] = columns
    return tables


def _inserts(dag: str):
    """Yield ``(table, [columns], value_count)`` for every INSERT in the DAG."""
    pattern = r'INSERT INTO (\w+)\s*\(([^)]*)\)\s*VALUES\s*\(([^;]*?)\)\s*(?:RETURNING|ON CONFLICT|""")'
    for match in re.finditer(pattern, dag, re.S):
        columns = [
            c.strip().strip('"')
            for c in _strip_comments(match.group(2)).split(",")
            if c.strip()
        ]
        values = [v for v in _strip_comments(match.group(3)).split(",")]
        yield match.group(1), columns, len(values)


def _updates(dag: str):
    """Yield ``(table, [columns])`` for every UPDATE in the DAG."""
    for match in re.finditer(r'UPDATE (\w+)\s*\n?\s*SET (.*?)(?:WHERE|""")', dag, re.S):
        assignments = re.findall(r"([a-z_]+)\s*=", _strip_comments(match.group(2)))
        yield match.group(1), assignments


SCHEMA = _live_schema()
DAG_SOURCE = "\n".join(path.read_text() for path in _DAGS)
#: Every SQL-bearing module. The DAG delegates to the store, so the store is
#: where the statements now live -- but the DAG is scanned too, so SQL creeping
#: back into it would still be checked rather than silently unverified.
STORE_SOURCE = "\n".join(
    path.read_text() for path in sorted(_STORE.glob("*.py"))
)
SQL_SOURCE = STORE_SOURCE + "\n" + DAG_SOURCE
INSERTS = list(_inserts(SQL_SOURCE))
UPDATES = list(_updates(SQL_SOURCE))


def test_the_fixture_and_the_sql_were_both_found():
    """Guard against the parsing silently matching nothing and passing."""
    assert len(SCHEMA) >= 11, f"only parsed {sorted(SCHEMA)}"
    assert len(INSERTS) >= 4, "no INSERT statements parsed out of the source"
    assert len(UPDATES) >= 4, "no UPDATE statements parsed out of the source"


@pytest.mark.parametrize(
    ("table", "columns"),
    [(t, c) for t, c, _ in INSERTS],
    ids=[f"insert-{t}-{i}" for i, (t, _, _) in enumerate(INSERTS)],
)
def test_every_inserted_column_exists(table, columns):
    assert table in SCHEMA, f"INSERT into unknown table {table!r}"
    unknown = [c for c in columns if c not in SCHEMA[table]]
    assert not unknown, (
        f"{table} has no column(s) {unknown}. Known columns: "
        f"{sorted(SCHEMA[table])}"
    )


@pytest.mark.parametrize(
    ("table", "columns", "value_count"),
    INSERTS,
    ids=[f"arity-{t}-{i}" for i, (t, _, _) in enumerate(INSERTS)],
)
def test_every_insert_supplies_one_value_per_column(table, columns, value_count):
    assert value_count == len(columns), (
        f"INSERT INTO {table} names {len(columns)} columns but supplies "
        f"{value_count} values"
    )


@pytest.mark.parametrize(
    ("table", "columns"),
    UPDATES,
    ids=[f"update-{t}-{i}" for i, (t, _) in enumerate(UPDATES)],
)
def test_every_updated_column_exists(table, columns):
    assert table in SCHEMA, f"UPDATE of unknown table {table!r}"
    unknown = [c for c in columns if c not in SCHEMA[table]]
    assert not unknown, f"{table} has no column(s) {unknown}"


def test_references_is_quoted_because_it_is_a_reserved_word():
    """Unquoted, the analysis_kpi_results insert is a syntax error."""
    assert 'attempted, "references"' in SQL_SOURCE


def test_kpi_codes_are_resolved_through_the_batchs_pinned_config():
    """Reading the use case's current config would score a batch against a
    different KPI set than the batch it is compared with."""
    assert "batch_kpi_configs" in SQL_SOURCE
    assert "usecase_kpi_selections" in SQL_SOURCE
    assert "sel.is_selected IS TRUE" in SQL_SOURCE


@pytest.mark.parametrize("dag_path", _DAGS, ids=[p.stem for p in _DAGS])
def test_no_dag_imports_the_compute_library(dag_path):
    """The library reaches a DAG as a container image, never as an import.

    `voice_analytics_store` is a different matter -- see ADR 0007 -- so this
    checks the compute package specifically, not the word "voice_analytics".
    """
    import ast

    modules = set()
    for node in ast.walk(ast.parse(dag_path.read_text())):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])

    assert "voice_analytics" not in modules, (
        f"{dag_path.name} imports the compute library. Anything it needs from "
        "it should be a CLI subcommand instead."
    )
    assert "voice_analytics_store" in modules


def test_both_dags_were_found():
    """Guard against the glob silently matching nothing."""
    names = {p.name for p in _DAGS}
    assert names == {"voice_analytics_pipeline.py", "voice_analytics_transcribe.py"}, names
