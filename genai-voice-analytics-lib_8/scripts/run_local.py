#!/usr/bin/env python3
"""Run the whole voice analytics pipeline on a laptop, without Airflow.

The reference DAG needs Kubernetes, GCS and Postgres. None of that is needed to
see the pipeline work: every stage is a command, and this script runs those
commands over a folder of audio files the way the DAG runs them over pods.

    python scripts/run_local.py --input-dir ./calls --output-dir ./out

Each file goes through transcribe (anonymising, then translating), then scoring
and extraction, with the same ordering and the same concurrency limit the DAG
uses. Results are written as JSON next to each other:

    out/transcripts/call_0001.json
    out/analysis/call_0001.json
    out/extracted/call_0001.json
    out/summary.json

Deliberately a **subprocess** runner rather than a set of library calls. Pods
invoke the CLI and are judged on their exit code, so running it the same way
here exercises the same contract -- including the exit codes, which is what
makes a local run a rehearsal rather than an approximation.

By default nothing is written to a database -- the JSON files are the result.
Pass ``--postgres`` with a batch id to also write the rows the DAG writes,
through the same `voice_analytics_store` module the DAG uses, so a local run is
a genuine rehearsal rather than an approximation::

    createdb voice_analytics_local
    psql voice_analytics_local -f docs/schema/bootstrap.sql
    export VOICE_ANALYTICS_DSN=postgresql://localhost/voice_analytics_local

    python scripts/run_local.py --input-dir ./calls --prompt kpis.txt \
        --postgres --batch-id 1

The batch row and its KPI configuration must already exist -- this script runs
the pipeline, it does not create the configuration the pipeline reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".flac", ".webm", ".aac"}

#: Exit codes the CLI uses. Mirrored here so the report can explain a failure
#: instead of printing a number. Source: src/voice_analytics/cli/exit_codes.py
EXIT_MEANING = {
    0: "success",
    1: "unexpected error",
    2: "the command line was wrong",
    3: "configuration is missing or wrong -- check your environment variables",
    4: "the input file could not be read",
    5: "the AI service rejected the credentials",
    6: "the AI service is rate limiting",
    7: "the AI service is unreachable",
    8: "the AI service's reply could not be used",
    9: "personal information could not be removed, so nothing was saved",
    130: "interrupted",
}


def log(message: str) -> None:
    print(message, flush=True)


def _configured(name: str, env_file: str = ".env") -> str | None:
    """A setting's value, from the environment or a ``.env`` file beside us.

    Mirrors what `voice_analytics.config.load_settings` does, so this script
    accepts exactly the configurations the subcommands it runs will accept.
    """
    value = os.environ.get(name)
    if value:
        return value
    if os.path.isfile(env_file):
        try:
            from dotenv import dotenv_values
        except ImportError:  # pragma: no cover
            return None
        return dotenv_values(env_file).get(name)
    return None


async def run_command(args: list[str], quiet: bool) -> tuple[int, str]:
    """Run one CLI command, returning its exit code and stderr."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "voice_analytics", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    text = stderr.decode("utf-8", "replace")
    if not quiet and text:
        for line in text.splitlines():
            print(f"    {line}", flush=True)
    return process.returncode or 0, text


async def process_one(
    audio: Path, out: Path, args: argparse.Namespace, limiter: asyncio.Semaphore
) -> dict:
    """Take one recording all the way through. Never raises."""
    stem = audio.stem
    record: dict = {"file": audio.name, "stages": {}}

    async with limiter:
        started = time.perf_counter()
        log(f"\n[{audio.name}] starting")

        # --- transcribe -----------------------------------------------------
        transcript_path = out / "transcripts" / f"{stem}.json"
        command = [
            "transcribe",
            "--input", str(audio),
            "--output", str(transcript_path),
        ]
        if not args.no_anonymize:
            # Forces the ordering the pipeline exists for: transcribe, then
            # anonymise, then translate -- so no personal information reaches
            # the translation model.
            command.append("--anonymize")
        if args.target_lang:
            command += ["--target-lang", args.target_lang]
        if args.romanize:
            command.append("--romanize")
        if args.transcription_model:
            command += ["--model", args.transcription_model]

        code, _ = await run_command(command, args.quiet)
        record["stages"]["transcribe"] = code
        if code != 0:
            record["failed_at"] = "transcribe"
            record["reason"] = EXIT_MEANING.get(code, f"exit code {code}")
            log(f"[{audio.name}] FAILED at transcribe: {record['reason']}")
            return record

        if args.transcribe_only:
            record["elapsed_s"] = round(time.perf_counter() - started, 1)
            log(f"[{audio.name}] transcribed in {record['elapsed_s']}s")
            return record

        # --- score, and extract, together -----------------------------------
        # Neither reads the other's output, so they run side by side, exactly
        # as the DAG runs them.
        tasks = {
            "analyse": run_command([
                "analyse",
                "--input", str(transcript_path),
                "--prompt", str(args.prompt),
                "--output", str(out / "analysis" / f"{stem}.json"),
                *(["--model", args.analysis_model] if args.analysis_model else []),
            ], args.quiet),
        }
        if not args.no_extract:
            tasks["extract"] = run_command([
                "extract",
                "--input", str(transcript_path),
                "--output", str(out / "extracted" / f"{stem}.json"),
                *(["--model", args.analysis_model] if args.analysis_model else []),
            ], args.quiet)

        results = await asyncio.gather(*tasks.values())
        for name, (code, _) in zip(tasks, results):
            record["stages"][name] = code

        if record["stages"]["analyse"] != 0:
            code = record["stages"]["analyse"]
            record["failed_at"] = "analyse"
            record["reason"] = EXIT_MEANING.get(code, f"exit code {code}")
            log(f"[{audio.name}] FAILED at analyse: {record['reason']}")
            return record

        # An extraction failure is not a file failure: the originating service
        # left the field empty and carried on, and so does this.
        if record["stages"].get("extract", 0) != 0:
            log(f"[{audio.name}] topics and agent name unavailable; continuing")

        record["elapsed_s"] = round(time.perf_counter() - started, 1)
        log(f"[{audio.name}] done in {record['elapsed_s']}s")
        return record


def summarise(record: dict, out: Path) -> dict:
    """Pull the headline numbers out of one file's results, for the report."""
    stem = Path(record["file"]).stem
    detail: dict = {}

    transcript_path = out / "transcripts" / f"{stem}.json"
    if transcript_path.is_file():
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        detail["language"] = transcript.get("primary_language")
        detail["transcript_chars"] = len(transcript.get("transcript") or "")
        detail["translated_chars"] = len(transcript.get("translated_transcript") or "")

    analysis_path = out / "analysis" / f"{stem}.json"
    if analysis_path.is_file():
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        detail["duration_sec"] = analysis.get("duration_sec")
        detail["kpis_scored"] = sum(
            1 for k in analysis.get("kpis") or []
            if k.get("score") is not None and k.get("applicable", True)
        )
        detail["kpis_total"] = len(analysis.get("kpis") or [])
        detail["unevidenced"] = analysis.get("unevidenced_kpis") or []
        detail["by_objective"] = {
            row["objective"]: f"{row['scored']}/{row['total']}"
            for row in analysis.get("by_objective") or []
        }
        impact = analysis.get("call_impact") or {}
        detail["call_impact"] = impact.get("level")

    extracted_path = out / "extracted" / f"{stem}.json"
    if extracted_path.is_file():
        extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
        detail["topics"] = extracted.get("topics")
        detail["agent_name"] = extracted.get("agent_name")

    return detail


def _open_store(args: argparse.Namespace):
    """Connection plus batch context, or ``None`` when --postgres was not given."""
    if not args.postgres:
        return None

    import voice_analytics_store as store

    if args.batch_id is None:
        raise SystemExit("--postgres needs --batch-id: which transcription_batches row to write to")

    # open_connection, not connect(): this connection outlives the call,
    # and the caller closes it in its own finally.
    conn = store.open_connection(args.dsn)
    config = store.read_batch_config(conn, args.batch_id)
    kpi_codes = store.selected_kpi_codes(conn, args.batch_id)
    if not kpi_codes and not args.transcribe_only:
        raise SystemExit(
            f"Batch {args.batch_id} has no KPIs selected. Check batch_kpi_configs "
            "and usecase_kpi_selections, or pass --transcribe-only."
        )
    store.mark_batch_processing(conn, args.batch_id)
    conn.commit()
    log(f"Writing to Postgres: batch {args.batch_id} ({config['usecase_name']})"
        + (", transcription only." if args.transcribe_only
           else f", {len(kpi_codes)} KPI(s) selected."))
    return {"store": store, "conn": conn, "config": config, "kpi_codes": kpi_codes}


def _persist(db, records: list[dict], out: Path, args: argparse.Namespace) -> None:
    """Write every result to Postgres, exactly as the DAG's tasks would."""
    store, conn = db["store"], db["conn"]
    batch_id = args.batch_id

    seeded = store.seed_transcriptions(conn, batch_id, [
        {"name": r["file"], "file_path": str(Path(args.input_dir) / r["file"])}
        for r in records
    ])
    conn.commit()

    by_name = {s["name"]: s for s in seeded}
    for record in records:
        row = by_name[record["file"]]
        stem = Path(record["file"]).stem
        path = out / "transcripts" / f"{stem}.json"
        if "failed_at" in record or not path.is_file():
            store.mark_transcription_failed(
                conn, row["transcription_id"],
                record.get("reason", "no transcript produced"))
        else:
            store.persist_transcript(
                conn, row["transcription_id"],
                json.loads(path.read_text(encoding="utf-8")))
        conn.commit()

    tally = store.transcription_tally(conn, batch_id)
    conn.commit()
    log(f"  transcriptions: {tally['completed']} completed, {tally['failed']} failed")

    if args.transcribe_only:
        final = store.finalise_batch(conn, batch_id)
        conn.commit()
        log(f"  batch {batch_id} is now '{final['status']}' (transcription only)")
        return

    analysis_batch_id = store.open_analysis_batch(
        conn, batch_id, db["config"]["usecase_id"], db["config"]["usecase_name"])
    conn.commit()

    kpi_ids = store.kpi_code_to_id(conn)
    transcripts = {
        r["transcription_id"]: r["transcript"]
        for r in store.completed_transcriptions(conn, batch_id)
    }

    stored, unknown = 0, set()
    for record in records:
        if "failed_at" in record:
            continue
        stem = Path(record["file"]).stem
        analysis_path = out / "analysis" / f"{stem}.json"
        if not analysis_path.is_file():
            continue
        extracted_path = out / "extracted" / f"{stem}.json"
        row = by_name[record["file"]]

        result = store.persist_analysis(
            conn,
            analysis_batch_id=analysis_batch_id,
            transcription_id=row["transcription_id"],
            filename=record["file"],
            transcript=transcripts.get(row["transcription_id"]),
            analysis=json.loads(analysis_path.read_text(encoding="utf-8")),
            extracted=(
                json.loads(extracted_path.read_text(encoding="utf-8"))
                if extracted_path.is_file() else None
            ),
            kpi_ids=kpi_ids,
            usecase_id=db["config"]["usecase_id"],
            usecase_name=db["config"]["usecase_name"],
            transcription_batch_id=batch_id,
        )
        conn.commit()
        unknown.update(result["unknown_codes"])
        stored += 1

    counts = store.close_analysis_batch(conn, analysis_batch_id)
    final = store.finalise_batch(conn, batch_id)
    conn.commit()

    if unknown:
        log(f"  WARNING: {len(unknown)} KPI code(s) have no row in the kpis table, "
            f"so those scores were not stored: {', '.join(sorted(unknown))}")
    log(f"  calls: {stored} scored ({counts['completed']} analyses)")
    log(f"  batch {batch_id} is now '{final['status']}' "
        f"(analysis batch {analysis_batch_id})")


async def main_async(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir)
    out = Path(args.output_dir)

    if not input_dir.is_dir():
        log(f"ERROR: --input-dir is not a directory: {input_dir}")
        return 2
    if not args.transcribe_only and not Path(args.prompt).is_file():
        log(
            f"ERROR: --prompt file not found: {args.prompt}\n"
            "Build one with:  python -m voice_analytics build-prompt "
            "--kpi-codes a,b,c --output kpis.txt\n"
            "Or write a plain-text prompt by hand -- any file works."
        )
        return 2
    # The subcommands read a .env file as well as the real environment, so
    # this check has to look in both -- otherwise a perfectly good .env setup
    # is rejected here by a script that would have worked.
    if not _configured("LLM_BASE_URL"):
        log(
            "ERROR: LLM_BASE_URL is not set, in the environment or in a .env "
            "file in this directory. Copy env.sample to .env and fill it in, "
            "or export it."
        )
        return 3

    audio_files = sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    )
    if args.limit:
        audio_files = audio_files[: args.limit]
    if not audio_files:
        log(f"ERROR: no audio files in {input_dir} "
            f"(looking for {', '.join(sorted(AUDIO_SUFFIXES))})")
        return 4

    for sub in ("transcripts", "analysis", "extracted"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    log(f"{len(audio_files)} recording(s) to process, "
        f"{args.concurrency} at a time.")

    db = _open_store(args)

    started = time.perf_counter()
    limiter = asyncio.Semaphore(args.concurrency)
    records = await asyncio.gather(
        *(process_one(p, out, args, limiter) for p in audio_files)
    )

    completed = [r for r in records if "failed_at" not in r]
    failed = [r for r in records if "failed_at" in r]
    elapsed = round(time.perf_counter() - started, 1)

    report = {
        "total": len(records),
        "completed": len(completed),
        "failed": len(failed),
        "elapsed_s": elapsed,
        "files": {r["file"]: summarise(r, out) for r in completed},
        "failures": {r["file"]: r["reason"] for r in failed},
    }
    (out / "summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    log("\n" + "=" * 62)
    log(f"{len(completed)} of {len(records)} recording(s) processed in {elapsed}s")
    for name, detail in report["files"].items():
        bits = []
        if detail.get("transcript_chars"):
            bits.append(f"{detail['transcript_chars']} chars"
                        + (f" {detail['language']}" if detail.get("language") else ""))
        if detail.get("translated_chars"):
            bits.append(f"translated {detail['translated_chars']}")
        if detail.get("kpis_total"):
            bits.append(f"{detail['kpis_scored']}/{detail['kpis_total']} KPIs")
        if detail.get("topics"):
            bits.append("about " + ", ".join(detail["topics"]))
        if detail.get("agent_name"):
            bits.append(f"agent {detail['agent_name']}")
        if detail.get("unevidenced"):
            bits.append(f"{len(detail['unevidenced'])} unevidenced")
        log(f"  {name}: {'; '.join(bits) if bits else 'no results'}")
    for name, reason in report["failures"].items():
        log(f"  {name}: FAILED -- {reason}")
    log(f"\nFull results in {out}/  (summary.json has everything)")

    if db is not None:
        log("\nWriting to Postgres...")
        try:
            _persist(db, list(records), out, args)
        finally:
            db["conn"].close()

    # Same rule the DAG's halt_gate applies: too many failures means the run
    # failed, rather than quietly reporting on whatever survived.
    if failed and len(failed) * 100 / len(records) > args.halt_above_pct:
        log(f"\n{len(failed)} of {len(records)} failed, above the "
            f"{args.halt_above_pct}% limit.")
        return 8
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_local.py",
        description=(
            "Run the voice analytics pipeline over a folder of recordings, "
            "without Airflow, Kubernetes, GCS or a database."
        ),
    )
    parser.add_argument("--input-dir", required=True, help="Folder of audio files.")
    parser.add_argument("--output-dir", default="./out", help="Where results go.")
    parser.add_argument(
        "--prompt", default="kpis.txt",
        help="The scoring prompt. Build one with 'build-prompt', or write one by hand.",
    )
    parser.add_argument("--target-lang", default=None, help="Translate into this language.")
    parser.add_argument("--romanize", action="store_true", help="Ask for Latin script.")
    parser.add_argument("--transcription-model", default=None)
    parser.add_argument("--analysis-model", default=None)
    parser.add_argument(
        "--concurrency", type=int, default=4,
        help="Recordings in flight at once. The DAG's equivalent is 8.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after N files.")
    parser.add_argument(
        "--no-anonymize", action="store_true",
        help=(
            "Skip PII removal. For local testing against sample audio only -- "
            "the pipeline anonymises before anything is stored or translated."
        ),
    )
    parser.add_argument("--no-extract", action="store_true",
                        help="Skip topics and agent name.")
    parser.add_argument(
        "--transcribe-only", action="store_true",
        help=(
            "Stop after transcription: no scoring, no extraction, and no "
            "--prompt needed. With --postgres this writes `transcriptions` "
            "rows and leaves `calls` and `call_analyses` alone -- the same "
            "half of the pipeline the transcribe-only DAG runs, so a batch "
            "with no KPI configuration at all is fine."
        ),
    )
    parser.add_argument("--halt-above-pct", type=int, default=10,
                        help="Exit non-zero if more than this %% of files fail.")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Hide each command's own logs.")
    parser.add_argument(
        "--postgres", action="store_true",
        help=(
            "Also write the rows the DAG writes, through the same "
            "voice_analytics_store module. Needs the 'store' extra and "
            "--batch-id."
        ),
    )
    parser.add_argument(
        "--batch-id", type=int, default=None,
        help="Which transcription_batches row to write to. Required by --postgres.",
    )
    parser.add_argument(
        "--dsn", default=None,
        help="Postgres connection string. Read from VOICE_ANALYTICS_DSN when omitted.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        log("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
