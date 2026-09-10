# genai-voice-analytics-lib

Reusable voice analytics building blocks, packaged as an importable Python
library. Extracted from the `genai-speech-analytics` service so the same logic
can run inside a web API, an Airflow worker pod, a batch job or a notebook.

**Version 0.1.0 ships transcription.** Summarization, validation and KPI
analysis follow in later releases and reuse the same LLM and prompt layers.

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
| 130 | Interrupted (SIGINT/SIGTERM) | — |

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
│   └── languages.py   supported-language allow-list
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

`transcribe(target_lang=...)` transcribes and translates in a single call.

Where a transcript must be anonymized before any PII reaches storage or a
second service, split the two steps:

```python
result = await transcribe(client=client, content=audio,
                          mime_type="audio/wav", model_name=MODEL,
                          target_lang=None)          # transcribe only
clean = await anonymize(result.transcript)           # your anonymization step
translated = await translate(client=client, text=clean,
                             target_lang="Hindi", model_name=MODEL)
```

This ordering is a compliance control, not an optimisation. Do not collapse it
back into one call.

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
