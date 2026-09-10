# ADR 0003 — Exit codes are the orchestration contract

**Status:** Accepted
**Date:** 2026-09-10

## Context

The target deployment runs this image as an Airflow `KubernetesPodOperator`
task: the pod executes one command and exits, and Airflow acts on the result.

Airflow fails a task on any non-zero exit. Beyond that, an operator diagnosing
a red task should not have to read pod logs to learn whether a secret is wrong
or the gateway was briefly down — and a DAG author should be able to decide
what is worth retrying.

## Decision

Each failure class maps to a distinct, documented exit code.

| Code | Meaning | Retryable |
|---|---|---|
| 0 | Success | — |
| 1 | Unexpected error | Maybe |
| 2 | Bad command-line usage (argparse) | No |
| 3 | Configuration missing or invalid | No |
| 4 | Input unreadable, or an argument rejected | No |
| 5 | Gateway rejected the credential | No |
| 6 | Rate limited after internal retries | Yes, later |
| 7 | Gateway unreachable or failing | Yes |
| 8 | Result unusable (unparseable, or silent audio) | Rarely helps |
| 130 | Interrupted (SIGINT/SIGTERM) | — |

These are **a public interface**. Codes may be added; existing codes are never
renumbered. A test asserts they remain distinct.

Two supporting conventions:

- **Results to stdout, diagnostics to stderr**, so stdout can be piped.
- **An empty transcript exits 8, not 0.** Silent audio or a wrong file should
  fail the orchestrating task rather than quietly reporting success. The result
  is still written, so the failure can be diagnosed.

## Consequences

**Positive**

- A DAG can branch on the cause without parsing logs — retry a 7, alert on a 5.
- Failures are attributable at a glance in the Airflow UI.
- The library's exception hierarchy is mapped in exactly one place
  (`cli/main.py`), so adding a command does not mean re-deriving the mapping.

**Negative**

- Renumbering would silently break every consuming DAG, so the table is
  effectively frozen. The test guarding distinctness is a partial safeguard;
  it cannot catch a deliberate renumber.
- Code 1 remains a catch-all. Anything landing there is a gap in the mapping
  and should be classified.

## Alternatives considered

**Exit 0 or 1 only, with detail in the logs.** Rejected: it forces log parsing
into the orchestration layer and makes retry policy impossible to express
declaratively.

**A machine-readable status file.** Rejected as insufficient on its own — a pod
that crashes may never write one, whereas an exit code always exists.
