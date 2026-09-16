#!/usr/bin/env python3
"""Create a batch to run against, in a local throwaway database.

The pipeline reads its configuration; it does not create it. In production the
API and UI write that configuration, so a local run needs something to stand in
for them. This is that stand-in.

    createdb voice_analytics_local
    psql voice_analytics_local -f docs/schema/bootstrap.sql
    export VOICE_ANALYTICS_DSN=postgresql://localhost/voice_analytics_local

    python scripts/seed_local_batch.py
    python scripts/run_local.py --input-dir ./calls --prompt kpis.txt \
        --postgres --batch-id <the id it prints>

It builds the whole chain the pipeline resolves through::

    kpi_sections -> kpis -> usecase_kpi_configs -> usecase_kpi_selections
                 -> batch_kpi_configs -> transcription_batches

**Never run this against a shared or production database.** It inserts test
configuration, and `--reset` deletes every row in all nine tables. It refuses
to run unless the connection string names a database that looks local.
"""

from __future__ import annotations

import argparse
import json
import sys

#: KPI codes the bundled fake gateway scores, so a seeded batch and
#: `scripts/fake_gateway.py` line up out of the box.
DEFAULT_KPIS = [
    ("Compliance", "rpc_verified", "RPC Verification"),
    ("Compliance", "recording_consent", "Recording Consent"),
    ("Empathy and tone", "polite_greeting", "Polite Greeting"),
    ("Empathy and tone", "no_evidence_demo", "Unevidenced Demo"),
]

#: A connection string must contain one of these to be treated as local. Not a
#: security control -- a guard against the obvious accident of pointing a
#: seeding script at the dev server while a DBeaver tab is open on it.
LOCAL_HINTS = ("localhost", "127.0.0.1", "/tmp/", "/private/tmp/", "@/", "host=/")


def looks_local(dsn: str) -> bool:
    return any(hint in dsn for hint in LOCAL_HINTS)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="seed_local_batch.py",
        description="Create a transcription batch and its KPI configuration, locally.",
    )
    parser.add_argument("--dsn", default=None,
                        help="Connection string. Read from VOICE_ANALYTICS_DSN when omitted.")
    parser.add_argument(
        "--usecase-id", default="1149",
        help=(
            "Stored on the batch. The default is taken from one sample row in "
            "the schema export (batch 196, 'Batch to Test NEW KPI') and is a "
            "placeholder, not a canonical value. Nothing local validates it -- "
            "but it IS the id PromptHub resolves prompts and keys against, so "
            "use your real one before going near the gateway."
        ),
    )
    parser.add_argument("--usecase-name", default="Voice-Analytics",
                        help="Stored on the batch; informational only.")
    parser.add_argument("--batch-name", default="local test batch")
    parser.add_argument("--target-lang", default="English",
                        help="Stored in the batch metadata, where the pipeline reads it.")
    parser.add_argument("--romanize", action="store_true")
    parser.add_argument(
        "--kpi-codes", default=None,
        help=(
            "Comma-separated KPI codes to configure. Defaults to the four the "
            "bundled fake gateway scores, so a local run works end to end."
        ),
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Delete every row in all nine tables first. Local databases only.",
    )
    parser.add_argument(
        "--i-know-this-is-not-local", action="store_true",
        help="Override the local-database check. Do not use on a shared database.",
    )
    args = parser.parse_args()

    from voice_analytics_store import open_connection
    from voice_analytics_store.connection import dsn_from_env

    dsn = args.dsn or dsn_from_env()
    if not looks_local(dsn) and not args.i_know_this_is_not_local:
        print(
            f"Refusing to seed: {dsn.split('@')[-1]} does not look like a local\n"
            "database. This inserts test configuration, and --reset deletes every\n"
            "row. If you are certain, pass --i-know-this-is-not-local.",
            file=sys.stderr,
        )
        return 2

    if args.kpi_codes:
        codes = [c.strip() for c in args.kpi_codes.split(",") if c.strip()]
        kpis = [("Compliance", code, code.replace("_", " ").title()) for code in codes]
    else:
        kpis = DEFAULT_KPIS

    conn = open_connection(dsn)
    try:
        with conn.cursor() as cur:
            if args.reset:
                cur.execute(
                    "TRUNCATE analysis_kpi_results, call_analyses, calls, batches, "
                    "transcriptions, batch_kpi_configs, usecase_kpi_selections, "
                    "usecase_kpi_configs, kpis, kpi_sections, transcription_batches "
                    "RESTART IDENTITY CASCADE"
                )
                print("All nine tables emptied.")

            section_ids: dict[str, str] = {}
            kpi_ids: list[str] = []
            for order, (section, code, name) in enumerate(kpis):
                if section not in section_ids:
                    cur.execute(
                        "INSERT INTO kpi_sections (name, display_order) VALUES (%s, %s) "
                        "RETURNING id",
                        (section, len(section_ids)),
                    )
                    section_ids[section] = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO kpis (section_id, name, kpi_code, display_order) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (section_ids[section], name, code, order),
                )
                kpi_ids.append(cur.fetchone()[0])

            # A version, because usecase_kpi_configs is versioned and the batch
            # pins one. The next seed makes version 2, leaving this batch's
            # scoring unchanged -- which is the point of the pinning.
            cur.execute(
                "SELECT coalesce(max(version), 0) + 1 FROM usecase_kpi_configs "
                "WHERE usecase_id = %s",
                (args.usecase_id,),
            )
            version = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO usecase_kpi_configs (usecase_id, usecase_name, version, "
                "is_active) VALUES (%s, %s, %s, true) RETURNING id",
                (args.usecase_id, args.usecase_name, version),
            )
            config_id = cur.fetchone()[0]

            for kpi_id in kpi_ids:
                cur.execute(
                    "INSERT INTO usecase_kpi_selections (config_id, kpi_id, is_selected) "
                    "VALUES (%s, %s, true)",
                    (config_id, kpi_id),
                )

            cur.execute(
                """
                INSERT INTO transcription_batches
                       (batch_name, usecase_id, usecase_name, metadata, status,
                        schedule_type, source)
                VALUES (%s, %s, %s, %s::jsonb, 'pending', 'run_now', 'audio_upload')
                RETURNING id
                """,
                (
                    args.batch_name, args.usecase_id, args.usecase_name,
                    json.dumps({
                        "target_language": args.target_lang,
                        "romanize": args.romanize,
                    }),
                ),
            )
            batch_id = int(cur.fetchone()[0])

            cur.execute(
                "INSERT INTO batch_kpi_configs (batch_id, usecase_kpi_config_id) "
                "VALUES (%s, %s)",
                (batch_id, config_id),
            )
        conn.commit()
    finally:
        conn.close()

    print(
        f"\nBatch {batch_id} created.\n"
        f"  use case      {args.usecase_id} ({args.usecase_name}), config version {version}\n"
        f"  KPIs          {', '.join(code for _, code, _ in kpis)}\n"
        f"  target lang   {args.target_lang}\n"
        f"\nRun it:\n"
        f"  python scripts/run_local.py --input-dir ./calls --prompt kpis.txt \\\n"
        f"      --postgres --batch-id {batch_id}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
