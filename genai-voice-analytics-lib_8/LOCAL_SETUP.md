# Running this on your Mac

Written for PyCharm and DBeaver, with no Airflow, no Kubernetes and no VPN.

---

## First: which database is your DBeaver pointed at?

This is the one question that changes everything.

> ### ⚠️ Do not run any of this against the dev server
>
> The schema exports you provided came from dev, and those tables already
> exist there. `bootstrap.sql` is for creating them somewhere empty, and
> `seed_local_batch.py --reset` **deletes every row in all nine tables**.
>
> Both scripts refuse to run against a connection string that does not look
> local, but the real protection is pointing them somewhere else. Use a
> throwaway database on your Mac.

Two paths. Pick one.

| | Path A: no database | Path B: local Postgres |
| --- | --- | --- |
| Setup | none | install Postgres, create tables, seed a batch |
| Output | JSON files | JSON files **and** rows |
| Good for | seeing the pipeline work | rehearsing what the DAG does |

---

## Path A -- no database, five minutes

### 1. Open the project in PyCharm

Unzip it, then **File -> Open** and choose the `genai-voice-analytics-lib`
folder.

### 2. Create the interpreter

In a terminal, at the project root:

```bash
uv sync
```

Then **PyCharm -> Settings -> Project -> Python Interpreter -> Add
Interpreter -> Existing**, and pick `.venv/bin/python` inside the project.

> If PyCharm underlines `import voice_analytics` in red, the interpreter is
> not the project's `.venv`. That is the only cause -- the package is under
> `src/`, and `uv sync` installs it in editable mode, so a correct interpreter
> resolves it.

### 3. Start the stand-ins

The real gateway and anonymisation service are on the corporate network. Two
scripts answer in their place, standard library only:

```bash
uv run python scripts/fake_gateway.py --port 8099 &
uv run python scripts/fake_anonymizer.py --port 8098 &
```

### 4. Point the tools at them

```bash
export LLM_BASE_URL=http://127.0.0.1:8099/v1
export LLM_API_KEY=sk-fake
export MODEL_NAME=gemini-2.5-flash
export ANONYMIZATION_URL=http://127.0.0.1:8098/anonymize
export SSL_VERIFY=False
```

### 5. Run one file

Put a `.wav` or `.mp3` in a folder called `calls/`, then:

```bash
uv run python -m voice_analytics transcribe \
    --input calls/your_file.wav --output out.json \
    --anonymize --target-lang English
```

Watch for this ordering in the log -- it is the guarantee the pipeline exists
for:

```
Personal information removed (156 characters in, 155 out)
Translating 155 characters to English...
```

**155, not 156.** The translator received the anonymised text.

### 6. Run the whole folder

```bash
printf 'Score this call.\n\nKPI CODE : rpc_verified\nDid the agent verify identity?\n' > kpis.txt

uv run python scripts/run_local.py \
    --input-dir ./calls --output-dir ./out --prompt kpis.txt
```

```
out/transcripts/<file>.json    anonymised, and translated from the anonymised text
out/analysis/<file>.json       KPI scores, rollups, risk, call impact
out/extracted/<file>.json      topics and agent name
out/summary.json               every file's headline numbers
```

Open `out/summary.json` in PyCharm. That is Path A finished.

---

## Path B -- with a local Postgres

Everything in Path A, plus rows.

### 1. Install and start Postgres

**Any Postgres 13 or newer works.** `bootstrap.sql` checks the version and
refuses early rather than failing halfway through. Nothing in this schema needs
anything newer:

| Feature used | Available since |
| --- | --- |
| `gen_random_uuid()` | 13 (before that, the `pgcrypto` extension) |
| `FILTER (WHERE ...)` | 9.4 |
| `jsonb`, GIN on `jsonb` | 9.4 |

#### Already have a Postgres container?

Use it. Start it with a mapped port, and give this a database of its own:

```bash
docker run -d --name pg -p 5432:5432 \
    -e POSTGRES_PASSWORD=postgres postgres:15

docker exec -it pg createdb -U postgres voice_analytics_local

export VOICE_ANALYTICS_DSN=postgresql://postgres:postgres@localhost:5432/voice_analytics_local
```

Then `psql` from your Mac, or run the DDL through the container:

```bash
docker exec -i pg psql -U postgres -d voice_analytics_local < docs/schema/bootstrap.sql
```

A separate database inside the container, not the default `postgres` one --
so `--reset` and the test suite cannot touch anything else living there.

#### Or install it

```bash
brew install postgresql@16
brew services start postgresql@16
```

No Homebrew? [Postgres.app](https://postgresapp.com) works the same way. Or, if
you would rather install nothing, the test suite's bundled server also works:

```bash
uv pip install pgserver
uv run python -c "import pgserver, pathlib; \
print(pgserver.get_server(pathlib.Path('/tmp/pgdata'), cleanup_mode=None).get_uri())"
```

That prints a connection string you can use directly and delete afterwards.

### 2. Create a throwaway database and its tables

```bash
createdb voice_analytics_local
psql voice_analytics_local -f docs/schema/bootstrap.sql
```

**Yes, you need to create the tables.** The exports you gave me were from dev;
your Mac has nothing. Your local database stands in for **`ai_analytics_db`** --
the originating service runs two, and that is the one the pipeline uses
(`speech_analytics_db` holds a different application's jobs workflow). `bootstrap.sql` creates the nine tables the pipeline
uses, built from those exports with the primary keys, foreign keys and
constraints added back from the ORM (DBeaver's export omits them).

In DBeaver, add a connection to `localhost:5432/voice_analytics_local` and you
will see all nine.

### 3. Tell the tools where it is

Put everything in a `.env` file at the project root instead of exporting:

```bash
cat > .env <<'EOF'
LLM_BASE_URL=http://127.0.0.1:8099/v1
LLM_API_KEY=sk-fake
MODEL_NAME=gemini-2.5-flash
ANONYMIZATION_URL=http://127.0.0.1:8098/anonymize
SSL_VERIFY=False
VOICE_ANALYTICS_DSN=postgresql://localhost/voice_analytics_local
EOF
```

Every command reads it, so **it does not matter which terminal you use**, and
nothing is lost when you close one. Real environment variables still win, so a
one-off override on the command line works.

> `.env` is in `.gitignore`. Keep real credentials there, not in a shell
> history or a run configuration.

### 4. Seed a batch

The pipeline **reads** its configuration; it does not create it. In production
the API and UI write that. Locally:

```bash
uv run python scripts/seed_local_batch.py
```

```
Batch 1 created.
  use case      1149 (Voice-Analytics), config version 1
  KPIs          rpc_verified, recording_consent, polite_greeting, no_evidence_demo
  target lang   English
```

Those four KPI codes are the ones the fake gateway scores, so everything lines
up out of the box. `--kpi-codes a,b,c` overrides them; `--reset` empties the
tables first.

### 5. Run it, writing rows

```bash
uv run python scripts/run_local.py \
    --input-dir ./calls --output-dir ./out --prompt kpis.txt \
    --postgres --batch-id 1
```

```
Writing to Postgres...
  transcriptions: 3 completed, 0 failed
  calls: 3 scored (3 analyses)
  batch 1 is now 'completed' (analysis batch 7facbeb3-...)
```

### 6. Look at what landed, in DBeaver

```sql
SELECT status, total_files, completed_files FROM transcription_batches;

SELECT filename, detected_language, left(transcript, 60) FROM transcriptions;

SELECT c.filename, c.duration_sec, c.metadata->>'agent_name' AS agent,
       a.overall_score, a.topics, a.call_impact_level
FROM   calls c JOIN call_analyses a ON a.call_id = c.id;

-- The evidence rule: a score with no supporting quote is discarded, not zeroed
SELECT k.kpi_code, r.score, jsonb_array_length(coalesce(r."references",'[]')) AS quotes
FROM   analysis_kpi_results r JOIN kpis k ON k.id = r.kpi_id
ORDER  BY k.display_order;
```

The last query is the one worth looking at:

```
 kpi_code           | score | quotes
--------------------+-------+--------
 rpc_verified       |  0.00 |      1
 recording_consent  | 10.00 |      1
 polite_greeting    |  9.00 |      1
 no_evidence_demo   |       |      0     <-- NULL, not 0
```

`no_evidence_demo` is NULL because the model scored it without quoting the
transcript. A score without evidence is a failed KPI, not a zero -- and that
rule survives all the way into the database.

**These are the same rows the Airflow DAG writes**, through the same
`voice_analytics_store` module. A local run is a rehearsal, not an
approximation.

---

## Running the tests

```bash
uv run pytest -q                    # 384 passed, 27 skipped
```

The 27 skipped need a database. To run them:

```bash
createdb voice_analytics_test
export VOICE_ANALYTICS_TEST_DSN=postgresql://localhost/voice_analytics_test
uv run pytest -q                    # 411 passed
```

They create their own tables and truncate between tests, so use a database you
do not mind losing. **Not the one from step 2** -- they would wipe your seeded
batch.

---

## When you are on the corporate network

### There is no single gateway key

The originating system never used one. Each use case has its own key **per
model**, held in PromptHub, and the pipeline fetches it before doing any work:

```
GET {PROMPTHUB_BASE_URL}/client/usecase/{usecase_id}
    header: INTERNAL-API-KEY

-> { "data": { "status": "APPROVED", "disable": false,
               "models": [ { "modelName": "gemini-2.5-flash",
                             "llmApiKey": "sk-...",
                             "removeTokenModelAccess": false } ] } }
```

A model is usable only when the use case is `APPROVED`, not `disable`d, and its
entry carries an `llmApiKey` with no `removeTokenModelAccess` flag. That is
`validate_access` in the DAG -- the only place per-use-case model entitlement is
enforced anywhere in the system. The same key then authenticates to PromptHub
for the prompts themselves, as `lite-llm-api-key`.

Find yours:

```bash
export PROMPTHUB_BASE_URL=https://10.216.70.62/DEV/prompthub-service
export PROMPTHUB_INTERNAL_API_KEY=<the internal key>

uv run python scripts/fetch_usecase_key.py --usecase-id 1149 --write-env
```

### If curl reaches PromptHub but the script does not

Usually a proxy. Either export it:

```bash
export HTTPS_PROXY=http://proxy.internal:8080
```

or skip the request entirely -- run your own curl and feed the reply in:

```bash
curl -sk -H 'INTERNAL-API-KEY: <the internal key>' \
     https://10.216.70.62/DEV/prompthub-service/client/usecase/1149 > usecase.json

uv run python scripts/fetch_usecase_key.py --usecase-id 1149 \
    --from-response usecase.json --write-env
```

Same output, no request made. Save the **whole** response body, not one field.

```
Use case 1149: Voice-Analytics
  status       APPROVED
  disabled     False

  model                          usable  key
  gemini-2.5-flash               yes     sk-abc...wxyz
  qwen3-30b-a3b-instruct         yes     sk-def...1234

LLM_API_KEY written to .env. Not printed, so it is not in your shell history.
```

`usable NO` means the pipeline would refuse this use case -- exactly as the DAG
would, before spending anything.

### Then swap the fakes for the real endpoints:

```bash
export LLM_BASE_URL=https://10.216.70.62/DEV/litellm
export LLM_API_KEY=<your key>
export ANONYMIZATION_URL=https://10.216.70.62/DEV/platform-auxiliary-services/api/v1/internal/anonymize-messages
export SSL_VERIFY=True
export SSL_CA_BUNDLE=/path/to/corp-ca.pem
```

`SSL_VERIFY=True` on its own will fail the handshake -- the system trust store
has no corporate root, so `SSL_CA_BUNDLE` must point at it. Everything else is
identical.

Keep writing to your **local** database even then. Reading the dev gateway is
fine; writing test rows into the dev database is not.

---

## If something does not work

| Symptom | Cause |
| --- | --- |
| PyCharm underlines `import voice_analytics` | Interpreter is not the project's `.venv` |
| `LLM_BASE_URL is not set` | The exports above were not run in this shell |
| `exit code 3` | A configuration variable is missing or wrong |
| `exit code 9` | Anonymisation failed, so nothing was saved. Is `fake_anonymizer.py` still running? |
| `No transcription_batches row with id=N` | Seed one, or pass the id `seed_local_batch.py` printed |
| `has no KPIs selected` | The batch has no `batch_kpi_configs` row -- re-run the seeder |
| `Refusing to seed: ... does not look like a local database` | `VOICE_ANALYTICS_DSN` points at a server. That is the guard working. |
| `The AI returned no KPI scores at all`, with `response_keys=primary_language,segments` | The gateway answered with a *transcript*. Your prompt file has no KPI marker it recognises -- it needs a line like `KPI CODE : some_code`. |
| A batch that already ran will not run again cleanly | `--batch-id` is one-shot. Re-run `seed_local_batch.py` for a fresh id, or `--reset` to start over. |

### Re-running after a failure

A run that fails leaves the batch at `failed` and its rows at `failed`. Running
the same `--batch-id` again **adds a second set of rows** rather than replacing
the first. Start clean:

```bash
uv run python scripts/seed_local_batch.py --reset   # empties all nine tables
```

or drop `--reset` to leave the previous run in place and get a new batch id.
