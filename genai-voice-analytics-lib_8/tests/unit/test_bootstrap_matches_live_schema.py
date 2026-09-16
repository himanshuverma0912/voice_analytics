"""`bootstrap.sql` must create the same columns the live database has.

Two files describe the same nine tables:

* `live-schema.sql` -- exported from the real database. Column names, types and
  nullability only; DBeaver's simplified DDL omits keys, defaults and indexes.
* `bootstrap.sql` -- creates them locally, with those omissions added back from
  the ORM so a local database behaves like the real one.

The second is only useful if it matches the first. This checks that
mechanically, because reading two DDL files side by side is exactly the kind of
comparison a person does badly -- it already caught a malformed line in the
exported fixture, where the PDF extraction pasted a repeated header onto a
column definition.

Additions in `bootstrap.sql` -- primary keys, foreign keys, defaults, indexes
-- are deliberately ignored here: they are what the export omits, not
disagreements with it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "docs" / "schema"
_LIVE = _SCHEMA_DIR / "live-schema.sql"
_BOOTSTRAP = _SCHEMA_DIR / "bootstrap.sql"

TABLES = [
    "calls", "call_analyses", "analysis_kpi_results", "batches",
    "batch_kpi_configs", "usecase_kpi_selections", "kpis", "kpi_sections",
    "usecase_kpi_configs",
]

#: Spellings Postgres treats as the same type.
_ALIASES = {
    "int4": "integer", "int8": "bigint", "bool": "boolean",
    "float8": "double precision", "bpchar": "char",
    "serial": "integer", "bigserial": "bigint",
}


def _table_body(sql: str, table: str) -> str | None:
    match = re.search(
        rf"CREATE TABLE (?:IF NOT EXISTS )?(?:\w+\.)?{table} \((.*?)\n\);", sql, re.S)
    return match.group(1) if match else None


def _split_definitions(text: str) -> list[str]:
    """Split on top-level commas, so ``numeric(5, 2)`` is not torn in half."""
    parts, depth, buffer = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(buffer)
            buffer = ""
        else:
            buffer += char
    parts.append(buffer)
    return [re.sub(r"--[^\n]*", "", p).strip() for p in parts if p.strip()]


def _columns(sql: str, table: str) -> list[tuple[str, str]]:
    body = _table_body(sql, table)
    assert body is not None, f"{table} not found"
    columns = []
    for definition in _split_definitions(body):
        definition = re.sub(r"\s+", " ", definition).strip()
        if definition.upper().startswith(
            ("CONSTRAINT", "PRIMARY KEY", "UNIQUE (", "CHECK", "FOREIGN KEY")
        ):
            continue
        match = re.match(r'"?([A-Za-z_][A-Za-z0-9_]*)"?\s+(.*)', definition)
        if match:
            columns.append((match.group(1), match.group(2)))
    return columns


def _normalise(spec: str) -> tuple[str, bool]:
    """``(type, nullable)``, with defaults, keys and references stripped."""
    text = re.sub(r"\s+", " ", spec.lower())
    text = re.sub(r"\bdefault\b.*$", "", text)
    text = re.sub(r"\breferences\b.*$", "", text)
    nullable = not ("not null" in text or "primary key" in text)
    for keyword in ("primary key", "not null", "unique", "null"):
        text = text.replace(keyword, "")
    text = re.sub(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", r"(\1,\2)", text)
    text = re.sub(r"\s+", " ", text).strip()
    for alias, canonical in _ALIASES.items():
        text = re.sub(rf"\b{alias}\b", canonical, text)
    return text, nullable


LIVE = _LIVE.read_text()
BOOTSTRAP = _BOOTSTRAP.read_text()


def test_both_files_describe_every_table():
    for table in TABLES:
        assert _table_body(LIVE, table) is not None, f"{table} missing from live-schema.sql"
        assert _table_body(BOOTSTRAP, table) is not None, f"{table} missing from bootstrap.sql"


@pytest.mark.parametrize("table", TABLES)
def test_the_same_columns_in_the_same_order(table):
    live = [name for name, _ in _columns(LIVE, table)]
    local = [name for name, _ in _columns(BOOTSTRAP, table)]
    assert live == local


@pytest.mark.parametrize("table", TABLES)
def test_the_same_types_and_nullability(table):
    for (name, live_spec), (_, local_spec) in zip(
        _columns(LIVE, table), _columns(BOOTSTRAP, table)
    ):
        assert _normalise(live_spec) == _normalise(local_spec), (
            f"{table}.{name}: live={_normalise(live_spec)} "
            f"bootstrap={_normalise(local_spec)}"
        )


def test_no_column_definition_swallowed_a_stray_create_table():
    """The PDF extraction repeated two headers and pasted them onto a column.

    It produced a fixture that parsed without error and was wrong, which is the
    worst kind. Cheap to assert, so assert it.
    """
    assert "CREATE TABLE information_schema" not in LIVE
    for table in TABLES:
        for name, _ in _columns(LIVE, table):
            assert name.upper() != "CREATE", f"{table} has a malformed column line"


def test_bootstrap_adds_what_the_export_omits():
    """The point of bootstrap.sql: the export carries no keys or constraints."""
    assert "PRIMARY KEY" not in LIVE
    assert "REFERENCES" not in LIVE
    assert BOOTSTRAP.count("PRIMARY KEY") >= 9
    assert "REFERENCES batches(id)" in BOOTSTRAP
    assert "ck_call_analyses_impact_level" in BOOTSTRAP
    assert "uq_analysis_kpi" in BOOTSTRAP
