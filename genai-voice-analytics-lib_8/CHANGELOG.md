# Changelog

## 0.13.2

### Fixed
- **`analyse` and `extract` defaulted to the transcription model.** Without an
  explicit `--model` they fell back to `MODEL_NAME`, which is
  `gemini-2.5-flash` -- so a local run scored calls with the transcription
  model instead of Qwen. Different scores, no warning. They now default to a
  new `ANALYSIS_MODEL_NAME` (`qwen3-30b-a3b-instruct`), and `transcribe` still
  uses `MODEL_NAME`.

  The originating service had this setting too, as `ANALYTICS_MODEL_NAME`, and
  ignored it: `call_llm` hardcoded the model in the request and read neither
  the setting nor its own `model_name` argument. A test now asserts no model
  name is hardcoded into a request anywhere in this library.

## 0.13.1

### Added
- `test_prompts.py` now reproduces the originating `_resolve_prompt_entry`
  verbatim and runs both implementations over nine version-resolution cases --
  highest-approved-wins, a PENDING version above the approved one, only
  PENDING, only REJECTED, a missing `version` field, duplicates. They agree on
  every one. Which version of a prompt is used decides what the model is asked,
  and getting it wrong changes scores with nothing in the data to show why.

## 0.13.0

### Added
- `build-prompt --from-prompts-response <file>` assembles from a saved reply
  from PromptHub's prompts endpoint, for a machine that cannot reach the
  registry directly. Unlike the committed snapshot this carries the real
  approved versions, and the approval rules are applied to it exactly as to a
  live response: a version in review never displaces the approved wording, and
  a KPI with only a PENDING version is skipped and reported.
- Accepts the whole response body, `data` alone, or a bare list -- the obvious
  ways a curl gets saved.

### Notes
- The two PromptHub endpoints are different and return different things:
  `/client/usecase/{id}` with `INTERNAL-API-KEY` gives models and keys;
  `/extenal/get-all-prompts` with `lite-llm-api-key` gives the prompts. A
  saved `usecase.json` cannot build a scoring prompt.

## 0.12.6

### Added
- `run_local.py --transcribe-only` -- stops after transcription. No scoring, no
  extraction, no `--prompt`, and with `--postgres` it writes `transcriptions`
  rows while leaving `calls` and `call_analyses` alone. The same half of the
  pipeline the transcribe-only DAG runs, so a batch with no KPI configuration
  at all is valid.

### Fixed
- The per-file summary said "no results" for a transcription that had plainly
  succeeded, because it only looked for scores. It now reports the transcript's
  length and language: `c1.wav: 155 chars Hindi; translated 139`.

## 0.12.5

### Added
- `fetch_usecase_key.py --from-response <file>` reads PromptHub's reply from a
  file instead of making the request, for a machine that can reach the registry
  through a proxy or a jump host but not directly. Run your own curl, save the
  body, feed it in.
- The request path now sets `trust_env=True`, so `HTTPS_PROXY` is honoured --
  often the whole difference between a curl that works and a script that does
  not -- and follows redirects.
- A connection failure now prints the exact curl to run and the command to feed
  its output back in, rather than only the error.

## 0.12.4

### Added
- `examples/airflow/voice_analytics_transcribe.py` -- a transcription-only DAG,
  for proving that half on a cluster before the rest is wired up. It needs
  **one** `transcription_batches` row and none of the KPI configuration,
  because it scores nothing.
- `examples/airflow/TRANSCRIBE_SETUP.md` -- every Variable, connection,
  Kubernetes object and database row it needs, extracted from the DAG rather
  than written from memory.
- `validate_access` there falls back to a static `LLM_API_KEY` when
  `PROMPTHUB_BASE_URL` is unset, and says loudly that it skipped the
  entitlement check. For bringing the DAG up, not for running batches.
- The schema-conformance test now covers every DAG in `examples/airflow/`,
  including the rule that none of them may import the compute library.

## 0.12.3

### Fixed
- `read_batch_config` now refuses a batch whose `usecase_id` is NULL or empty.
  The column is nullable in the schema but the value is not optional in the
  pipeline: it is what PromptHub resolves prompts and model entitlement
  against, so a missing one surfaced as a 404 for use case "None" several tasks
  later -- after the batch had already looked healthy.

### Notes
- `scripts/fetch_usecase_key.py` now says plainly that it is a laptop
  stand-in. Under Airflow no one supplies a use case id: the DAG reads it off
  the batch row and does the lookup itself, per run.

## 0.12.2

### Fixed
- **`docs/schema/live-schema.sql` had two malformed column lines.** The PDF the
  schema was extracted from repeated its header for `usecase_kpi_selections`
  and `usecase_kpi_configs`, and the extractor pasted that header onto each
  table's `id` column. The file parsed without error and was wrong, which is
  the worst kind of wrong. Both tables' `id` columns are restored.

### Added
- `tests/unit/test_bootstrap_matches_live_schema.py` -- checks `bootstrap.sql`
  against the exported schema column by column: same names, same order, same
  types, same nullability, across all nine tables. Reading two DDL files side
  by side is exactly the comparison a person does badly; this is what caught
  the malformed fixture.

## 0.12.1

### Fixed
- **The default use case prompt fallback was never wired up.** The originating
  service consulted a second, default use case whenever the active one had no
  prompt for a KPI, and for the base prompt too -- a use case may define only
  KPI prompts. `resolve_kpi_prompts` accepted a `fallback_prompts` argument but
  nothing passed one, and `resolve_base_prompt` had no such argument. Both now
  take it, and `build-prompt` supplies it from the new optional
  `PROMPTHUB_DEFAULT_LITELLM_KEY`.
- `build-prompt` now tolerates a failed read of the active use case's prompts
  by falling back to the default, as the original did, instead of failing the
  batch.

## 0.12.0

### Added
- `build-prompt --from-file` assembles a scoring prompt from a local JSON
  snapshot instead of PromptHub, for a machine that cannot reach the registry.
  The same `build_consolidated_prompt` runs either way, so only the source of
  the text differs. Omitting `--kpi-codes` takes every KPI in the file.
- `examples/prompts/analytics_prompts.json` -- the originating service's
  committed prompt set: one base prompt plus 20 KPIs across 4 sections. Under
  `examples/`, deliberately **not** inside the wheel: KPI prompts are
  configuration authored and versioned in PromptHub, and a baked-in copy would
  be a second source of truth that looks authoritative and goes stale in
  silence.

### Notes
- That snapshot contradicts itself on the scoring scale -- the instructions say
  0-10, the scale below defines 0-3. Worth resolving in PromptHub.
- Its `OUTPUT FORMAT` asks only for `kpis`, while the code parses
  `risk_summary`, `call_impact` and `customer_experience_drivers` too, so the
  live version must ask for more than the snapshot does.
- It asks for `confidence`, which the originating ORM does not map -- the model
  returns it and the value is dropped before the database.

## 0.11.0

### Added
- `scripts/fetch_usecase_key.py` -- looks up a use case's per-model gateway key
  from PromptHub, which is what the originating DAG's `validate_access` does.
  There was no shared gateway key in that system: each use case has its own key
  per model, and a model is usable only when the use case is APPROVED, not
  disabled, and its entry has an `llmApiKey` with no `removeTokenModelAccess`.
  Keys are masked unless asked for; `--write-env` puts one into `.env` without
  printing it, and chmods the file to 600.

## 0.10.4

### Fixed
- `scripts/fake_gateway.py` only recognised a scoring prompt written exactly as
  `KPI CODE :`. A hand-written `KPI CODE:rpc_verified` was treated as a
  transcription request, so `analyse` got a transcript back and failed with
  "the model returned no KPI scores" -- an error pointing at the model rather
  than at a missing space. Now matches any spacing and either separator.

### Added
- `LOCAL_SETUP.md` gains a troubleshooting entry for that failure, and
  `--batch-id` reuse is called out: a batch that has already run is not
  reusable.

## 0.10.3

### Fixed
- A `.env` file now configures the **database** as well as the gateway.
  `dsn_from_env()` and `run_local.py`'s precheck read only real environment
  variables, so a `.env` that worked for `transcribe` was rejected by the
  runner and ignored by `--postgres`. Half-working configuration is worse than
  none: the gateway connected, the run started, and the failure arrived at the
  persistence step minutes later. Real environment variables still take
  precedence.

## 0.10.2

### Added
- `bootstrap.sql` checks the server version and refuses below Postgres 13,
  naming the reason, rather than failing partway through with an error that
  does not.
- `LOCAL_SETUP.md` covers using an existing Postgres container.

### Notes
- **Minimum Postgres is 13**; verified against 16. The schema uses
  `gen_random_uuid()` (core from 13), `FILTER (WHERE ...)` and `jsonb` (both
  9.4), and nothing newer.

## 0.10.1

### Added
- `scripts/seed_local_batch.py` -- creates a batch and its KPI configuration in
  a local database, so a local run has something to read. Defaults to the KPI
  codes the bundled fake gateway scores. Refuses to run against a connection
  string that does not look local, because `--reset` empties all nine tables.
- `LOCAL_SETUP.md` -- running this on a Mac with PyCharm and DBeaver, no
  Airflow and no VPN.

## 0.10.0

### Added
- **`voice_analytics_store`** -- the persistence half, as a separate package
  (`store` extra). Holds every INSERT and UPDATE the pipeline makes. The DAG
  and `scripts/run_local.py` both use it, so they cannot write different rows.
  The library itself still opens no connection; see ADR 0007.
- `scripts/run_local.py --postgres --batch-id N` -- a local run that writes
  real rows.
- `docs/schema/bootstrap.sql` -- creates the nine tables for a throwaway local
  database.
- `tests/integration/` -- 27 tests against a **real** Postgres, covering what a
  mocked connection cannot: that the SQL parses, the columns exist, the
  constraints hold, and a full run leaves rows that add up. Skipped when
  `VOICE_ANALYTICS_TEST_DSN` is unset.

### Changed
- The DAG no longer carries SQL; it calls the store. 1247 lines to 976.
- `Dockerfile` takes `--build-arg EXTRAS="--extra transfer"`, and its build-time
  check now asserts every subcommand is present.

### Fixed
- `store.connect(...).__enter__()` closed the connection when the generator was
  collected. `open_connection()` added for callers holding one across a run.
- `bootstrap.sql` no longer requires the `pgcrypto` extension:
  `gen_random_uuid()` is core Postgres from 13 onwards.

## 0.9.0

### Added
- **`transfer` command** -- SFTP to GCS, the pipeline's first stage. Streams in
  8 MiB chunks and computes each file's MD5 in the same pass, so a large
  recording never lands in memory and the checksum is free. Host keys are
  verified with no opt-out (`RejectPolicy`, required `SFTP_KNOWN_HOSTS`,
  `allow_agent=False`). `paramiko` and `google-cloud-storage` are an **optional
  extra**: `pip install 'genai-voice-analytics-lib[transfer]'`.
- **`extract` command** and `voice_analytics.extraction` -- topics and agent
  name, ported from `batch_service.py` with prompts verbatim. Failure leaves
  the field empty and still exits 0, matching the originating service;
  `--strict` opts out.
- **`build-prompt` command** -- assembles the KPI scoring prompt once per
  batch. Previously the one step an orchestrator could not do through the
  container, which forced this library onto the scheduler's machine.
- `analyse` and `extract` accept the JSON `transcribe` writes, not just plain
  text, so one command's output feeds the next with nothing in between.
- `duration_sec` in the `analyse` output.
- `docs/schema/` -- the live schema and what every DAG task reads and writes.
- `docs/parity.md` -- old DAG vs new, endpoint by endpoint.
- `docs/proposals/flows-table.md` -- a home for recurring schedules.
- `examples/airflow/voice_analytics_pipeline.py` -- the reference DAG.
- `scripts/run_local.py` -- runs the whole pipeline over a folder of recordings
  with no Airflow, Kubernetes, GCS or database. A subprocess runner, so it
  exercises the same CLI contract and exit codes the pods do.

### Fixed
- `mask_secret` on values under 8 characters.
- Cost-attribution tags reverted to the originating service's, so existing
  LiteLLM dashboards keep working.
- `--romanize` had no effect: `romanized_text` was never read.
- `call_impact` and `customer_experience_drivers` were parsed but dropped.

### Notes
- The library still writes nothing to a database (ADR 0001).
- Core dependencies remain **seven**. The transfer extra adds two, and only
  for images that need it.
