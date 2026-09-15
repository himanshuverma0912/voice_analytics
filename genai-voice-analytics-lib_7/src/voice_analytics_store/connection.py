"""Getting a Postgres connection.

``psycopg`` is imported lazily so the core library -- and any image that only
transcribes or scores -- does not carry a database driver it never uses.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

#: Environment variables checked, in order, for a connection string.
DSN_VARIABLES = ("VOICE_ANALYTICS_DSN", "DATABASE_URL", "POSTGRES_DSN")

#: Read as a fallback, matching what ``load_settings`` does for the library's
#: own settings. Without this a ``.env`` file would configure the gateway but
#: not the database, which is the kind of half-working that costs an afternoon.
DEFAULT_ENV_FILE = ".env"


class StoreError(Exception):
    """The database could not be reached, or a write could not be completed."""


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise StoreError(
            "Writing to Postgres needs the 'store' extra: "
            "pip install 'genai-voice-analytics-lib[store]'"
        ) from exc
    return psycopg


def dsn_from_env(env_file: str | None = DEFAULT_ENV_FILE) -> str:
    """The connection string, from the environment or a ``.env`` file.

    Real environment variables win over the file, so a one-off override on the
    command line still works. The file is read because the library's own
    settings are read from it too -- a ``.env`` that configured the gateway but
    not the database would be the kind of half-working that costs an afternoon.

    Raises:
        StoreError: None of them is set. The message names all three variables
            rather than only the first, so a reader can use whichever their
            environment already defines.
    """
    for name in DSN_VARIABLES:
        value = os.environ.get(name)
        if value:
            return value

    if env_file and os.path.isfile(env_file):
        try:
            from dotenv import dotenv_values
        except ImportError:  # pragma: no cover - ships with pydantic-settings
            dotenv_values = None
        if dotenv_values is not None:
            values = dotenv_values(env_file)
            for name in DSN_VARIABLES:
                value = values.get(name)
                if value:
                    return value

    raise StoreError(
        "No database connection string. Set one of "
        + ", ".join(DSN_VARIABLES)
        + " in the environment or in a .env file beside the project, "
        "e.g. postgresql://user:pass@localhost:5432/voice_analytics_local"
    )


def open_connection(dsn: str | None = None, autocommit: bool = False) -> Any:
    """Open a connection the caller will close itself.

    Use :func:`connect` where a ``with`` block fits. This exists for a caller
    that holds one connection across a long run and closes it in its own
    ``finally`` -- calling ``connect(...).__enter__()`` instead would leave the
    generator unreferenced, and the connection closes when it is collected.

    Raises:
        StoreError: The driver is not installed, or the server refused the
            connection.
    """
    psycopg = _psycopg()
    try:
        return psycopg.connect(dsn or dsn_from_env(), autocommit=autocommit)
    except psycopg.Error as exc:
        raise StoreError(f"Could not connect to Postgres: {exc}") from exc


@contextmanager
def connect(dsn: str | None = None, autocommit: bool = False) -> Iterator[Any]:
    """Open a connection, closing it on the way out.

    Args:
        dsn: Connection string. Read from the environment when omitted.
        autocommit: Leave this ``False`` and commit explicitly. Transaction
            boundaries are a caller's decision -- a DAG task commits per file
            so progress survives a failure, a local run may prefer one
            transaction for the whole batch.

    Raises:
        StoreError: The driver is not installed, or the server refused the
            connection.
    """
    connection = open_connection(dsn, autocommit=autocommit)
    try:
        yield connection
    finally:
        connection.close()
