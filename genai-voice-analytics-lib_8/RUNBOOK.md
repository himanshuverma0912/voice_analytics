# Testing transcription, step by step

For a laptop with PyCharm, DBeaver and a Postgres container. Three phases,
each one proving something the next depends on.

| Phase | Proves | Needs |
| --- | --- | --- |
| A | the install works | nothing |
| B | the rows land | Postgres |
| C | the real gateway works | VPN, a key, the CA bundle |

Do them in order. Most first-run problems on a cluster are a bad key, a bad CA
path or an unreachable anonymisation service -- A and B cost ten minutes and
C then tells you which of those it is.

---

## Phase A -- offline. No database, no VPN.

### 1. Install

```bash
cd genai-voice-analytics-lib
uv sync --extra store
uv run python -m voice_analytics --version        # voice-analytics 0.12.6
uv run pytest -q                                  # 461 passed, 29 skipped
```

The 29 skipped need Postgres; they run in Phase B.

In PyCharm: Settings -> Python Interpreter -> Add -> Existing -> `.venv/bin/python`.
If `import voice_analytics` goes red, that is the only cause.

### 2. Configuration

```bash
cat > .env <<'EOF'
LLM_BASE_URL=http://127.0.0.1:8099/v1
LLM_API_KEY=sk-fake
MODEL_NAME=gemini-2.5-flash
ANONYMIZATION_URL=http://127.0.0.1:8098/anonymize
SSL_VERIFY=False
EOF
```

Every command reads `.env`, so which terminal you use stops mattering.

### 3. Audio

```bash
mkdir -p calls
cp /path/to/your/recording.mp3 calls/
```

Recognised: `.wav .mp3 .m4a .mp4 .ogg .opus .flac .webm .aac`.

### 4. The stand-ins

The real gateway is on the corporate network. Two scripts answer in its place:

```bash
uv run python scripts/fake_gateway.py --port 8099 &
uv run python scripts/fake_anonymizer.py --port 8098 &
```

They die when the terminal tab closes. Exit 7 or 9 later usually means they
stopped.

### 5. Run

```bash
uv run python scripts/run_local.py \
    --input-dir ./calls --output-dir ./out \
    --transcribe-only --target-lang English
```

```
2 of 2 recording(s) processed in 0.9s
  sample_1.wav: 155 chars Hindi; translated 139
```

Drop `--quiet`-free output and look for this ordering:

```
Personal information removed (156 characters in, 155 out)
Translating 155 characters to English...
```

**155, not 156.** The translator received the anonymised text. That is the
guarantee the pipeline exists for, and it is visible in the log.

Results are in `out/transcripts/`. **The transcript is the stub's canned text,
not your audio** -- the fake gateway ignores the bytes. Phase C fixes that.

---

## Phase B -- with Postgres.

### 1. A database

```bash
docker run -d --name pg -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:15
docker exec -it pg createdb -U postgres voice_analytics_local
docker exec -i pg psql -U postgres -d voice_analytics_local < docs/schema/bootstrap.sql
```

A database of its own, not the default `postgres` one: `--reset` below
truncates every table.

> **Never point this at the dev server.** `bootstrap.sql` is for an empty
> database, and the seeding script deletes rows. Both refuse a connection
> string that does not look local, but the real protection is the address.

Add it in DBeaver: `localhost:5432/voice_analytics_local`, user/password
`postgres`. Tables appear under Schemas -> public -> Tables.

### 2. Point at it

```bash
echo 'VOICE_ANALYTICS_DSN=postgresql://postgres:postgres@localhost:5432/voice_analytics_local' >> .env
```

### 3. A batch

```bash
uv run python scripts/seed_local_batch.py --reset
```

Transcription needs no KPI configuration, so ignore the KPI codes it prints.
Note the batch id.

### 4. Run, writing rows

```bash
uv run python scripts/run_local.py \
    --input-dir ./calls --output-dir ./out \
    --transcribe-only --target-lang English \
    --postgres --batch-id 1
```

```
Writing to Postgres...
  transcriptions: 2 completed, 0 failed
  batch 1 is now 'completed' (transcription only)
```

### 5. Look at it

```sql
SELECT filename, detected_language, status,
       length(transcript)            AS chars,
       length(translated_transcript) AS translated
FROM   transcriptions WHERE batch_id = 1;
```

`translated` close to but **not equal to** `chars` -- the translation was made
from the anonymised text.

`calls` and `call_analyses` stay empty. `--transcribe-only` writes the
transcription half and nothing else, which is what the transcribe-only DAG
does.

### 6. Optionally, the database tests

```bash
createdb voice_analytics_test        # or docker exec ... createdb
export VOICE_ANALYTICS_TEST_DSN=postgresql://postgres:postgres@localhost:5432/voice_analytics_test
uv run pytest -q                     # 490 passed
```

**A different database.** They truncate between tests and would wipe your
batch.

---

## Phase C -- the real gateway.

On the VPN.

### 1. Your key

There is no shared gateway key: each use case has its own, per model, held in
PromptHub.

```bash
export PROMPTHUB_BASE_URL=https://10.216.70.62/DEV/prompthub-service
export PROMPTHUB_INTERNAL_API_KEY=<the internal key>

uv run python scripts/fetch_usecase_key.py --usecase-id <YOUR id> --write-env
```

Your use case id, not `1149` -- that came from one sample row. Find it:

```sql
SELECT DISTINCT usecase_id, usecase_name FROM transcription_batches;   -- on dev
```

If the script cannot reach PromptHub but curl can, it is usually a proxy:

```bash
export HTTPS_PROXY=http://proxy.internal:8080
```

or skip the request entirely:

```bash
curl -sk -H 'INTERNAL-API-KEY: <key>' \
     https://10.216.70.62/DEV/prompthub-service/client/usecase/<id> > usecase.json

uv run python scripts/fetch_usecase_key.py --usecase-id <id> \
    --from-response usecase.json --write-env
```

`usable NO` means the pipeline would refuse this use case, exactly as the DAG
would, before spending anything.

### 2. Point at the real endpoints

```bash
cat > .env <<'EOF'
LLM_BASE_URL=https://10.216.70.62/DEV/litellm/v1
MODEL_NAME=gemini-2.5-flash
ANONYMIZATION_URL=https://10.216.70.62/DEV/platform-auxiliary-services/api/v1/internal/anonymize-messages
SSL_VERIFY=True
SSL_CA_BUNDLE=/absolute/path/to/corp-ca.pem
REQUEST_TIMEOUT_SECONDS=300

VOICE_ANALYTICS_DSN=postgresql://postgres:postgres@localhost:5432/voice_analytics_local
EOF

uv run python scripts/fetch_usecase_key.py --usecase-id <YOUR id> --write-env
```

Note the `/v1` -- without it, requests 404.

`SSL_VERIFY=True` alone fails the handshake: the system trust store has no
corporate root, so `SSL_CA_BUNDLE` must point at the PEM. Cannot find it?
`SSL_VERIFY=False` gets a first run through. Do not leave it that way.

**The DSN stays local.** Read the dev gateway, write to your own database.

### 3. Stop the stand-ins

```bash
kill %1 %2
```

### 4. One file first

```bash
uv run python -m voice_analytics transcribe \
    --input calls/your_recording.mp3 --output check.json --anonymize
```

Fastest feedback there is. Success looks like real numbers:

```
Transcript ready: 47 segments, 8213 characters, language detected as Hindi
```

Thousands of characters and dozens of segments, not the stub's 156 and 3. Open
`check.json` -- that is your actual call.

### 5. Then the folder

```bash
uv run python scripts/seed_local_batch.py --reset

uv run python scripts/run_local.py \
    --input-dir ./calls --output-dir ./out \
    --transcribe-only --target-lang English \
    --postgres --batch-id 1
```

---

## When something fails

The exit code names the cause.

| Exit | Meaning | Usually |
| --- | --- | --- |
| 3 | configuration | `SSL_CA_BUNDLE` path wrong, or a variable missing |
| 4 | invalid input | the audio could not be read, or the folder has none |
| 5 | authentication | the key was rejected, or the use case is not entitled to the model |
| 7 | upstream unavailable | gateway unreachable -- VPN? proxy? stand-ins stopped? |
| 8 | processing failed | the model's reply could not be used |
| 9 | anonymisation failed | the PII service is unreachable. **Nothing was stored.** |

Test the anonymisation service separately if you see 9 -- it is a different
host path from the gateway and can fail on its own.

### Re-running

A batch is one-shot. Running the same `--batch-id` again **adds a second set of
rows**:

```bash
uv run python scripts/seed_local_batch.py --reset    # wipe and start over
uv run python scripts/seed_local_batch.py            # or keep it, get a new id
rm -rf out
```

---

## Then Airflow

`examples/airflow/TRANSCRIBE_SETUP.md`. Phase C will already have proved the
key, the CA bundle and the anonymisation endpoint -- most of what goes wrong on
a first cluster run.
