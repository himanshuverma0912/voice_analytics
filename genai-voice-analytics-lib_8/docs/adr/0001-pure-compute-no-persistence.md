# ADR 0001 — The library is pure compute; callers own persistence

**Status:** Accepted
**Date:** 2026-09-10

## Context

This library is extracted from the `genai-speech-analytics` service so the same
logic can run in a web API, an Airflow worker pod, a batch job or a notebook.

The service it came from mixes concerns: `analytics_repository.py` is 1,915
lines of SQLAlchemy tightly bound to a request-scoped session, and
`database.py` creates two engines at import time. Carrying that into a library
would mean every consumer inherits a database dependency whether or not it has
a database.

A new PostgreSQL database is being provisioned for the new architecture,
separate from the existing `speech_analytics_db` and `ai_analytics_db`.

## Decision

**The library performs computation only. It never opens a database connection.**

Functions take their inputs as arguments and return results as values:

```python
result = await transcribe(client=..., content=audio, ...)      # -> TranscriptionResult
result = await analyse(transcript=..., kpi_prompts=[...], ...)  # -> AnalysisResult
```

The caller is responsible for reading inputs (KPI configuration, transcripts)
and writing outputs (scores, evidence, status) to its own database.

There is no `persistence` package and no `[postgres]` optional dependency.

## Consequences

**Positive**

- No SQLAlchemy, asyncpg, greenlet or Alembic in the dependency tree. The
  worker image stays small and its vulnerability surface narrow.
- Every function is testable with no infrastructure. The suite runs in under a
  second with no database, no network and no `.env` file.
- Schema ownership stays with one team. Two repositories able to migrate the
  same schema is a known source of production incidents.
- The same functions serve a web API, a pod and a notebook without change,
  because none of them assume a transaction is in progress.

**Negative**

- Callers write more code. Reading a KPI configuration and persisting results
  is work the service used to do inside `batch_service.py`.
- Multi-step orchestration moves outward. Where the service passed state
  between steps through the database, the caller must now do that.
- Two callers could persist results inconsistently. Publishing the result
  schemas (`voice_analytics.schemas`) mitigates this: they define the shape
  even though the library does not store it.

**Neutral**

- If a future consumer genuinely needs shared persistence, it belongs in a
  separate package that depends on this one, never inside it.

## Alternatives considered

**The library owns persistence.** Rejected: it forces a database dependency on
consumers that have none, makes the test suite require PostgreSQL, and creates
the two-repository migration conflict described above.

**An optional `[postgres]` extra.** Rejected as premature. It splits the API
into "with database" and "without database" variants, doubling the paths to
test and document, for a need no confirmed consumer has today.
