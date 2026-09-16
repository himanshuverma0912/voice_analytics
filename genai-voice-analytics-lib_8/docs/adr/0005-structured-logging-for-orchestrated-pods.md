# ADR 0005 — Logs are written for people; where try/except belongs

**Status:** Accepted
**Date:** 2026-09-11

## Context

These libraries run as Airflow `KubernetesPodOperator` tasks, and the pod's
output becomes the Airflow task log. Whoever opens that log is often not the
person who wrote the code — an operator, a support engineer, or a business
analyst checking why a batch failed.

A first attempt used `key=value` machine syntax with a start/complete pair for
every step. It produced 24 lines for a successful run, most of which reported
that a function had been entered and had returned in 0.0 ms. It was
greppable but not readable, and the signal was buried.

## Decision

### One line, two audiences

Every line is a plain-English sentence, with the technical detail appended in
brackets. The sentence carries the meaning; the brackets carry the evidence.

```
Read input file call.wav (2.5 MB).  [path=/data/call.wav bytes=2646060]
└──────── an operator reads this ──┘ └──── a developer reads this ────┘
```

A reader who wants the gist stops at the full stop. A developer reads on. One
call site, written once:

```python
say(logger, "Read input file %s (%s).", name, size,
    path=str(file_path), bytes=len(data))
```

Three renderings from that same call:

| Mode | Output |
|---|---|
| default | `Read input file call.wav (2.5 MB).  [path=... bytes=2646060]` |
| `--plain` | `Read input file call.wav (2.5 MB).` |
| `--log-format json` | sentence in `message`, detail as typed fields in `detail` |

The sentence must stand alone: detail is additive, never load-bearing. A test
enforces this.

### The sentences themselves

The default format is prose, not machine syntax:

```
Starting 'transcribe' (voice-analytics 0.1.0, run 6e68dde772f4, host worker-7).
Read input file sample-call.wav (2.5 MB).
Connecting to the AI service at https://10.216.70.62/DEV/litellm (TLS verification on).
Transcribing 2.5 MB of audio and translating to English using model 'gemini-2.5-flash-transcription'...
AI service responded in 8.4s.
Transcript ready: 12 segments, 1843 characters, language detected as Hindi.
Saved result to /data/call.json (2.4 KB).
Finished successfully in 9.1s.
```

Two rules keep it short:

* **Log an outcome, not a function call.** A step that completes in under a
  second and cannot meaningfully fail on its own gets no line.
* **Log before anything slow**, so a pause is explained rather than looking
  like a hang. The line before a long wait ends in `...`.

Sizes are rendered as `2.5 MB`, durations as `8.4s` — not raw byte counts and
milliseconds.

`--log-format json` emits one object per line, with `detail` as **native
types** — `{"segments": 3, "elapsed_ms": 187.5}`, not strings to re-parse.
`--plain` drops the brackets entirely for an audience that only wants the
narrative.

### Where try/except belongs

Handlers exist for three reasons, and no others:

1. **Add context, then re-raise.** A missing prompt logs which domain was asked
   for and which exist, then propagates unchanged.
2. **Classify and translate.** Gateway errors become library exceptions, and the
   message says whether it is being retried and why.
3. **Clean up.** `finally` releases the HTTP client whether or not the call
   succeeded.

There is exactly **one** swallowing handler, at the process boundary in
`cli/main.py`, which turns anything unexpected into exit code 1 and logs the
traceback.

### Secrets

Anything credential-like is masked by `mask_secret`. Verified end to end: a key
does not appear anywhere in verbose output. `httpx`, `httpcore` and `openai`
are pinned to WARNING unless `--verbose`, because they log request headers at
DEBUG.

## Consequences

**Positive**

- A successful transcription is **8 lines**, down from 24. Every line reports
  something a reader can act on.
- Non-technical readers can follow a run without knowing the codebase.
- Failures state the problem and the remedy: *"Configuration problem: these
  settings are missing or invalid: LLM_BASE_URL. See env.sample for what each
  one means."*
- Waits are explained: *"The AI service did not respond properly
  (InternalServerError). Waiting 5 seconds, then trying again (attempt 2 of 3)."*

**Negative**

- Lines are longer. A default line runs 120-200 characters; `--plain` halves
  that where width matters.
- Two things to keep in sync per call: the sentence and the detail. A test
  guards the important half — the sentence must be readable alone.
- Per-phase timings are gone. Only the gateway call and the total are timed —
  which is where the time actually goes; the rest were sub-millisecond.
- Message wording appears in a few test assertions, so rewording is a visible
  change.

## Alternatives considered

**`key=value` for everything.** Tried, then reverted. Optimised for a grep that
is rarely run, at the cost of the reading that happens every time.

**Sentences only, no detail.** Also tried. Readable, but a developer diagnosing
a failure then had to reproduce the run to learn the gateway URL, the payload
size or the attempt number. Appending the detail costs a reader nothing and
saves a developer a round trip.

**Broad `try/except` around each step, logging and continuing.** Rejected: it
would let a pod exit 0 after failing, breaking the exit-code contract in
ADR 0003 and hiding bugs.
