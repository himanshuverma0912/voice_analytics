# What is in this archive

`genai-voice-analytics-lib` 0.9.0 -- the library, its tests, and the reference
Airflow DAG that drives it.

    src/voice_analytics/     the library
    examples/airflow/        the reference DAG
    docs/                    ADRs, schema, parity, proposals
    tests/unit/              383 tests
    scripts/                 local stand-ins for the gateway and anonymizer
    Dockerfile               the worker image

## Run the tests

    uv sync
    uv run pytest -q          # 384 passed, 27 skipped

The 27 skipped ones need a Postgres. Point them at a throwaway database and
they run:

    createdb voice_analytics_test
    export VOICE_ANALYTICS_TEST_DSN=postgresql://localhost/voice_analytics_test
    uv run pytest -q          # 411 passed

They are not mocked. They check what a mocked connection cannot: that the SQL
parses, the columns exist, the constraints hold, and a full run leaves rows
that add up.

## Try it without a VPN or credentials

Two standard-library stubs answer the gateway and the anonymization service, so
the whole path -- CLI parsing, settings, retries, JSON repair, segment
formatting, file output, exit codes -- runs on a laptop.

    uv run python scripts/fake_gateway.py --port 8099 &
    uv run python scripts/fake_anonymizer.py --port 8098 &

    export LLM_BASE_URL=http://127.0.0.1:8099/v1 \
           LLM_API_KEY=sk-fake \
           MODEL_NAME=gemini-2.5-flash \
           ANONYMIZATION_URL=http://127.0.0.1:8098/anonymize \
           SSL_VERIFY=False

    uv run python -m voice_analytics transcribe \
        --input call.wav --output call.json --anonymize --target-lang English

    uv run python -m voice_analytics extract --input call.json

Watch the log line `Translating 155 characters` after
`Personal information removed (156 in, 155 out)` -- that is the translator
receiving the anonymised text, which is the ordering the pipeline exists to
guarantee.

## Run the whole pipeline locally, no Airflow

    python -m voice_analytics build-prompt --kpi-codes a,b --output kpis.txt
    python scripts/run_local.py --input-dir ./calls --output-dir ./out \
        --prompt kpis.txt --target-lang English

Runs every stage over a folder of recordings the way the DAG runs them over
pods -- same ordering, same concurrency limit, same halt-above-N%-failed rule,
same exit codes. Results land as JSON under `out/`, with `out/summary.json`
holding each file's headline numbers. No database, no cloud, no cluster.

## Build the image

    docker build -t voice-analytics:0.9.0 .                        # 7 deps
    docker build --build-arg EXTRAS="--extra transfer" \
                 -t voice-analytics-transfer:0.9.0 .               # + SFTP/GCS

Only the pod that runs `transfer` needs the second. The build fails if a
prompt or a subcommand did not land in the image.

**Neither image has been built here** -- no Docker daemon was available. This
is the first thing to verify.

## Deploy the DAG

`examples/airflow/voice_analytics_pipeline.py` drops into your DAG folder. It
imports Airflow, Kubernetes and the standard library, and **never
`voice_analytics`** -- the library reaches it only as a container image, so
upgrading is a one-line change to `IMAGE`.

Set these Airflow Variables:

    LLM_BASE_URL  ANONYMIZATION_URL  AUDIO_BUCKET
    PROMPTHUB_BASE_URL  PROMPTHUB_INTERNAL_API_KEY
    SFTP_HOST  SFTP_USERNAME  CORP_CA_BUNDLE  HALT_ABOVE_PCT

and these Kubernetes objects:

    configmap/corp-ca-bundle   key: corp-ca.pem
    secret/sftp-credentials    keys: password or id_ed25519, known_hosts

## Not yet verified

Honestly, because it cannot be done from a laptop:

- the Docker build, and the image against the real gateway
- the pod specs, the GCS FUSE mount, and the SFTP connection
- `DEFAULT` clauses and constraint names -- the schema export omitted them, so
  `ON CONFLICT ON CONSTRAINT uq_analysis_kpi` and the `id` defaults are assumed

Everything else is covered by the test suite, including the DAG's SQL, which is
checked column by column against the exported schema.

## Still open

- **Token usage** is not captured, by this library or the originating service,
  so cost per call cannot be computed.
- **Anonymisation status** has no column, so the "PII removed" claim cannot be
  evidenced from the database.
- **Recurring schedules** have no home -- see `docs/proposals/flows-table.md`.
- Tasks 5 and 6 (entity extraction from config, entity matching) are Flow B and
  out of scope for now.
