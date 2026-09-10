"""Process exit codes.

These are the contract between a worker container and its orchestrator. Airflow
fails a ``KubernetesPodOperator`` task on any non-zero exit, but distinct codes
let an operator tell "the credential is wrong" apart from "the gateway was
down" without reading logs -- and let a DAG decide what is worth retrying.

Treat these as a versioned public interface: add codes, never renumber them.
"""

from __future__ import annotations

#: Completed successfully.
SUCCESS = 0

#: An unexpected error. Anything not covered by a more specific code.
UNEXPECTED = 1

#: Bad command-line usage. Reserved by argparse; never returned deliberately.
USAGE = 2

#: Configuration is missing or invalid, e.g. no LLM_BASE_URL. Not retryable.
CONFIGURATION = 3

#: Input could not be read, or an argument value was rejected. Not retryable.
INVALID_INPUT = 4

#: The gateway rejected the credential. Not retryable -- fix the secret.
AUTHENTICATION = 5

#: Rate limited after the library exhausted its own retries. Retryable later.
RATE_LIMITED = 6

#: The gateway failed or was unreachable after retries. Retryable.
UPSTREAM_UNAVAILABLE = 7

#: The request reached the model but the result was unusable, e.g. unparseable
#: output or silent audio. Retrying rarely helps.
PROCESSING_FAILED = 8

#: Interrupted by SIGINT or SIGTERM. Conventional 128 + signal number.
INTERRUPTED = 130
