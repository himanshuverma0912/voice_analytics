# genai-voice-analytics-lib

Reusable voice analytics building blocks, packaged as an importable Python
library. Extracted from the `genai-speech-analytics` service so the same logic
can run inside a web API, an Airflow worker pod, a batch job or a notebook.

**Version 0.6.0 completes the voice analytics flow**: transcription,
anonymization and KPI scoring, plus summarization. Extraction and matching --
the second flow's tail -- follow in a later release.

---

## Install

```bash
uv add genai-voice-analytics-lib
```

For local development against a checkout:

```bash
uv sync --all-groups
```

---

## Quick start

```python
import asyncio

from voice_analytics import build_llm_client, load_settings, transcribe


async def main() -> None:
    settings = load_settings()                      # reads .env / environment
    client = build_llm_client(settings, api_key="sk-your-key")

    with open("call.wav", "rb") as handle:
        audio = handle.read()

    result = await transcribe(
        client=client,
        content=audio,
        mime_type="audio/wav",
        model_name="gemini-2.5-flash-transcription",
        target_lang="Hindi",     # optional; omit to skip translation
    )

    print(result.transcript)
    print(result.translated_transcript)
    print(f"{result.processing_time_ms} ms")


asyncio.run(main())
```

---

## Configuration

Nothing in this package reads configuration at import time. Build a `Settings`
object explicitly and pass it in.

```python
from voice_analytics import Settings, load_settings

settings = load_settings()                    # from environment and .env
settings = load_settings(env_file=None)       # environment only
settings = Settings(                          # explicit, e.g. in tests
    LLM_BASE_URL="https://llm.internal/v1",
    LLM_API_KEY="sk-...",
)
```

| Setting | Required | Default | Purpose |
|---|---|---|---|
| `LLM_BASE_URL` | **yes** | — | OpenAI-compatible gateway URL |
| `LLM_API_KEY` | no | `""` | Fallback key; override per call with `api_key=` |
| `MODEL_NAME` | no | `gemini-2.5-flash` | Model used when the caller names none |
| `TRANSCRIPTION_MODEL_NAMES` | no | two Gemini models | Allow-list of audio-capable models |
| `PROMPTHUB_BASE_URL` | no | `""` | Prompt registry; only for KPI scoring |
| `ANONYMIZATION_URL` | no | `""` | PII service; only for `anonymize` |
| `SSL_VERIFY` | no | `False` | Verify the gateway's TLS certificate |
| `SSL_CA_BUNDLE` | no | `None` | Path to the corporate CA (PEM). Setting it implies verification |
| `MAX_RETRIES` | no | `3` | SDK-level retries; **stacks** with this library's own |
| `REQUEST_TIMEOUT_SECONDS` | no | `120` | Per-request timeout |

See `env.sample`.

### TLS in the corporate environment

Internal gateways present certificates signed by a corporate CA that is **not**
in the system trust store, so `SSL_VERIFY=True` alone fails the handshake.
Supply the CA as well:

```bash
SSL_VERIFY=True
SSL_CA_BUNDLE=/etc/ssl/certs/corp-ca.pem
```

The bundle is loaded into an `ssl.SSLContext` (httpx deprecated passing a bare
path). A missing or malformed bundle raises `ConfigurationError` at startup
rather than failing later with an opaque handshake error.

Setting `SSL_CA_BUNDLE` implies verification, so it takes precedence over a
false `SSL_VERIFY`. Leave both unset only for local development.

In a container, either mount the CA or bake it into the image:

```dockerfile
COPY corp-ca.pem /etc/ssl/certs/corp-ca.pem
ENV SSL_VERIFY=True SSL_CA_BUNDLE=/etc/ssl/certs/corp-ca.pem
```

---

## Command line

The same logic is exposed as commands, which is how a container is invoked.

```bash
python -m voice_analytics transcribe \
    --input  call.wav \
    --output call.json \
    --target-lang Hindi
```

`--output -` (the default) writes to stdout. **Results go to stdout,
diagnostics to stderr**, so stdout can be piped safely.

| Command | Runs | Purpose |
| --- | --- | --- |
| `transfer` | **per batch** | SFTP in, GCS out. Needs the `transfer` extra. |
| `transcribe` | per file | Audio in, transcript out. `--anonymize` removes PII first. |
| `build-prompt` | **per batch** | KPI codes in, the scoring prompt out. |
| `analyse` | per call | Transcript plus prompt in, KPI scores out. |
| `extract` | per call | Transcript in, topics and the agent's name out. |
| `summarize` | per call | Transcript or audio in, a summary out. |

`build-prompt` runs once per batch on purpose: a 12,480-file batch makes one
PromptHub request instead of 12,480 identical ones.

`analyse` and `extract` both accept either plain text or the JSON `transcribe`
writes, so one command's output goes straight into the next with nothing in
between:

```bash
python -m voice_analytics transcribe --input call.wav --output call.json
python -m voice_analytics analyse    --input call.json --prompt kpis.txt
python -m voice_analytics extract    --input call.json
```

Run `python -m voice_analytics --help` for the full list.

### Exit codes

A contract an orchestrator can branch on. Add codes; never renumber them.

| Code | Meaning | Retry? |
|---|---|---|
| 0 | Success | — |
| 1 | Unexpected error | Maybe |
| 2 | Bad command-line usage | No |
| 3 | Configuration missing or invalid | No — fix the environment |
| 4 | Input unreadable, or an argument rejected | No |
| 5 | Gateway rejected the credential | No — fix the secret |
| 6 | Rate limited after internal retries | Yes, later |
| 7 | Gateway unreachable or failing | Yes |
| 8 | Result unusable (unparseable, or silent audio) | Rarely helps |
| 9 | Personal information could not be removed | Only if transient |
| 130 | Interrupted (SIGINT/SIGTERM) | — |

---

## Running the whole pipeline without Airflow

The reference DAG needs Kubernetes, GCS and Postgres. None of that is needed to
watch the pipeline work end to end -- every stage is a command, and
`scripts/run_local.py` runs those commands over a folder of recordings the way
the DAG runs them over pods.

```bash
python -m voice_analytics build-prompt \
    --kpi-codes rpc_verified,disclosure_given --output kpis.txt

python scripts/run_local.py \
    --input-dir ./calls --output-dir ./out \
    --prompt kpis.txt --target-lang English
```

```
5 recording(s) to process, 4 at a time.
...
5 of 5 recording(s) processed in 2.5s
  call_0001.wav: 3/4 KPIs; about outstanding amount, credit limit; agent Priya; 1 unevidenced
```

```
out/transcripts/call_0001.json     anonymised, and translated from the anonymised text
out/analysis/call_0001.json        KPI scores, rollups, risk, call impact
out/extracted/call_0001.json       topics and agent name
out/summary.json                   every file's headline numbers
```

It is a **subprocess** runner, not a set of library calls. Pods invoke the CLI
and are judged on their exit code, so running it the same way makes a local run
a rehearsal rather than an approximation -- the same ordering, the same
concurrency limit, the same halt-above-N%-failed rule, and the same exit codes
translated into plain English:

```
call_0001.wav: FAILED -- personal information could not be removed, so nothing was saved
```

### Writing the rows too

By default the JSON files are the result. To also write what the DAG writes:

```bash
createdb voice_analytics_local
psql voice_analytics_local -f docs/schema/bootstrap.sql
export VOICE_ANALYTICS_DSN=postgresql://localhost/voice_analytics_local

python scripts/run_local.py --input-dir ./calls --prompt kpis.txt \
    --postgres --batch-id 1
```

```
Writing to Postgres...
  transcriptions: 3 completed, 0 failed
  calls: 3 scored (3 analyses)
  batch 1 is now 'completed' (analysis batch 4a8b83d8-...)
```

Through the **same `voice_analytics_store` module the DAG uses**, so a local run
writes identical rows -- a rehearsal, not an approximation. See ADR 0007.

The batch row and its KPI configuration must already exist: this runs the
pipeline, it does not create the configuration the pipeline reads.

The library itself still writes nothing (ADR 0001). `voice_analytics_store` is
a separate package, installed with the `store` extra, and no pod has it.

Useful flags: `--concurrency N`, `--limit N` to try a handful first,
`--no-extract` to skip topics and agent name, `--quiet` to hide each command's
own logs.

---

## Container

The image runs one command and exits. It has **no listening port and no
server** — it is a job, not a service.

```bash
docker build -t voice-analytics:0.1.0 .

docker run --rm \
  -e LLM_BASE_URL=https://10.216.70.62/DEV/litellm \
  -e LLM_API_KEY=sk-your-key \
  -e MODEL_NAME=gemini-2.5-flash-transcription \
  -v "$(pwd)/data:/data" \
  voice-analytics:0.1.0 \
  transcribe --input /data/call.wav --output /data/call.json
```

Build against an approved internal base image and package index:

```bash
docker build \
  --build-arg BASE_IMAGE=<internal-registry>/python-312-ubi9 \
  --build-arg PYPI_INDEX_URL=<internal-index-url> \
  -t voice-analytics:0.1.0 .
```

### From Airflow

```python
KubernetesPodOperator(
    task_id="transcribe_call",
    name="transcribe-worker",
    namespace="default",
    image="artifactory.hdfcbank.com/genai/voice-analytics:0.1.0",
    cmds=["python", "-m", "voice_analytics"],
    arguments=[
        "transcribe",
        "--input",  "/data/call.wav",
        "--output", "/data/call.json",
        "--target-lang", "Hindi",
    ],
    env_from=[k8s.V1EnvFromSource(
        secret_ref=k8s.V1SecretEnvSource(name="your-team-llm-secret")
    )],
    volumes=[shared_volume],
    volume_mounts=[shared_volume_mount],
    is_delete_operator_pod=True,
)
```

Each consuming team supplies **their own** secret, so usage, quota and cost
land with the caller. No credential is shared, and no argument carries one.

The caller must also ensure the pod can reach the LLM gateway on the network,
and that input and output paths are mounted.

---

## Scoring a call against KPIs

Resolve the prompts **once per batch**, then reuse the assembled prompt for
every call. Resolving per call would mean one registry request per file.

```python
from voice_analytics import analyse, build_consolidated_prompt
from voice_analytics.prompthub import (
    build_prompthub_client, fetch_prompts,
    resolve_base_prompt, resolve_kpi_prompts,
)

# once per batch
async with build_prompthub_client(settings) as hub:
    prompts = await fetch_prompts(hub, settings, litellm_api_key="sk-...")

base = resolve_base_prompt(prompts)
kpi_prompts, skipped = resolve_kpi_prompts(prompts, kpi_codes=["rpc_verified", ...])
prompt = build_consolidated_prompt(base, kpi_prompts)

# per call
result = await analyse(client, transcript, prompt, model_name="...")
```

```python
result.kpis            # every KPI: score, rationale, evidence
result.by_objective    # [ObjectiveRollup(objective="Compliance", scored=8, total=8, average=7.4), ...]
result.unevidenced_kpis
result.risk            # optional; None when the model returns no risk block
```

Three things to know:

- **There is no overall score.** Results are grouped by objective, as the
  interface presents them. A caller that needs a headline number computes it,
  and can choose its own weighting without changing the library.
- **A score without evidence is a failed KPI, not a zero.** A KPI that returns
  a number with no supporting quote has its score discarded and its code listed
  in `unevidenced_kpis`. Treating it as zero would make an unjustifiable score
  indistinguishable from a genuine failure.
- **Only `APPROVED` prompt versions are used.** A version in review is ignored
  in favour of the last approved one. `resolve_kpi_prompts` also returns the
  codes it could not resolve, so a batch scored against fewer KPIs than were
  selected is visible rather than silent.

---

## Package layout

```
src/voice_analytics/
├── cli/             command entry points — the container's public interface
├── config/          Settings class + load_settings()
├── exceptions/      library exception hierarchy
├── llm/
│   ├── client.py    build_llm_client / close_llm_clients
│   ├── base.py      audio and text request plumbing
│   └── json_repair.py  recovering JSON from unreliable model output
├── prompts/
│   ├── manager.py   Jinja rendering over a YAML catalogue
│   └── data/        catalogue shipped inside the wheel
├── transcription/
│   ├── service.py   transcribe() and translate()
│   ├── formatting.py  segments to text, speaker-label repair
│   ├── duration.py    call length from the last timestamp
│   └── languages.py   supported-language allow-list
├── analysis/        KPI scoring, the evidence rule, objective rollups
├── extraction/
│   ├── service.py   extract_keywords() — the generic "find these" call
│   └── presets.py   extract_topics() and extract_agent_name()
├── anonymization/   PII removal, before storage and before translation
├── summarization/   short summary, bullet points, key insights
├── prompthub/       caller-side prompt resolution
├── transfer/        SFTP to GCS, streaming and hashing (optional extra)
└── ...

src/voice_analytics_store/   the persistence half -- a separate package
├── batch.py         config in, status and counters out
├── transcripts.py   seeding rows, recording results
├── analysis.py      calls / call_analyses / analysis_kpi_results
└── connection.py    psycopg, imported lazily
└── schemas/         result models
```

---

## Client lifecycle

`build_llm_client` returns a client owning an HTTP connection pool. Reuse it
across requests; do not build one per call.

```python
from voice_analytics import build_llm_client, close_llm_clients

client = build_llm_client(settings)
try:
    ...
finally:
    await close_llm_clients()      # release sockets at shutdown
```

---

## Errors

All exceptions inherit `VoiceAnalyticsError`. They carry **no HTTP status
codes** — mapping them onto responses is the calling application's job.

| Exception | Raised when |
|---|---|
| `ConfigurationError` | A required setting is missing or invalid |
| `LLMAuthenticationError` | The gateway rejected the credential (never retried) |
| `LLMRateLimitError` | Still rate-limited after all retries |
| `LLMServiceError` | Gateway unreachable or failing after all retries |
| `LLMOutputParsingError` | The response could not be parsed as JSON |
| `TranscriptionError` | Transcription could not be completed |
| `PromptNotFoundError` | No prompt exists for that domain and task |

`ValueError` is raised for caller mistakes — a missing `model_name`, empty
audio, or an unsupported language — and is deliberately not wrapped.

Retry policy for `transcribe`: three attempts total, with 5s then 10s backoff.
Authentication failures and model-capability mismatches are never retried,
because they cannot succeed on a second attempt.

---

## PII and translation ordering

`transcribe(target_lang=...)` transcribes and translates in a **single** call,
which means raw text -- including any personal information -- reaches the
translation model and whatever you store.

Where that is not acceptable, the order must be **transcribe → anonymize →
translate**. Use the flag, which enforces it:

```bash
python -m voice_analytics transcribe \
    --input call.wav --anonymize --target-lang Hindi --output call.json
```

Or in code:

```python
result = await transcribe(client=client, content=audio, mime_type="audio/wav",
                          model_name=MODEL, target_lang=None)   # no translation yet

async with build_anonymization_client(settings) as anon:
    clean = await anonymize(anon, settings, result.transcript)

translated = await translate(client=client, text=clean,
                             target_lang="Hindi", model_name=MODEL)
```

Three things to know:

- **This ordering is a compliance control, not an optimisation.** Do not
  collapse it back into one call.
- **Anonymization fails closed.** If the service is unreachable or returns
  nothing usable, `AnonymizationError` is raised and the run fails. There is no
  fallback to the original text — that would leak precisely what the step
  exists to remove.
- **`--anonymize` clears `segments`.** Only the whole transcript is
  anonymized, so the per-segment text would still hold PII. The timestamped
  lines survive inside `transcript`; the structured `segments` list does not.

---

## Notes for callers migrating from the service

- **No HTML escaping.** The library returns data; the boundary that renders it
  decides how to encode. The service escaped in three places, which
  double-encoded ampersands in real output.
- **Parse failures raise.** `parse_agent_output` raises `LLMOutputParsingError`
  rather than returning `{"error": ...}`, which callers routinely forgot to check.
- **The prompt catalogue is packaged**, so it loads from any working directory.
- **No API-key validation or model allow-list enforcement.** The service enforced
  these in `validate_request_params`. If your deployment needs them, enforce
  them at your own boundary — the library does not.

---

## Development

```bash
uv sync --all-groups
uv run pytest                          # unit tests: no network, no database
uv run pytest --cov=src/voice_analytics --cov-report=term-missing
```

Tests construct `Settings` inline and stub the gateway, so the suite needs no
infrastructure and no `.env` file.

### IDE setup

This project uses a `src/` layout, and `uv sync` installs it as an **editable
install** — a `.pth` file inside `.venv` holding an *absolute* path to `src/`.

Two consequences:

- **Never copy `.venv` between machines.** The path inside it will not exist on
  the new one, and every import breaks. Copy the source, then run `uv sync`.
- **The IDE must use this project's interpreter**, not a system Python.

If your editor reports `Unresolved reference: voice_analytics` (PyCharm) or
`Import "voice_analytics" could not be resolved` (VS Code):

```bash
uv run pytest -q     # passes? the code is fine -- it is an IDE setting
```

**Rebuild the environment**

```bash
rm -rf .venv          # Windows: Remove-Item -Recurse -Force .venv
uv sync --all-groups
uv run python -c "import voice_analytics; print(voice_analytics.__file__)"
```

**Point the IDE at it**

| Editor | Steps |
|---|---|
| PyCharm | Settings → Project → Python Interpreter → Add Local Interpreter → Select existing → `.venv/bin/python` (Windows: `.venv\Scripts\python.exe`) |
| VS Code | `Cmd/Ctrl+Shift+P` → Python: Select Interpreter → the one under `./.venv` |

In PyCharm, also right-click `src` → **Mark Directory as → Sources Root**.

Open the folder containing `pyproject.toml` as the project root — not its
parent, and not `src/`.

### Testing the CLI without a real gateway

`scripts/fake_gateway.py` answers `POST /v1/chat/completions` with a canned
transcript. It exercises the full path -- argument parsing, settings, client,
retries, JSON repair, formatting, file output and exit codes -- with no VPN and
no credentials. Standard library only.

```bash
python scripts/fake_gateway.py &                 # listens on 127.0.0.1:8099

LLM_BASE_URL=http://127.0.0.1:8099/v1 \
LLM_API_KEY=sk-fake \
MODEL_NAME=gemini-2.5-flash-transcription \
  uv run python -m voice_analytics transcribe \
      --input call.wav --target-lang en --output result.json
```

Flags for exercising failure paths:

| Flag | Simulates | Expected exit code |
|---|---|---|
| *(none)* | Success | 0 |
| `--garbage` | Prose wrapped around the JSON | 0 — repair recovers it |
| `--fail-times 2` | Two 503s, then success | 0 — after backoff |
| `--rate-limit` | Persistent 429 | 6 |

> Note that the OpenAI SDK retries internally as well, so `MAX_RETRIES` in the
> environment multiplies with this library's own attempts. Set `MAX_RETRIES=0`
> if you want this library's retry policy to be the only one.
