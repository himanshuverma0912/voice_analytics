# 7. Persistence is a separate package, not part of the library

Date: 2026-09-15

## Status

Accepted.

## Context

ADR 0001 says the library writes nothing to a database. That has held: every
module under `voice_analytics` takes bytes or text and returns JSON, and no pod
needs a database credential.

The SQL still has to live somewhere. It first lived inside the Airflow DAG, as
task bodies. That was fine while Airflow was the only caller, and stopped being
fine the moment a second one appeared: `scripts/run_local.py` runs the same
stages on a laptop, and could not write the same rows without copying the
statements.

Copied SQL drifts. The two copies would agree on the day they were written and
disagree by the time anyone noticed, in the direction that matters least
visibly -- a column added on one side, a counter recalculated on one side.

## Decision

The SQL lives in **`voice_analytics_store`**, a separate top-level package in
this repository, installed with the `store` extra.

* `voice_analytics` -- computation. No database driver, no connection, no
  credential. Ships as a container image. **Nothing imports it except a pod.**
* `voice_analytics_store` -- persistence. Depends on `psycopg` and nothing
  else. Imported by the DAG and by the local runner.

Every function takes an open connection and does not commit. Transaction
boundaries belong to the caller: the DAG commits per file so progress survives
a failure, while a local run may prefer one transaction per batch.

## Consequences

**The DAG now imports something.** Its rule was "imports Airflow, Kubernetes
and the standard library, and never `voice_analytics`". That rule stands for
the *compute*: upgrading the library is still one line, `IMAGE`, with no
scheduler dependency to align. But the scheduler now needs
`genai-voice-analytics-lib[store]` installed, and a schema change means
upgrading it there.

That is a real cost, accepted because the alternative is two copies of the SQL.
It is also a smaller cost than it looks: the store depends only on `psycopg`,
which an Airflow deployment talking to Postgres already has, and it changes
only when the schema does.

**The SQL is testable.** `tests/integration/` runs it against a real Postgres
-- twenty-seven tests covering what a mocked connection cannot: that the
statements parse, the columns exist, the constraints hold, and a full run
leaves rows that add up. `pgserver` provides a server on a machine that has
none, and the whole directory skips when `VOICE_ANALYTICS_TEST_DSN` is unset.

**A local run is a rehearsal, not a demonstration.** `run_local.py --postgres`
writes the same rows through the same code as production. A behaviour that
works locally works in the DAG, because it is the same function.

**The library's guarantee is unchanged.** `voice_analytics` still opens no
connection. A reader checking ADR 0001 will find it still true, and will find
the persistence next door rather than absent.

## Alternatives considered

**Leave the SQL in the DAG.** Rejected: the local runner could not write rows,
so end-to-end testing needed a cluster.

**Put persistence inside `voice_analytics`.** Rejected: it would give every
pod a database driver it must not use, and make ADR 0001 a claim about
intent rather than about what the package can do.

**Have a pod do the writing.** Rejected: 12,480 pods each opening a connection
would exhaust the pool before the batch finished, and every pod would need a
credential.
