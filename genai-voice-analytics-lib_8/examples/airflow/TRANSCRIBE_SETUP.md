# Running `voice_analytics_transcribe` on a cluster

Everything this DAG needs, and nothing it does not. Extracted from the DAG
itself rather than written from memory.

---

## 1. Airflow Variables

| Variable | Required | Value |
| --- | --- | --- |
| `LLM_BASE_URL` | **yes** | `https://10.216.70.62/DEV/litellm/v1` |
| `ANONYMIZATION_URL` | **yes** | `https://10.216.70.62/DEV/platform-auxiliary-services/api/v1/internal/anonymize-messages` |
| `AUDIO_BUCKET` | **yes** | your GCS bucket name, no `gs://` |
| `PROMPTHUB_BASE_URL` | see below | `https://10.216.70.62/DEV/prompthub-service` |
| `PROMPTHUB_INTERNAL_API_KEY` | with the above | the internal key |
| `LLM_API_KEY` | only as a fallback | a static gateway key |
| `CORP_CA_BUNDLE` | no | path to the CA PEM **on the scheduler**, default `/etc/ssl/certs/corp-ca.pem` |
| `DELETE_AUDIO_AFTER_TRANSCRIBE` | no | `false` while testing |

Note the `/v1` on `LLM_BASE_URL` -- the originating service's
`ANALYTICS_LLM_BASE_URL` carries it, and without it requests 404.

### PromptHub, or the fallback

With `PROMPTHUB_BASE_URL` set, `validate_access` fetches **this use case's own
key for this model** and refuses to proceed unless the use case is `APPROVED`,
not disabled, and its entry has an `llmApiKey` with no `removeTokenModelAccess`
flag. That is the only place per-use-case model entitlement is enforced
anywhere in the system.

Without it, the DAG uses the static `LLM_API_KEY` Variable and logs:

```
WARNING: PROMPTHUB_BASE_URL is not set, so this run uses the static
LLM_API_KEY Variable and DOES NOT check whether this use case may use the
model.
```

Use the fallback to bring the DAG up. Configure PromptHub before running real
batches.

---

## 2. Airflow Connections

| Conn Id | Type | Points at |
| --- | --- | --- |
| `analytics_db` | Postgres | the database holding the nine tables -- `ai_analytics_db`, **not** `speech_analytics_db` |
| `google_cloud_default` | Google Cloud | a service account with `objectAdmin` on the bucket |
| `kubernetes_default` | Kubernetes | the cluster, if the scheduler is not already in it |

`speech_analytics_db` belongs to a different application. Point at the wrong
one and the DAG fails on its first query saying the table does not exist.

---

## 3. Kubernetes objects

```yaml
# The corporate CA, mounted into every pod at /etc/ssl/certs/corp-ca.pem
apiVersion: v1
kind: ConfigMap
metadata:
  name: corp-ca-bundle
  namespace: speech-analytics
data:
  corp-ca.pem: |
    -----BEGIN CERTIFICATE-----
    ...
```

`SSL_VERIFY=True` on its own fails the handshake: the system trust store has
no corporate root, so the bundle must be there.

Also needed:

* the namespace `speech-analytics`, or change `NAMESPACE` in the DAG;
* the image pushed to a registry the cluster can pull from, and `IMAGE` in the
  DAG pointing at it;
* the **GCS FUSE CSI driver** enabled on the cluster. Outside GKE, replace the
  CSI volume with an initContainer that copies the one object it needs into an
  `emptyDir` mounted at `/gcs` -- nothing else changes.

---

## 4. Database

**One row.** That is the whole requirement:

```sql
INSERT INTO transcription_batches
       (batch_name, usecase_id, usecase_name, metadata, status,
        schedule_type, "source", created_at)
VALUES ('cluster test',
        '<your real usecase_id>',
        'Voice-Analytics',
        '{"target_language": "English", "romanize": false}'::jsonb,
        'pending', 'run_now', 'audio_upload', now())
RETURNING id;
```

Note the id it returns -- that is what you trigger the DAG with.

### What the columns do

| Column | Effect |
| --- | --- |
| `usecase_id` | **required** -- what PromptHub resolves the key against. The DAG refuses a batch without one. |
| `metadata.target_language` | becomes `--target-lang`. Omit it and nothing is translated. |
| `metadata.romanize` | becomes `--romanize` |
| `status` | set to `processing` at the start, then `completed` / `partial` / `failed` |

### What you do NOT need

`kpis`, `kpi_sections`, `usecase_kpi_configs`, `usecase_kpi_selections` and
`batch_kpi_configs` can all be **empty**. This DAG does not score anything, so
it never reads them. The full pipeline does.

The `transcriptions` rows are created by the DAG. Do not insert them yourself.

---

## 5. The audio

Upload to a prefix named after the batch id:

```bash
gsutil -m cp *.mp3 gs://<AUDIO_BUCKET>/batches/<batch id>/audio/
```

Recognised extensions: `.wav .mp3 .m4a .mp4 .ogg .opus .flac .webm .aac`.
Anything else is not listed, and the DAG fails saying so.

Transcripts are written back to `batches/<id>/transcripts/<name>.json` by the
pods, and read from there by `persist_transcripts`.

---

## 6. Trigger it

Airflow UI -> `voice_analytics_transcribe` -> Trigger DAG w/ config:

```json
{"transcription_batch_id": 196}
```

---

## What success looks like

```
Batch 196 (Voice-Analytics): target_lang=English romanize=False
13 recording(s) queued for batch 196
  [13 mapped `transcribe` pods]
13 transcribed, 0 failed (0%)
Batch 196 finished: completed (13 done, 0 failed)
```

```sql
SELECT filename, detected_language, status,
       length(transcript)            AS transcript_chars,
       length(translated_transcript) AS translated_chars
FROM   transcriptions WHERE batch_id = 196 ORDER BY id;
```

`translated_chars` should be close to but not equal to `transcript_chars` --
the translation is made from the **anonymised** text, which is shorter.

---

## When a pod fails

The failing pod is kept (`on_finish_action="delete_succeeded_pod"`), so:

```bash
kubectl -n speech-analytics get pods | grep va-transcribe
kubectl -n speech-analytics logs <pod>
```

Its exit code names the cause:

| Exit | Meaning | Usually |
| --- | --- | --- |
| 3 | configuration | `SSL_CA_BUNDLE` path wrong, or a Variable missing |
| 4 | invalid input | the audio could not be read |
| 5 | authentication | the gateway rejected the key |
| 7 | upstream unavailable | gateway unreachable from the pod -- check egress |
| 8 | processing failed | the model's reply could not be used |
| 9 | **anonymisation failed** | the PII service was unreachable. Nothing was stored. |

A pod that fails does not stop the batch: its row is marked `failed` with the
reason, the rest continue, and `finalise` reports `partial`.

---

## Two things to decide before real batches

**API keys in XCom.** `validate_access` returns PromptHub's per-use-case key,
and XCom is plain text in the Airflow metadata database. `mask_secret` keeps it
out of the logs, not out of that table. The durable fix is to write it into a
short-lived Kubernetes Secret and return only its name. It is flagged rather
than assumed because it is a deployment decision.

**Nothing records that anonymisation ran.** There is no column for it. If the
claim is "PII is removed before storage", the database cannot evidence it. An
`anonymised_at timestamptz` on `transcriptions` costs nothing.
