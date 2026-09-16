# ADR 0006 — What was not ported from the originating service

**Status:** Accepted
**Date:** 2026-09-11

## Context

This library is extracted from `genai-speech-analytics`. Anything left behind
is a decision, and an undocumented one becomes a surprise later. This record
lists every capability of the originating service that does **not** appear
here, and why.

## Audit findings

A line-by-line sweep of the originating service against this library found six
discrepancies. All are now resolved.

| Found | Effect had it shipped | Resolution |
|---|---|---|
| `call_impact` not parsed | High/Low judgement discarded; two database columns never populated | Ported, with the High/Low constraint enforced |
| `customer_experience_drivers` not parsed | Silently dropped | Ported |
| `extract_duration_sec` missing | Caller could not populate `calls.duration_sec` | Ported |
| Cost-attribution tag renamed `service` -> `library` | **LiteLLM dashboards group by this tag; cost history would have been orphaned** | Reverted to `service` / `voice-transcription-service` |
| `romanized_text` never read | `--romanize` would return the original script when the model used a separate field | Ported as a preference in the formatter |
| `_format_segments_multi_key` not ported | None -- dead code in the originating repo, called from nowhere | Correctly omitted |

Two further differences are intentional and not defects:

- **`normalize_optional_text`** converts the empty strings a multipart form
  sends into `None`. Argparse supplies `None` directly, so there is nothing to
  normalise.
- **A top-level `translated_transcript`** is read by `orchestrator.py` but not
  by the live router, which builds it from segments. This library matches the
  router -- the orchestrator is the unfinished per-pod path that cannot run.

## Ported in full

| Originating code | Here |
|---|---|
| `TranscriptionService.transcribe` / `.translate` | `transcription/service.py` |
| `SummarizationService.get_multi_format_summary` | `summarize_text` |
| `SummarizationService.summarize_audio_direct` | `summarize_audio` |
| `select_summary_formats` | `summarization/formats.py` |
| `BaseLLMService.process_audio_request` / `process_text_request` | `llm/base.py` |
| `parse_llm_json_safely`, `normalize_llm_output`, `parse_agent_output` | `llm/json_repair.py` |
| `PromptManager` | `prompts/manager.py` |
| `resolve_base_prompt`, `resolve_kpi_prompts` | `prompthub/client.py` |
| `build_consolidated_prompt` + the Jinja template | `analysis/prompt.py` |
| `_anonymize_transcript`, `_extract_anonymized_content` | `anonymization/` |
| `call_llm` (KPI scoring) | `analysis/service.py` |
| `_format_segments`, the speaker-label regex | `transcription/formatting.py` |
| `INDIAN_LANGUAGES` | `transcription/languages.py` |
| `extract_duration_sec` | `transcription/duration.py` |
| `risk_summary`, `call_impact`, `customer_experience_drivers` | `analysis/models.py` |
| `LLM_SERVICE_NAME`, `API_LLM_METADATA`, `BATCH_LLM_METADATA` | `llm/base.py`, tag names unchanged |
| `normalize_llm_output`, `reject_error_payload` | `llm/json_repair.py` |
| `_infer_mime_type` | `cli/_common.py` |

## Deliberately excluded

### Database writes

Every `session.add`, `commit` and `update_*` call. Per ADR 0001 the caller owns
persistence. Note the originating service already kept these out of its service
layer -- `TranscriptionService` has no database import either. The writes lived
in `_transcribe_single_row` and `save_analysis_results`, both callers.

**The caller must now do:** claim the row as `processing`, save the transcript
or scores, mark failures, and recalculate batch counters.

### HTML escaping

The originating code escaped in three places, producing `&amp;amp;` in real
output. A library returns data; the boundary that renders it decides encoding.

### `overall_score`

Removed. The interface groups KPIs by objective and shows no single number, and
the originating implementation averaged unweighted despite weight columns
existing. `by_objective` carries per-objective averages; a caller that wants a
headline number computes it and picks its own weighting.

### The `audio/*` content-type check

`routers/transcription.py` rejected non-audio uploads with HTTP 415 by reading
the browser's `Content-Type` header. That header only exists in an HTTP upload;
a pod reading a file has none. The library instead validates that the *model*
can accept audio. **An HTTP adapter built on this library should re-add the 415
check.**

### Authentication and the model allow-list

`validate_request_params`, PromptHub API-key validation, and the per-key model
allow-list. These are policy enforcement points that belong to whatever fronts
the library.

**Correction (2026-09-11).** An earlier revision of this ADR recorded model
entitlement as an unowned gap. It is not unowned: the production DAG's
`validate_access` task enforces it, by fetching the usecase from PromptHub and
refusing to proceed unless the usecase is `APPROVED`, not `disable`d, and
carries a model entry with an `llmApiKey` and no `removeTokenModelAccess`
flag. The FastAPI service never checked this; the DAG always did.

The consequence is that **the check must survive the move to pods**. It is
reproduced in `examples/airflow/voice_analytics_pipeline.py`, which also
tightens it: the old DAG validated the analysis model *after* transcribing the
whole batch, so an entitlement failure could surface only once the money had
been spent. Both models are now checked before any audio is read.

### Job credential encryption

`job_credential_service` (Fernet). Belongs to the jobs workflow, which is not
part of the voice analytics flow.

### Batch and file handling

`create_batch_from_zip`, `parse_batch_zip`, `decode_transcript_content`,
`_write_temp`. Unpacking archives, detecting encodings and writing temporary
files are orchestration, not analysis.

### Orchestration wrappers

`run_single_analysis`, `run_batch_item_analysis`. These interleave database
writes with the work; the work is here, the writes are the caller's.

Note `run_single_analysis` and `POST /analyses/batch` both passed a **`None`**
system prompt, so they could not have produced usable scores. Not ported, and
not a loss.

## Not ported — open, and worth a decision

### `extract_topics` and `extract_agent_name` -- PORTED in 0.8.0

Two extra LLM calls the originating worker makes per call, storing
`call_analyses.topics` and `calls.metadata.agent_name`.

**Originally deferred** on the grounds that the wireframe never mentions
topics or agent names. That was the wrong test. Both columns are populated in
production and `calls.metadata.agent_name` is what the Agent Communication
Report aggregates on (`routers/pipeline.py:920`), so omitting them would have
emptied an existing report rather than declined a new feature.

Now in `voice_analytics.extraction`, with the `keyword_extraction` prompt
ported byte-for-byte and both keyword descriptions copied verbatim -- they read
like documentation but they *are* the prompt, and rewording them changes the
output.

Three behaviours were preserved deliberately:

* **Failure is not fatal.** The originating worker wrapped each extraction in
  its own `try/except` and left the field empty on failure. `extract` does the
  same and still exits 0; `--strict` opts out.
* **No retries.** The original did not retry either, and both callers treat a
  failure as "leave the field empty" -- so a retry loop would spend money
  populating an optional field.
* **Both response shapes.** Models return topics either as one match holding a
  list, or as one match per phrase ranked by count. Both are handled, because
  the original had to handle both.

`analyse(agent_name=...)` now has a producer: run `extract` first and pass
what it returns.

### Extraction and matching

`ValidationService.extract_keywords` and `cross_check_with_llm`, which map onto
the second flow's Extract and Match nodes. Out of scope while the focus is the
voice analytics flow.

Note the interface specifies matching as a **database join with tolerance
rules** returning six outcomes, which is a different capability from the
originating LLM-as-judge similarity check. That is new work, not a port.

## Consequences

- A reader can tell in one place what this library does not do.
- The auth gap is recorded rather than assumed, which is what a security review
  will ask about.
- Two items -- topics/agent name, and the 415 check -- are cheap to add if a
  consumer turns out to need them.


## Added beyond the originating service

### SFTP to GCS transfer (0.9.0)

Not a port: the originating service had no SFTP connector. Files arrived by
HTTP upload, were written to a temporary directory, and `file_path` pointed at
local disk -- which is why `_transcribe_single_row` opens `row.file_path` with
a plain `open()`.

Added because the task breakdown lists "sftp connector: to pull data from sftp
and put it on a gcs bucket" as the pipeline's first stage. It is in the library
rather than in an Airflow operator so it runs the same way from a pod, a
laptop, or a different orchestrator -- and so the checksum it computes while
streaming is available to whatever is tracking which files have been seen.

`paramiko` and `google-cloud-storage` are an **optional extra**, not core
dependencies. A pod that only transcribes or scores does not carry an SSH
stack or a cloud SDK, so the core stays at seven direct dependencies.

Two decisions worth knowing about:

* **Host keys are verified, with no way to opt out.** `paramiko.RejectPolicy`,
  a required `SFTP_KNOWN_HOSTS`, and `allow_agent=False` / `look_for_keys=False`
  so only the configured credential is ever offered. An SFTP session carries
  both the credential and the recordings; `AutoAddPolicy` would trust whatever
  answered on the first connection, which is exactly when a substitution would
  go unnoticed.
* **Serial, not parallel.** The bottleneck is the SFTP server, usually a shared
  corporate box that throttles or drops connections under a fan-out. A transfer
  that finishes slowly beats one that trips a rate limit halfway.


### PII anonymization -- ported unchanged, and proven

The task breakdown says "PII anonymization based on the defined PII
identifiers", which reads as though the caller supplies an identifier list.
The originating service supplies none: the request body is
`{"messages": [{"role": "user", "content": transcript}]}` and nothing else.
The identifiers live inside the auxiliary service.

Confirmed with the team, and ported as-is. No identifier parameter was
invented -- guessing an API shape for a compliance control would produce
something that looks configurable and silently is not.

The response search is the part worth care: the service's response has no
stable shape, so the original searches for the anonymized user message across
three key spellings and then recurses. `test_anonymization_parity.py`
reproduces the original function verbatim and runs both over twenty-three
response shapes -- including key precedence when two spellings are present, a
blank first user message, and non-dict entries in the array -- asserting they
agree on every one, and that both refuse the same unusable responses rather
than falling back to the original text.


### Prompt resolution -- ported, with the fallback restored in 0.12.1

Three modules in the originating service did this work:

| Originating | Here |
| --- | --- |
| `prompt_service.PromptManager` | `voice_analytics.prompts.manager` |
| `prompt_resolver` | `voice_analytics.prompthub.client` + `analysis.prompt` |
| `prompthub_client` | **caller-side**: the DAG's `validate_access`, `scripts/fetch_usecase_key.py` |

`prompthub_client` is deliberately not in the library. Its three functions are
about *who may use which model*, which is policy enforcement, not analysis --
see the correction above about `validate_access`.

Two differences worth recording:

**`PromptManager` reads from the wheel.** The original did
`open("domains-prompt.yaml")`, a relative path, so it worked only when the
process started in the repository root. `importlib.resources` makes it work
from any directory, in any container.

**`_resolve_prompt_entry`'s PENDING branch was dead code.** It checked whether
any version was `PENDING` and then returned `latest_approved` either way --
both branches had the same result. `latest_approved` here does that directly:
a version in review never displaces the approved wording.

**The default use case fallback was missing, and is now restored.** The
original consulted a second, default use case whenever the active one had no
prompt for a KPI -- and for the base prompt too, since a use case may define
only KPI prompts. `resolve_kpi_prompts` accepted a `fallback_prompts` argument
from the start but nothing ever passed one, and `resolve_base_prompt` had no
such argument at all. Both now take it, and `build-prompt` supplies it from
`PROMPTHUB_DEFAULT_LITELLM_KEY`.

It is **optional** here, where the original hardcoded the key in source. Unset,
a KPI with no prompt of its own is skipped and reported -- the same outcome as
a default use case that does not define it either.

The original's resilience is kept too: a failure to read the active use case's
prompts falls back to the default rather than failing the batch, and a failure
to read the default is a warning rather than an error, because a KPI missing
from both is skipped either way.
