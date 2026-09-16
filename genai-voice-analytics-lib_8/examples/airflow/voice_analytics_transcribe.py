"""Transcription only -- the first slice of the pipeline, for testing.

Audio already in GCS, one pod per file, transcripts written to Postgres. No
scoring, no extraction, no SFTP transfer. `voice_analytics_pipeline.py` is the
full thing; this exists so transcription can be proved end to end on a cluster
before the rest is wired up.

    read_config -> validate_access -> list_audio_files
                -> transcribe (one pod per file) -> persist_transcripts
                -> finalise

Because it does not score, it needs **none** of the KPI configuration:
`kpis`, `kpi_sections`, `usecase_kpi_configs`, `usecase_kpi_selections` and
`batch_kpi_configs` can all be empty. One `transcription_batches` row is
enough.

Like the full DAG, this imports `voice_analytics_store` but never
`voice_analytics`: the library reaches Airflow as a container image, and only
the persistence is shared code. See ADR 0007.

Trigger it with:

    {"transcription_batch_id": 196}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import requests
from airflow import DAG
from airflow.decorators import task
from airflow.exceptions import AirflowException, AirflowFailException
from airflow.models import Variable
from airflow.operators.python import get_current_context
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.google.cloud.hooks.gcs import GCSHook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.utils.log.secrets_masker import mask_secret
from kubernetes.client import models as k8s

import voice_analytics_store as store

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Read inside tasks, never at module scope: Airflow re-parses this file every
# 30 seconds, and a module-level Variable.get() is a database round trip on
# every one of those parses.

IMAGE = "registry.internal/voice-analytics:0.13.2"

TRANSCRIPTION_MODEL_NAME = "gemini-2.5-flash"

POSTGRES_CONN_ID = "analytics_db"
GCS_CONN_ID = "google_cloud_default"
KUBERNETES_CONN_ID = "kubernetes_default"
NAMESPACE = "speech-analytics"

#: Mount point of the GCS FUSE volume inside every pod. Pods read audio and
#: write transcripts through the filesystem, so the image needs no cloud SDK.
GCS_MOUNT = "/gcs"

# The container's exit codes are the whole error contract:
#
#   0 success          3 configuration    6 rate limited
#   1 unexpected       4 invalid input    7 upstream unavailable
#   2 usage            5 authentication   8 processing failed
#                                         9 anonymisation failed
#
# Pods get one Airflow retry. The library already retried transient gateway
# failures internally (three attempts, 5s then 10s), so a non-zero exit has
# generally exhausted the retries worth taking; the one here covers a node
# eviction or a failed image pull.


def _conn():
    """A connection to the analytics database, from the Airflow connection."""
    return store.connect(PostgresHook(postgres_conn_id=POSTGRES_CONN_ID).get_uri())


def _var(name: str, default: str | None = None) -> str:
    value = Variable.get(name, default_var=default)
    if value is None or value == "":
        raise AirflowFailException(f"Airflow Variable '{name}' is not set")
    return value


def _mark_batch_failed(context) -> None:
    """Mark the batch and its pending rows failed.

    Marking only the batch is the failure mode that looks fine on a dashboard:
    the batch is red, every file still says it is waiting its turn, and the
    counters never add up.
    """
    conf = (context.get("dag_run").conf or {}) if context.get("dag_run") else {}
    batch_id = conf.get("transcription_batch_id")
    if not batch_id:
        print("WARNING: failure callback fired with no transcription_batch_id")
        return

    ti = context.get("task_instance")
    failed_task = ti.task_id if ti else "unknown"
    try:
        with _conn() as conn:
            store.mark_pipeline_failed(conn, batch_id, failed_task, "transcription")
            conn.commit()
        print(f"Batch {batch_id} marked failed (task={failed_task})")
    except Exception as exc:  # noqa: BLE001 -- a callback must never raise
        print(f"WARNING: could not mark batch {batch_id} failed: {exc}")


default_args = {
    "owner": "speech-analytics",
    "depends_on_past": False,
    "on_failure_callback": _mark_batch_failed,
    "retries": 2,
    "retry_delay": timedelta(seconds=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(seconds=30),
}


with DAG(
    dag_id="voice_analytics_transcribe",
    description="Transcribe a batch of recordings already in GCS",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=10,
    default_args=default_args,
    tags=["voice-analytics", "transcribe"],
) as dag:

    # -----------------------------------------------------------------------
    @task
    def read_config() -> dict:
        """Read the batch row once. Every later task works from this dict.

        No KPI lookup here, unlike the full pipeline: transcription does not
        need one, so a batch with no KPI configuration is perfectly valid.
        """
        conf = (get_current_context()["dag_run"].conf) or {}
        batch_id = conf.get("transcription_batch_id")
        if batch_id is None:
            raise AirflowFailException("Missing dag_run.conf.transcription_batch_id")

        with _conn() as conn:
            config = store.read_batch_config(conn, int(batch_id))
            store.mark_batch_processing(conn, int(batch_id))
            conn.commit()

        print(
            f"Batch {config['batch_id']} ({config['usecase_name']}): "
            f"target_lang={config['target_lang']} romanize={config['romanize']}"
        )
        return config

    # -----------------------------------------------------------------------
    @task
    def validate_access(state: dict) -> dict:
        """Get this use case's key for the transcription model, from PromptHub.

        There is no single shared gateway key: each use case has its own key
        per model, and a model is usable only when the use case is APPROVED,
        not disabled, and its entry carries an `llmApiKey` with no
        `removeTokenModelAccess` flag.

        **A fallback exists for a first cluster test**: if `PROMPTHUB_BASE_URL`
        is not set, the `LLM_API_KEY` Variable is used and a warning is logged.
        That skips the entitlement check, which is the only place per-use-case
        model access is enforced anywhere in the system -- so it is for
        bringing the DAG up, not for running batches.
        """
        base = Variable.get("PROMPTHUB_BASE_URL", default_var="")
        if not base:
            print(
                "WARNING: PROMPTHUB_BASE_URL is not set, so this run uses the "
                "static LLM_API_KEY Variable and DOES NOT check whether this "
                "use case may use the model. Configure PromptHub before "
                "running real batches."
            )
            key = _var("LLM_API_KEY")
            mask_secret(key)
            return {**state, "transcription_model": TRANSCRIPTION_MODEL_NAME,
                    "transcription_llm_key": key, "entitlement_checked": False}

        try:
            response = requests.get(
                f"{base.rstrip('/')}/client/usecase/{state['usecase_id']}",
                headers={"accept": "*/*",
                         "INTERNAL-API-KEY": _var("PROMPTHUB_INTERNAL_API_KEY")},
                timeout=60,
                # The originating DAG passed verify=False here, on a request
                # carrying an internal API key. Inside the corporate network
                # that accepts any certificate presented.
                verify=_var("CORP_CA_BUNDLE", "/etc/ssl/certs/corp-ca.pem"),
            )
        except requests.exceptions.RequestException as exc:
            raise AirflowException(f"Could not reach PromptHub: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise AirflowException(
                f"PromptHub is unavailable ({response.status_code}): {response.text}")
        if not response.ok:
            raise AirflowFailException(
                f"PromptHub rejected the request ({response.status_code}): "
                f"{response.text}")

        body = response.json()
        usecase = body.get("data") if body.get("success") else None
        if not usecase:
            raise AirflowFailException(
                f"PromptHub returned no use case for {state['usecase_id']}: "
                f"{body.get('message')}")

        if usecase.get("disable") or usecase.get("status") != "APPROVED":
            raise AirflowFailException(
                f"Use case {state['usecase_id']} is disabled or not approved in "
                "PromptHub, so nothing may be transcribed against it.")

        entry = None
        for model in usecase.get("models") or []:
            if (model.get("modelName") or "").strip().lower() != TRANSCRIPTION_MODEL_NAME:
                continue
            if model.get("removeTokenModelAccess") or not model.get("llmApiKey"):
                entry = None
            else:
                entry = model
            break

        if not entry:
            raise AirflowFailException(
                f"Use case {state['usecase_id']} may not use "
                f"{TRANSCRIPTION_MODEL_NAME}, so the calls cannot be transcribed.")

        # The key lands in XCom, which is plain text in the Airflow metadata
        # database. mask_secret keeps it out of the logs; see the notes below.
        mask_secret(entry["llmApiKey"])
        return {
            **state,
            "transcription_model": entry.get("modelName", TRANSCRIPTION_MODEL_NAME),
            "transcription_llm_key": entry["llmApiKey"],
            "entitlement_checked": True,
        }

    # -----------------------------------------------------------------------
    @task
    def list_audio_files(state: dict) -> list[dict]:
        """List the recordings for this batch and seed one row per file.

        Rows are created up front with status 'pending', so the batch's file
        count is right from the start and a file whose pod never runs leaves a
        visible row rather than a silently missing one.
        """
        bucket = _var("AUDIO_BUCKET")
        prefix = f"batches/{state['batch_id']}/audio/"
        gcs = GCSHook(gcp_conn_id=GCS_CONN_ID)

        names = [n for n in gcs.list(bucket, prefix=prefix) if not n.endswith("/")]
        if not names:
            raise AirflowFailException(
                f"No audio files under gs://{bucket}/{prefix}. Upload them there, "
                "or check the batch id."
            )

        with _conn() as conn:
            seeded = store.seed_transcriptions(conn, state["batch_id"], [
                {"name": os.path.basename(n), "file_path": f"gs://{bucket}/{n}"}
                for n in sorted(names)
            ])
            conn.commit()

        print(f"{len(seeded)} recording(s) queued for batch {state['batch_id']}")
        return [{**s, "stem": os.path.splitext(s["name"])[0]} for s in seeded]

    # -----------------------------------------------------------------------
    @task
    def transcribe_arguments(state: dict, items: list[dict]) -> list[list[str]]:
        """One argument list per file, for the mapped pod operator."""
        batch = state["batch_id"]
        commands = []
        for item in items:
            args = [
                "transcribe",
                "--input", f"{GCS_MOUNT}/batches/{batch}/audio/{item['name']}",
                "--output", f"{GCS_MOUNT}/batches/{batch}/transcripts/{item['stem']}.json",
                "--model", state["transcription_model"],
                # Anonymise inside the same pod, which also forces the order:
                # transcribe, anonymise, then translate -- so no personal
                # information reaches the translation model or the database.
                "--anonymize",
                "--log-format", "json",
            ]
            if state["target_lang"]:
                args += ["--target-lang", state["target_lang"]]
            if state["romanize"]:
                args.append("--romanize")
            commands.append(args)
        return commands

    # -----------------------------------------------------------------------
    volumes = [
        k8s.V1Volume(
            name="gcs",
            csi=k8s.V1CSIVolumeSource(
                driver="gcsfuse.csi.storage.gke.io",
                volume_attributes={"bucketName": "{{ var.value.AUDIO_BUCKET }}"},
            ),
        ),
        k8s.V1Volume(
            name="corp-ca",
            config_map=k8s.V1ConfigMapVolumeSource(name="corp-ca-bundle"),
        ),
    ]
    mounts = [
        k8s.V1VolumeMount(name="gcs", mount_path=GCS_MOUNT),
        k8s.V1VolumeMount(name="corp-ca", mount_path="/etc/ssl/certs/corp-ca.pem",
                          sub_path="corp-ca.pem", read_only=True),
    ]

    transcribe_pods = KubernetesPodOperator.partial(
        task_id="transcribe",
        name="va-transcribe",
        namespace=NAMESPACE,
        kubernetes_conn_id=KUBERNETES_CONN_ID,
        image=IMAGE,
        cmds=["python", "-m", "voice_analytics"],
        env_vars=[
            k8s.V1EnvVar(name="LLM_BASE_URL", value="{{ var.value.LLM_BASE_URL }}"),
            # Credentials go in the environment, never in `arguments`: those are
            # visible in the pod spec, in `kubectl describe`, and in the log.
            k8s.V1EnvVar(
                name="LLM_API_KEY",
                value="{{ ti.xcom_pull(task_ids='validate_access')['transcription_llm_key'] }}"),
            k8s.V1EnvVar(
                name="MODEL_NAME",
                value="{{ ti.xcom_pull(task_ids='validate_access')['transcription_model'] }}"),
            k8s.V1EnvVar(name="ANONYMIZATION_URL",
                         value="{{ var.value.ANONYMIZATION_URL }}"),
            # TLS on, pointed at the internal CA. SSL_VERIFY=True alone fails
            # the handshake: the system trust store has no corporate root.
            k8s.V1EnvVar(name="SSL_VERIFY", value="True"),
            k8s.V1EnvVar(name="SSL_CA_BUNDLE", value="/etc/ssl/certs/corp-ca.pem"),
            # The SDK's own retries multiply with the library's three attempts.
            k8s.V1EnvVar(name="MAX_RETRIES", value="0"),
            k8s.V1EnvVar(name="REQUEST_TIMEOUT_SECONDS", value="300"),
        ],
        volumes=volumes,
        volume_mounts=mounts,
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "512Mi"},
            limits={"cpu": "1", "memory": "2Gi"},
        ),
        # Failed pods are kept so `kubectl logs` still works on them.
        on_finish_action="delete_succeeded_pod",
        get_logs=True,
        # Where the old service's asyncio.Semaphore(10) used to be.
        max_active_tis_per_dag=8,
        retries=1,
        execution_timeout=timedelta(minutes=30),
    )

    # -----------------------------------------------------------------------
    @task(trigger_rule="all_done")
    def persist_transcripts(state: dict, items: list[dict]) -> dict:
        """Read each pod's output from the bucket and write it to Postgres.

        ``all_done`` so this runs even when some pods failed: a file with no
        result is recorded as failed rather than left at 'pending' forever.
        Committed per file, so a crash halfway leaves the finished files
        recorded.
        """
        bucket = _var("AUDIO_BUCKET")
        gcs = GCSHook(gcp_conn_id=GCS_CONN_ID)

        with _conn() as conn:
            for item in items:
                key = f"batches/{state['batch_id']}/transcripts/{item['stem']}.json"
                if not gcs.exists(bucket, key):
                    store.mark_transcription_failed(
                        conn, item["transcription_id"],
                        "Transcription pod produced no output")
                else:
                    store.persist_transcript(
                        conn, item["transcription_id"],
                        json.loads(gcs.download(bucket, key).decode("utf-8")))
                conn.commit()

            tally = store.transcription_tally(conn, state["batch_id"])
            conn.commit()

        print(f"{tally['completed']} transcribed, {tally['failed']} failed "
              f"({tally['failed_pct']}%)")
        return tally

    # -----------------------------------------------------------------------
    @task(trigger_rule="all_done")
    def finalise(state: dict, tally: dict) -> None:
        """Set the batch's final status from what the rows actually say.

        Deleting the audio is **not** done here. The full pipeline deletes it
        after scoring; while you are testing, you will want to re-run against
        the same files. Set DELETE_AUDIO_AFTER_TRANSCRIBE to "true" to turn it
        on, and remember that a re-run then needs the files uploading again.
        """
        if Variable.get("DELETE_AUDIO_AFTER_TRANSCRIBE",
                        default_var="false").lower() == "true":
            bucket = _var("AUDIO_BUCKET")
            gcs = GCSHook(gcp_conn_id=GCS_CONN_ID)
            removed = 0
            for name in gcs.list(bucket, prefix=f"batches/{state['batch_id']}/audio/"):
                try:
                    gcs.delete(bucket, name)
                    removed += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"WARNING: could not delete gs://{bucket}/{name}: {exc}")
            with _conn() as conn:
                store.clear_file_paths(conn, state["batch_id"])
                conn.commit()
            print(f"{removed} recording(s) deleted; no audio retained")

        with _conn() as conn:
            result = store.finalise_batch(conn, state["batch_id"])
            conn.commit()

        print(f"Batch {state['batch_id']} finished: {result['status']} "
              f"({result['completed']} done, {result['failed']} failed)")

        if not state.get("entitlement_checked", True):
            print("NOTE: this run did not verify model entitlement -- "
                  "PROMPTHUB_BASE_URL was not configured.")

    # -----------------------------------------------------------------------
    config = read_config()
    validated = validate_access(config)
    files = list_audio_files(validated)

    transcribed = transcribe_pods.expand(
        arguments=transcribe_arguments(validated, files))
    tally = persist_transcripts(validated, files)

    transcribed >> tally >> finalise(validated, tally)


# ---------------------------------------------------------------------------
# What this needs, and what it does not
# ---------------------------------------------------------------------------
#
# Database: ONE row in transcription_batches. Nothing else. The KPI tables can
# be empty, because nothing here scores anything.
#
# API keys in XCom: validate_access returns PromptHub's per-use-case key, and
# XCom is stored as plain text in the Airflow metadata database. mask_secret
# keeps it out of the logs, not out of that table. The durable fix is to write
# it into a short-lived Kubernetes Secret and return only its name. Flagged
# rather than assumed, because it is a deployment decision.
#
# GCS FUSE vs an init container: pods read and write the bucket as a
# filesystem, so the image needs no cloud SDK. Outside GKE, replace the CSI
# volume with an initContainer that copies the one object it needs into an
# emptyDir mounted at /gcs. Nothing else changes.
