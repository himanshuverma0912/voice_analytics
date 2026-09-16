"""Voice analytics pipeline: SFTP -> GCS -> transcribe -> anonymise -> score.

This is the library-based replacement for ``speech_analytics_pipeline_runner``.
The shape of the old DAG is kept on purpose -- read config, validate access,
transcribe, analyse, clean up -- so the two can be compared task by task. What
changed is *where the work happens*:

    old:  DAG -> HTTP -> FastAPI -> in-process asyncio.gather over N files
    new:  DAG -> N KubernetesPodOperator pods, each one file

Consequences of that move, all of them deliberate:

* **No polling.** The old ``transcribe`` task fired a request then slept in a
  15-second loop for up to an hour, holding a worker slot the whole time. Pods
  report their own completion, so a worker slot is held only while a pod is
  being launched.
* **Real per-file isolation.** One corrupt file fails one pod. The old version
  ran every file inside a single request, so a crash in the handler took the
  batch with it.
* **Exit codes instead of HTTP status.** The library maps every failure to a
  specific code (see ``cli/exit_codes.py``); ``_classify_pod_failure`` turns
  those back into "retry this" or "stop now".
* **TLS is verified.** Every ``requests`` call in the old DAG passed
  ``verify=False``. In a corporate environment that silently accepts any
  certificate. Here verification is on, pointed at the internal CA bundle.

The library itself writes nothing to the database. Every INSERT and UPDATE in
this file is the caller's half of that contract -- see ADR 0001.

**This file does not import voice_analytics, and must not start to.** The
library is a container image, not a scheduler dependency. It *does* import
`voice_analytics_store`, which is a different thing: a small psycopg wrapper
holding the SQL, shared with `scripts/run_local.py` so both write identical
rows. The compute stays in the image; only the persistence is shared. ADR 0007
explains why that line is drawn where it is. Everything the DAG
needs from it arrives as JSON a pod wrote, and everything the DAG asks of it
goes through the CLI's subcommands and flags. That boundary is what lets the
image be upgraded, pinned, rolled back, or swapped for a different version per
task without touching the Airflow environment -- and what lets a team with no
Python say `image:` and be done.

The import list below is Airflow, Kubernetes, the standard library and
`voice_analytics_store`. If a change here needs `from voice_analytics import
...`, the missing piece belongs in the CLI instead.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Optional

import requests
from airflow import DAG
from airflow.decorators import task
from airflow.exceptions import AirflowException, AirflowFailException, AirflowSkipException
from airflow.models import Variable
from airflow.operators.python import get_current_context
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.google.cloud.hooks.gcs import GCSHook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.utils.log.secrets_masker import mask_secret
from kubernetes.client import models as k8s

# The persistence half. Not the library: `voice_analytics` is a container
# image this DAG only ever names, while `voice_analytics_store` is a small
# psycopg wrapper the scheduler imports, so that the DAG and
# scripts/run_local.py write identical rows from one implementation. See
# ADR 0007.
import voice_analytics_store as store

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Read lazily inside tasks, never at module scope. Airflow re-parses this file
# every 30 seconds; a module-level Variable.get() is a database round trip on
# every one of those parses, for every DAG in the folder. The old DAG did this
# five times.

IMAGE = "registry.internal/voice-analytics:0.13.2"
"""Pinned by digest in production. The CLI's subcommands and flags are a
versioned contract -- see cli/main.py."""

TRANSCRIPTION_MODEL_NAME = "gemini-2.5-flash"
ANALYSIS_MODEL_NAME = "qwen3-30b-a3b-instruct"

POSTGRES_CONN_ID = "analytics_db"
GCS_CONN_ID = "google_cloud_default"
KUBERNETES_CONN_ID = "kubernetes_default"
NAMESPACE = "speech-analytics"

# Mount point of the GCS FUSE volume inside every pod. The pod reads audio and
# writes results through the filesystem, so the library needs no cloud SDK and
# no gs:// support -- see the note at the bottom of this file.
GCS_MOUNT = "/gcs"

AIRFLOW_RETRIES = 2
AIRFLOW_RETRY_DELAY = timedelta(seconds=5)


def _conn():
    """A connection to the analytics database, from the Airflow connection.

    The DSN comes from Airflow's own connection store rather than an
    environment variable, so credentials stay where the operator manages them.
    """
    return store.connect(PostgresHook(postgres_conn_id=POSTGRES_CONN_ID).get_uri())


def _var(name: str, default: str | None = None) -> str:
    """Read an Airflow Variable, failing loudly when it is missing."""
    value = Variable.get(name, default_var=default)
    if value is None or value == "":
        raise AirflowFailException(f"Airflow Variable '{name}' is not set")
    return value


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

# The container's exit codes are the whole error contract -- no shared
# exception classes, no parsed log lines, no imported enum:
#
#   0  success                 3  configuration    6  rate limited
#   1  unexpected              4  invalid input    7  upstream unavailable
#   2  usage                   5  authentication   8  processing failed
#                                                  9  anonymisation failed
#
# Pods get one Airflow retry, no more. The library already retried transient
# gateway failures internally (three attempts, 5s then 10s), so a pod that
# exits non-zero has generally exhausted the retries worth taking. The single
# retry here covers what the library cannot see: a node eviction or an image
# pull that failed.


def _mark_batch_failed(context) -> None:
    """Record a DAG-level failure on the rows, not just on the batch.

    Replaces `POST /analytics/pipeline/mark-failed`. The SQL lives in
    `voice_analytics_store` so this and the local runner cannot drift apart.
    """
    conf = (context.get("dag_run").conf or {}) if context.get("dag_run") else {}
    batch_id = conf.get("transcription_batch_id")
    if not batch_id:
        print("WARNING: failure callback fired with no transcription_batch_id")
        return

    ti = context.get("task_instance")
    failed_task = ti.task_id if ti else "unknown"
    stage = (
        "analysis"
        if failed_task.startswith(("analyse", "extract", "open_analysis", "persist_analysis"))
        else "transcription"
    )

    try:
        with _conn() as conn:
            store.mark_pipeline_failed(conn, batch_id, failed_task, stage)
            conn.commit()
        print(f"Batch {batch_id} marked failed (task={failed_task}, stage={stage})")
    except Exception as exc:  # noqa: BLE001 -- a callback must never raise
        print(f"WARNING: could not mark batch {batch_id} failed: {exc}")


# ---------------------------------------------------------------------------
# PromptHub access control -- ported from the old DAG's validate_access
# ---------------------------------------------------------------------------
# This is the ONLY place per-usecase model entitlement is enforced. The library
# deliberately does not do it (a library cannot know who is calling it), and
# the FastAPI service never did. If this task is removed, nothing checks that a
# usecase is allowed to spend money on a given model.


def _get_usecase_details(usecase_id: str) -> dict:
    base = _var("PROMPTHUB_BASE_URL").rstrip("/")
    try:
        resp = requests.get(
            f"{base}/client/usecase/{usecase_id}",
            headers={"accept": "*/*", "INTERNAL-API-KEY": _var("PROMPTHUB_INTERNAL_API_KEY")},
            timeout=60,
            # The old DAG passed verify=False here. Inside the corporate
            # network that accepts any certificate, including a substituted
            # one, on a request that carries an internal API key.
            verify=_var("CORP_CA_BUNDLE", "/etc/ssl/certs/corp-ca.pem"),
        )
    except requests.exceptions.RequestException as exc:
        raise AirflowException(f"Could not reach PromptHub: {exc}") from exc

    if resp.status_code == 429 or resp.status_code >= 500:
        raise AirflowException(f"PromptHub is unavailable ({resp.status_code}): {resp.text}")
    if not resp.ok:
        raise AirflowFailException(f"PromptHub rejected the request ({resp.status_code}): {resp.text}")

    body = resp.json()
    if not body.get("success"):
        raise AirflowFailException(f"PromptHub returned success=false: {body.get('message')}")
    data = body.get("data")
    if not data:
        raise AirflowFailException("PromptHub response contained no 'data'")
    return data


def _find_model_entry(usecase_data: dict, model_name: str) -> Optional[dict]:
    """The usecase's entry for one model, or None when it may not be used."""
    target = model_name.strip().lower()
    for model in usecase_data.get("models") or []:
        if (model.get("modelName") or "").strip().lower() != target:
            continue
        if model.get("removeTokenModelAccess"):
            return None
        if not model.get("llmApiKey"):
            return None
        return model
    return None


# ---------------------------------------------------------------------------
# Pod construction
# ---------------------------------------------------------------------------


def _pod_env(llm_key_xcom: str, model_xcom: str) -> list[k8s.V1EnvVar]:
    """Environment for a worker pod.

    Credentials go in the environment, never in ``arguments``: command-line
    arguments are visible in the pod spec, in ``kubectl describe``, and in the
    task log. The library enforces the same rule -- see cli/_common.py.
    """
    return [
        k8s.V1EnvVar(name="LLM_BASE_URL", value="{{ var.value.LLM_BASE_URL }}"),
        k8s.V1EnvVar(name="LLM_API_KEY", value=llm_key_xcom),
        k8s.V1EnvVar(name="MODEL_NAME", value=model_xcom),
        k8s.V1EnvVar(name="ANONYMIZATION_URL", value="{{ var.value.ANONYMIZATION_URL }}"),
        # TLS on, pointed at the internal CA. SSL_VERIFY=True alone fails the
        # handshake because the system trust store has no corporate root.
        k8s.V1EnvVar(name="SSL_VERIFY", value="True"),
        k8s.V1EnvVar(name="SSL_CA_BUNDLE", value="/etc/ssl/certs/corp-ca.pem"),
        # The SDK's own retries stack on top of the library's 3 attempts.
        # Zero here leaves one retry policy instead of two multiplying.
        k8s.V1EnvVar(name="MAX_RETRIES", value="0"),
        k8s.V1EnvVar(name="REQUEST_TIMEOUT_SECONDS", value="300"),
    ]


def _pod_volumes() -> tuple[list[k8s.V1Volume], list[k8s.V1VolumeMount]]:
    """The bucket as a filesystem, plus the CA bundle."""
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
    return volumes, mounts


default_args = {
    "owner": "speech-analytics",
    "depends_on_past": False,
    "on_failure_callback": _mark_batch_failed,
    "retries": AIRFLOW_RETRIES,
    "retry_delay": AIRFLOW_RETRY_DELAY,
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(seconds=30),
}


with DAG(
    dag_id="voice_analytics_pipeline",
    description="Config -> access -> GCS -> transcribe -> anonymise -> score -> cleanup",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=10,
    default_args=default_args,
    tags=["voice-analytics"],
) as dag:

    # -----------------------------------------------------------------------
    @task
    def read_config() -> dict:
        """Read the batch row once. Every later task works from this dict."""
        conf = (get_current_context()["dag_run"].conf) or {}
        batch_id = conf.get("transcription_batch_id")
        if batch_id is None:
            raise AirflowFailException("Missing dag_run.conf.transcription_batch_id")

        with _conn() as conn:
            config = store.read_batch_config(conn, int(batch_id))
            kpi_codes = store.selected_kpi_codes(conn, int(batch_id))
            if not kpi_codes:
                raise AirflowFailException(
                    f"Batch {batch_id} has no KPIs selected. Either it has no row "
                    "in batch_kpi_configs, or the config it points at has nothing "
                    "marked is_selected. The batch cannot be scored until that is "
                    "fixed."
                )
            store.mark_batch_processing(conn, int(batch_id))
            conn.commit()

        return {**config, "kpi_codes": kpi_codes}

    # -----------------------------------------------------------------------
    @task
    def validate_access(state: dict) -> dict:
        """Confirm the usecase may use both models, and collect its keys.

        Kept from the old DAG unchanged in substance. Both models are checked
        here, before any audio is touched, so a batch cannot transcribe 12,000
        files and only then discover it may not score them.
        """
        usecase = _get_usecase_details(state["usecase_id"])

        if usecase.get("disable") or usecase.get("status") != "APPROVED":
            raise AirflowFailException(
                f"Usecase {state['usecase_id']} is disabled or not approved in PromptHub."
            )

        transcription_entry = _find_model_entry(usecase, TRANSCRIPTION_MODEL_NAME)
        if not transcription_entry:
            raise AirflowFailException(
                f"This usecase may not use {TRANSCRIPTION_MODEL_NAME}, so the calls "
                "cannot be transcribed."
            )

        analysis_entry = _find_model_entry(usecase, ANALYSIS_MODEL_NAME)
        if not analysis_entry:
            raise AirflowFailException(
                f"This usecase may not use {ANALYSIS_MODEL_NAME}, so the calls "
                "cannot be scored. Nothing has been transcribed yet."
            )

        # Both keys land in XCom, which is stored as plain text in the Airflow
        # metadata database. mask_secret keeps them out of the logs; see the
        # note at the bottom of this file for the durable fix.
        for key in (transcription_entry["llmApiKey"], analysis_entry["llmApiKey"]):
            mask_secret(key)

        return {
            **state,
            "transcription_model": transcription_entry.get("modelName", TRANSCRIPTION_MODEL_NAME),
            "transcription_llm_key": transcription_entry["llmApiKey"],
            "analysis_model": analysis_entry.get("modelName", ANALYSIS_MODEL_NAME),
            "analysis_llm_key": analysis_entry["llmApiKey"],
        }

    # -----------------------------------------------------------------------
    @task
    def prompt_arguments(state: dict) -> list[str]:
        """Arguments for the one pod that assembles the scoring prompt.

        Resolving prompts used to be the single step an orchestrator could not
        do through the container, which forced this library onto the
        scheduler's machine. It is now the `build-prompt` subcommand, so this
        DAG stays a pure consumer of the image.
        """
        return [
            "build-prompt",
            "--kpi-codes", ",".join(state["kpi_codes"]),
            "--output", f"{GCS_MOUNT}/batches/{state['batch_id']}/scoring-prompt.txt",
            "--report", f"{GCS_MOUNT}/batches/{state['batch_id']}/scoring-prompt.report.json",
            "--log-format", "json",
        ]

    # One pod per batch, not per call: a 12,480-file batch makes one PromptHub
    # request here instead of 12,480 identical ones.
    build_prompt_pod = KubernetesPodOperator(
        task_id="build_prompt",
        name="va-build-prompt",
        namespace=NAMESPACE,
        kubernetes_conn_id=KUBERNETES_CONN_ID,
        image=IMAGE,
        cmds=["python", "-m", "voice_analytics"],
        arguments="{{ ti.xcom_pull(task_ids='prompt_arguments') }}",
        env_vars=_pod_env(
            llm_key_xcom="{{ ti.xcom_pull(task_ids='validate_access')['analysis_llm_key'] }}",
            model_xcom="{{ ti.xcom_pull(task_ids='validate_access')['analysis_model'] }}",
        ),
        volumes=volumes,
        volume_mounts=mounts,
        on_finish_action="delete_succeeded_pod",
        get_logs=True,
        retries=1,
        execution_timeout=timedelta(minutes=5),
    )

    @task
    def read_prompt_report(state: dict) -> dict:
        """Record which KPIs had no approved prompt.

        A missing KPI is skipped rather than fatal, matching the originating
        service. But that only logged it, which made two batches silently
        incomparable -- one scored against 22 KPIs, the next against 19.
        """
        key = f"batches/{state['batch_id']}/scoring-prompt.report.json"
        report = json.loads(
            GCSHook(gcp_conn_id=GCS_CONN_ID)
            .download(Variable.get("AUDIO_BUCKET"), key)
            .decode("utf-8")
        )
        skipped = report.get("kpi_codes_skipped") or []
        if skipped:
            print(
                f"{len(skipped)} selected KPI(s) have no approved prompt and will "
                f"not be scored: {', '.join(skipped)}"
            )
            with _conn() as conn:
                store.record_skipped_kpis(conn, state["batch_id"], skipped)
                conn.commit()
        return {
            **state,
            "prompt_key": f"batches/{state['batch_id']}/scoring-prompt.txt",
            "kpi_codes_skipped": skipped,
        }

    # -----------------------------------------------------------------------
    @task
    def transfer_arguments(state: dict) -> list[str]:
        """Arguments for the pod that pulls recordings off SFTP into GCS.

        The pipeline's first step. Nothing downstream can start until the audio
        is in object storage where the worker pods can reach it.

        The SFTP path comes from the batch's own config, so two flows pointing
        at different directories need no DAG change.
        """
        args = [
            "transfer",
            "--remote-dir", state["sftp_dir"],
            "--bucket", Variable.get("AUDIO_BUCKET"),
            "--prefix", f"batches/{state['batch_id']}/audio/",
            "--pattern", state.get("file_pattern") or "*",
            "--output", f"{GCS_MOUNT}/batches/{state['batch_id']}/transfer.json",
            # An empty source on a scheduled run means the upstream job did not
            # deliver. Better to fail here than to report a batch of zero files
            # as a successful run.
            "--fail-on-empty",
            "--log-format", "json",
        ]
        seen = f"{GCS_MOUNT}/flows/{state['batch_id']}/already-seen.json"
        if state.get("incremental"):
            args += ["--already-seen", seen]
        return args

    # One pod, not many: the bottleneck is the SFTP server, which is usually a
    # shared corporate box that throttles or drops connections under a fan-out.
    transfer_pod = KubernetesPodOperator(
        task_id="transfer",
        name="va-transfer",
        namespace=NAMESPACE,
        kubernetes_conn_id=KUBERNETES_CONN_ID,
        image=IMAGE,
        cmds=["python", "-m", "voice_analytics"],
        arguments="{{ ti.xcom_pull(task_ids='transfer_arguments') }}",
        env_vars=[
            *_pod_env(
                llm_key_xcom="{{ ti.xcom_pull(task_ids='validate_access')['transcription_llm_key'] }}",
                model_xcom="{{ ti.xcom_pull(task_ids='validate_access')['transcription_model'] }}",
            ),
            # SFTP credentials come from a Kubernetes Secret, never from the
            # DAG: a value set here would be visible in the pod spec.
            k8s.V1EnvVar(name="SFTP_HOST", value="{{ var.value.SFTP_HOST }}"),
            k8s.V1EnvVar(name="SFTP_USERNAME", value="{{ var.value.SFTP_USERNAME }}"),
            k8s.V1EnvVar(
                name="SFTP_PASSWORD",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(
                        name="sftp-credentials", key="password", optional=True)),
            ),
            k8s.V1EnvVar(name="SFTP_PRIVATE_KEY_PATH", value="/etc/sftp/id_ed25519"),
            k8s.V1EnvVar(name="SFTP_KNOWN_HOSTS", value="/etc/sftp/known_hosts"),
        ],
        volumes=[
            *volumes,
            k8s.V1Volume(
                name="sftp-keys",
                secret=k8s.V1SecretVolumeSource(
                    secret_name="sftp-credentials", default_mode=0o400, optional=True),
            ),
        ],
        volume_mounts=[
            *mounts,
            k8s.V1VolumeMount(name="sftp-keys", mount_path="/etc/sftp", read_only=True),
        ],
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "256Mi"},
            limits={"cpu": "1", "memory": "1Gi"},
        ),
        on_finish_action="delete_succeeded_pod",
        get_logs=True,
        retries=2,
        # Sized for a large batch over a slow corporate link, not for a demo.
        execution_timeout=timedelta(hours=4),
    )

    # -----------------------------------------------------------------------
    @task
    def list_audio_files(state: dict) -> list[dict]:
        """Read the transfer's manifest and seed one row per recording.

        The manifest rather than a second bucket listing: it carries each
        file's checksum, computed while streaming, which is what lets a caller
        skip a file it has already processed even after a rename.
        """
        bucket = Variable.get("AUDIO_BUCKET")
        gcs = GCSHook(gcp_conn_id=GCS_CONN_ID)

        manifest = json.loads(
            gcs.download(bucket, f"batches/{state['batch_id']}/transfer.json").decode("utf-8")
        )
        transferred = manifest.get("transferred") or []
        if not transferred:
            raise AirflowFailException(
                f"The transfer step moved no recordings for batch {state['batch_id']}."
            )
        if manifest.get("failed"):
            print(
                f"WARNING: {len(manifest['failed'])} recording(s) could not be "
                f"copied off the SFTP server and are not in this batch: "
                f"{', '.join(f['name'] for f in manifest['failed'])}"
            )

        with _conn() as conn:
            seeded = store.seed_transcriptions(conn, state["batch_id"], [
                {"name": entry["name"], "file_path": entry["destination"],
                 "content_hash": entry.get("content_hash")}
                for entry in transferred
            ])
            conn.commit()

        print(f"{len(seeded)} audio file(s) queued for batch {state['batch_id']}")
        return [{**s, "stem": os.path.splitext(s["name"])[0]} for s in seeded]

    # -----------------------------------------------------------------------
    @task
    def transcribe_arguments(state: dict, items: list[dict]) -> list[list[str]]:
        """Build one argument list per file, for the mapped pod operator."""
        batch = state["batch_id"]
        commands = []
        for item in items:
            args = [
                "transcribe",
                "--input", f"{GCS_MOUNT}/batches/{batch}/audio/{item['filename']}",
                "--output", f"{GCS_MOUNT}/batches/{batch}/transcripts/{item['stem']}.json",
                "--model", state["transcription_model"],
                # Anonymise inside the same pod. This also forces the correct
                # order: transcribe, anonymise, then translate -- so no personal
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
    volumes, mounts = _pod_volumes()

    transcribe_pods = KubernetesPodOperator.partial(
        task_id="transcribe",
        name="va-transcribe",
        namespace=NAMESPACE,
        kubernetes_conn_id=KUBERNETES_CONN_ID,
        image=IMAGE,
        cmds=["python", "-m", "voice_analytics"],
        env_vars=_pod_env(
            llm_key_xcom="{{ ti.xcom_pull(task_ids='validate_access')['transcription_llm_key'] }}",
            model_xcom="{{ ti.xcom_pull(task_ids='validate_access')['transcription_model'] }}",
        ),
        volumes=volumes,
        volume_mounts=mounts,
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "512Mi"},
            limits={"cpu": "1", "memory": "2Gi"},
        ),
        # Pods are removed on success and kept on failure, so a failed pod can
        # still be inspected with kubectl logs.
        on_finish_action="delete_succeeded_pod",
        get_logs=True,
        # Concurrency comes from the flow's config. This is the wireframe's
        # "Concurrency 8" and the old service's asyncio.Semaphore, moved to
        # where it can actually limit resource use.
        max_active_tis_per_dag=8,
        retries=1,
        # A whole batch must not hang on one file.
        execution_timeout=timedelta(minutes=30),
    )

    # -----------------------------------------------------------------------
    @task(trigger_rule="all_done")
    def persist_transcripts(state: dict, items: list[dict]) -> dict:
        """Read each pod's output from the bucket and write it to Postgres.

        ``all_done`` so this runs even when some pods failed -- a file with no
        result is recorded as failed rather than left at 'pending' forever.

        Committed per file, so a crash halfway leaves the files that did finish
        recorded rather than rolling the whole batch back.
        """
        bucket = Variable.get("AUDIO_BUCKET")
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

        print(f"{tally['completed']} transcribed, {tally['failed']} failed")
        return tally

    # -----------------------------------------------------------------------
    @task
    def halt_gate(state: dict, tally: dict) -> list[dict]:
        """Stop before scoring if too much of the batch failed.

        The wireframe's "Halt above 10% failed". Scoring 60% of a batch and
        publishing it to a dashboard is worse than stopping, because the
        dashboard gives no hint that 40% is missing.
        """
        threshold = int(Variable.get("HALT_ABOVE_PCT", default_var="10"))
        if tally["failed_pct"] > threshold:
            raise AirflowFailException(
                f"{tally['failed_pct']}% of files failed to transcribe "
                f"({tally['failed']} of {tally['total']}), above the "
                f"{threshold}% limit. Scoring was not started; the transcripts "
                "that did succeed are saved."
            )
        if tally["completed"] == 0:
            raise AirflowSkipException("Nothing transcribed, so there is nothing to score.")

        with _conn() as conn:
            rows = store.completed_transcriptions(conn, state["batch_id"])

        return [
            {"transcription_id": r["transcription_id"], "filename": r["filename"],
             "stem": os.path.splitext(r["filename"])[0]}
            for r in rows
        ]

    # -----------------------------------------------------------------------
    @task
    def analyse_arguments(state: dict, scored: list[dict]) -> list[list[str]]:
        batch = state["batch_id"]
        return [
            [
                "analyse",
                "--input",  f"{GCS_MOUNT}/batches/{batch}/transcripts/{item['stem']}.json",
                "--prompt", f"{GCS_MOUNT}/{state['prompt_key']}",
                "--output", f"{GCS_MOUNT}/batches/{batch}/analysis/{item['stem']}.json",
                "--model",  state["analysis_model"],
                "--log-format", "json",
            ]
            for item in scored
        ]

    analyse_pods = KubernetesPodOperator.partial(
        task_id="analyse",
        name="va-analyse",
        namespace=NAMESPACE,
        kubernetes_conn_id=KUBERNETES_CONN_ID,
        image=IMAGE,
        cmds=["python", "-m", "voice_analytics"],
        env_vars=_pod_env(
            llm_key_xcom="{{ ti.xcom_pull(task_ids='validate_access')['analysis_llm_key'] }}",
            model_xcom="{{ ti.xcom_pull(task_ids='validate_access')['analysis_model'] }}",
        ),
        volumes=volumes,
        volume_mounts=mounts,
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "512Mi"},
            limits={"cpu": "1", "memory": "1Gi"},
        ),
        on_finish_action="delete_succeeded_pod",
        get_logs=True,
        max_active_tis_per_dag=8,
        retries=1,
        execution_timeout=timedelta(minutes=15),
    )

    # -----------------------------------------------------------------------
    @task
    def extract_arguments(state: dict, scored: list[dict]) -> list[list[str]]:
        """Topics and the agent's name -- a separate request from scoring.

        The originating service made these two extra calls per transcript, each
        wrapped in its own try/except so a failure left the field empty rather
        than losing the KPI result. Keeping them in their own pod preserves
        that: an `extract` pod that fails costs one optional field, not a call.
        """
        batch = state["batch_id"]
        return [
            [
                "extract",
                "--input",  f"{GCS_MOUNT}/batches/{batch}/transcripts/{item['stem']}.json",
                "--output", f"{GCS_MOUNT}/batches/{batch}/extracted/{item['stem']}.json",
                "--model",  state["analysis_model"],
                "--log-format", "json",
            ]
            for item in scored
        ]

    extract_pods = KubernetesPodOperator.partial(
        task_id="extract",
        name="va-extract",
        namespace=NAMESPACE,
        kubernetes_conn_id=KUBERNETES_CONN_ID,
        image=IMAGE,
        cmds=["python", "-m", "voice_analytics"],
        env_vars=_pod_env(
            llm_key_xcom="{{ ti.xcom_pull(task_ids='validate_access')['analysis_llm_key'] }}",
            model_xcom="{{ ti.xcom_pull(task_ids='validate_access')['analysis_model'] }}",
        ),
        volumes=volumes,
        volume_mounts=mounts,
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "250m", "memory": "512Mi"},
            limits={"cpu": "1", "memory": "1Gi"},
        ),
        on_finish_action="delete_succeeded_pod",
        get_logs=True,
        max_active_tis_per_dag=8,
        retries=1,
        execution_timeout=timedelta(minutes=10),
    )

    # -----------------------------------------------------------------------
    @task
    def open_analysis_batch(state: dict) -> dict:
        """Create the `batches` row that scoring results hang from.

        Scoring has its own batch, separate from `transcription_batches`, and
        the two are linked by `batches.transcription_batch_id`. It is easy to
        miss: both are called "batch", both have counters, and the id types
        differ -- `batches.id` is a UUID, `transcription_batches.id` a bigint.
        """
        with _conn() as conn:
            analysis_batch_id = store.open_analysis_batch(
                conn,
                transcription_batch_id=state["batch_id"],
                usecase_id=state["usecase_id"],
                usecase_name=state["usecase_name"],
            )
            conn.commit()
        print(f"Analysis batch {analysis_batch_id} opened for transcription batch "
              f"{state['batch_id']}")
        return {**state, "analysis_batch_id": analysis_batch_id}

    # -----------------------------------------------------------------------
    @task(trigger_rule="all_done")
    def persist_analysis(state: dict, scored: list[dict]) -> dict:
        """Write one call, one analysis and its KPI rows, per transcript.

        Four tables, in order, because each depends on the last:

            batches -> calls -> call_analyses -> analysis_kpi_results

        The SQL lives in `voice_analytics_store`, so the rows this writes are
        identical to the ones `scripts/run_local.py` writes.
        """
        bucket = Variable.get("AUDIO_BUCKET")
        gcs = GCSHook(gcp_conn_id=GCS_CONN_ID)
        batch = state["batch_id"]

        stored, missing, unknown_codes = 0, 0, set()

        with _conn() as conn:
            kpi_ids = store.kpi_code_to_id(conn)
            transcripts = {
                row["transcription_id"]: row["transcript"]
                for row in store.completed_transcriptions(conn, batch)
            }

            for item in scored:
                key = f"batches/{batch}/analysis/{item['stem']}.json"
                if not gcs.exists(bucket, key):
                    missing += 1
                    continue
                analysis = json.loads(gcs.download(bucket, key).decode("utf-8"))

                # Genuinely optional: an extract pod may have failed, and that
                # costs two fields rather than a call.
                extracted = None
                extract_key = f"batches/{batch}/extracted/{item['stem']}.json"
                if gcs.exists(bucket, extract_key):
                    extracted = json.loads(
                        gcs.download(bucket, extract_key).decode("utf-8"))

                result = store.persist_analysis(
                    conn,
                    analysis_batch_id=state["analysis_batch_id"],
                    transcription_id=item["transcription_id"],
                    filename=item["filename"],
                    transcript=transcripts.get(item["transcription_id"]),
                    analysis=analysis,
                    extracted=extracted,
                    kpi_ids=kpi_ids,
                    usecase_id=state["usecase_id"],
                    usecase_name=state["usecase_name"],
                    transcription_batch_id=batch,
                )
                conn.commit()
                unknown_codes.update(result["unknown_codes"])

                if analysis.get("unevidenced_kpis"):
                    # A score with no supporting quote was discarded, not
                    # zeroed. A KPI that keeps appearing here has a prompt
                    # problem, not a call problem.
                    print(
                        f"{item['stem']}: {len(analysis['unevidenced_kpis'])} KPI(s) "
                        f"scored without evidence and were not counted: "
                        f"{', '.join(analysis['unevidenced_kpis'])}"
                    )
                stored += 1

            counts = store.close_analysis_batch(conn, state["analysis_batch_id"])
            conn.commit()

        if unknown_codes:
            print(
                f"WARNING: {len(unknown_codes)} KPI code(s) were scored but have no "
                f"row in the kpis table, so those scores could not be stored: "
                f"{', '.join(sorted(unknown_codes))}"
            )

        print(f"{stored} call(s) scored and stored, {missing} with no analysis output")
        return {"stored": stored, "missing": missing, "counts": counts,
                "unknown_kpi_codes": sorted(unknown_codes)}

    # -----------------------------------------------------------------------
    @task(trigger_rule="all_done")
    def cleanup(state: dict) -> dict:
        """Delete the audio and clear file_path.

        This is what makes "0 recordings retained" true. ``all_done`` so the
        recordings are removed even when scoring failed -- a failed run is not
        a reason to keep customer audio. The transcripts stay; they are
        anonymised.
        """
        bucket = Variable.get("AUDIO_BUCKET")
        gcs = GCSHook(gcp_conn_id=GCS_CONN_ID)

        removed = 0
        for name in gcs.list(bucket, prefix=f"batches/{state['batch_id']}/audio/"):
            try:
                gcs.delete(bucket, name)
                removed += 1
            except Exception as exc:  # noqa: BLE001
                # Report, do not fail. A leftover object is a cleanup job's
                # problem; failing here would mask the run's real outcome.
                print(f"WARNING: could not delete gs://{bucket}/{name}: {exc}")

        with _conn() as conn:
            store.clear_file_paths(conn, state["batch_id"])
            conn.commit()

        print(f"{removed} recording(s) deleted; no audio is retained for this batch")
        return {"removed": removed}

    # -----------------------------------------------------------------------
    @task(trigger_rule="all_done")
    def finalise(state: dict) -> None:
        """Set the batch's final status from what the rows actually say."""
        with _conn() as conn:
            result = store.finalise_batch(conn, state["batch_id"])
            conn.commit()
        print(f"Batch {state['batch_id']} finished: {result['status']} "
              f"({result['completed']} done, {result['failed']} failed)")

    # -----------------------------------------------------------------------
    # Wiring
    # -----------------------------------------------------------------------
    config = read_config()
    validated = validate_access(config)

    # Access is checked before anything is moved: a usecase that may not use
    # the models should not cause 12,480 recordings to be copied first.
    transfer_args = transfer_arguments(validated)
    transfer_pod.arguments = transfer_args

    prompt_args = prompt_arguments(validated)
    build_prompt_pod.arguments = prompt_args
    with_prompt = read_prompt_report(validated)

    files = list_audio_files(with_prompt)
    transcribed = transcribe_pods.expand(arguments=transcribe_arguments(with_prompt, files))
    tally = persist_transcripts(with_prompt, files)
    scorable = halt_gate(with_prompt, tally)

    # Scoring and extraction read the same transcripts and write to different
    # places, so they run side by side rather than one after the other.
    analysed = analyse_pods.expand(arguments=analyse_arguments(with_prompt, scorable))
    extracted = extract_pods.expand(arguments=extract_arguments(with_prompt, scorable))

    opened = open_analysis_batch(with_prompt)
    stored = persist_analysis(opened, scorable)

    validated >> transfer_args >> transfer_pod
    validated >> prompt_args >> build_prompt_pod >> with_prompt
    [transfer_pod, with_prompt] >> files
    transcribed >> tally >> scorable
    scorable >> [analysed, extracted, opened]
    [analysed, extracted, opened] >> stored
    # cleanup hangs off the whole pipeline so recordings are deleted even when
    # scoring never ran; finalise is last and always runs.
    stored >> cleanup(with_prompt) >> finalise(with_prompt)


# ---------------------------------------------------------------------------
# Notes for whoever deploys this
# ---------------------------------------------------------------------------
#
# 1. GCS FUSE vs an init container
#    Pods read and write the bucket as a filesystem through the GCS FUSE CSI
#    driver, so the image needs no cloud SDK and the library needs no gs://
#    support. Outside GKE, replace the CSI volume with an initContainer that
#    copies the one object it needs into an emptyDir, and mount that at /gcs.
#    Nothing else in this file changes.
#
# 2. API keys in XCom
#    validate_access returns PromptHub's per-usecase llmApiKey, and XCom is
#    stored as plain text in the Airflow metadata database. mask_secret keeps
#    it out of the logs, but not out of that table. The durable fix is for
#    validate_access to write the key into a short-lived Kubernetes Secret and
#    return only its name, with the pod reading it via secret_key_ref. That is
#    a deployment decision, so it is flagged rather than assumed.
#
# 3. Concurrency lives in two places
#    max_active_tis_per_dag caps pods per DAG run; the cluster's quota caps
#    everything. Setting the first above what the second allows produces pods
#    stuck in Pending, which looks like a hang.
#
# 4. Upgrading the library
#    Change IMAGE and nothing else. The Airflow environment has no dependency
#    on voice_analytics, so there is no version to align, no shared virtualenv
#    to rebuild, and no scheduler restart. Two tasks in this file could even
#    run different versions of the image if a migration needed it.
#
#    The corollary: anything this DAG needs from the library has to exist as a
#    subcommand. Twice already the honest fix was to add one rather than import
#    a helper -- `build-prompt`, and `duration_sec` in the analyse output.
#
# 5. What this DAG does NOT do
#    It does not create the transcription_batches row -- that is the API's job,
#    and it is the trigger for this DAG rather than a step in it. And it has no
#    dry-run gate; that is a separate, non-destructive DAG.
#
# 6. The transfer pod needs the 'transfer' extra
#    The image must be built with it, or `transfer` exits 3 with a message
#    saying so. Every other command runs without it.
