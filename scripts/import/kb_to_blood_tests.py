#!/usr/bin/env python3
"""
Sync FamilyHealth knowledge_base.json → Postgres blood_tests.

Source: ~/FamilyHealth/<user>/knowledge_base.json (Google Drive, local Mac)
Target: healthvault_postgres `blood_tests` table on Hetzner

Merges three KB sections — blood_tests, hormones, vitamins — into the
single Postgres blood_tests table. Each row keyed by (user_id, test_date,
test_type, file_path) for idempotent upsert.

Why: agent_tools_api.get_recent_biomarkers reads from this table; KB
itself isn't accessible from Hetzner. This script bridges that gap.

Usage:
    python3 scripts/import/kb_to_blood_tests.py --user-id <telegram_id> --folder "<Имя> — Здоровье"
    python3 scripts/import/kb_to_blood_tests.py --all  # sync all FamilyHealth users with users.kb_folder set

Idempotent — safe to run repeatedly. New analyses are added, existing ones
are updated if values changed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.health.kb_schema import looks_like_us_units  # noqa: E402

# SSH-tunnel out, write directly into Postgres via subprocess psql. Keeps deps
# minimal — no psycopg2 needed locally. The script runs on Mac, ships rows to
# Hetzner over SSH by key (same transport as sync_family_kb.py).

FAMILYHEALTH_BASE = Path(
    os.path.expanduser("~/Library/CloudStorage/GoogleDrive-lyskovsky@gmail.com/Мой диск/FamilyHealth")
)
SERVER = "root@116.203.213.137"
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("kb_sync")


# ---------------------------------------------------------------------------
# KB parsing
# ---------------------------------------------------------------------------


def _load_kb(folder_name: str) -> dict:
    path = FAMILYHEALTH_BASE / folder_name / "knowledge_base.json"
    if not path.exists():
        raise FileNotFoundError(f"KB not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _extract_rows(kb: dict, user_id: int) -> Iterable[dict]:
    """Pull rows from blood_tests / hormones / vitamins sections.

    Each row is shaped for Postgres blood_tests:
        user_id, test_date, test_type, values (jsonb), file_path, status
    """
    for kb_section, default_type in (
        ("blood_tests", "blood"),
        ("biochemistry", "biochemistry"),
        ("hormones", "hormones"),
        ("vitamins", "vitamins"),
    ):
        for entry in kb.get(kb_section, []):
            test_date = entry.get("date")
            if not test_date:
                log.warning("skip — no date in %s entry: %s", kb_section, entry.get("file"))
                continue

            # Prefer the more specific analysis_type over generic 'type'.
            # Some KBs (e.g. Andrey's) use 'subtype' (biochemistry/cbc/coagulation).
            test_type = entry.get("analysis_type") or entry.get("type") or entry.get("subtype") or default_type
            # Some hormone entries use type="DHT" — keep as-is

            # Canonical schema: entry["values"] = {marker_key: value, ...}
            # Legacy "markers" was migrated to "values" 2026-05-18.
            # See docs/operations/kb-schema.md. If "markers" reappears in a KB,
            # it's a regression — fix the KB instead of reintroducing fallback.
            if "markers" in entry and "values" not in entry:
                raise ValueError(
                    f"Legacy 'markers' key found in {kb_section} entry "
                    f"(date={test_date}, file={entry.get('file')}). "
                    f"Rename to 'values' — see docs/operations/kb-schema.md."
                )
            values_dict = entry.get("values") or {}

            # US-панели (g/dL·mg/dL, напр. Maccabi) несут признак единиц в JSONB,
            # чтобы to_canonical сконвертировал в метрику на чтении (issue #95).
            # Часть maccabi-панелей задаёт единицы инлайновыми _ref-диапазонами, а поля
            # units не имеет — детектим их по величине маркера-индикатора (issue #295).
            # Метрические записи остаются как есть.
            units = (entry.get("units") or "").lower()
            explicit_us = "mg/dl" in units or "g/dl" in units or "us" in units
            if values_dict and (explicit_us or looks_like_us_units(values_dict)):
                values_dict = {**values_dict, "_unit_system": "US"}

            yield {
                "user_id": user_id,
                "test_date": test_date,
                "test_type": test_type[:100],  # column is VARCHAR(100)
                "values": values_dict,
                "file_path": entry.get("file") or entry.get("source_text_file"),
                "status": (entry.get("status") or "current")[:50],
                "note": entry.get("note"),
                "laboratory": entry.get("laboratory") or entry.get("source") or entry.get("lab"),
            }


# ---------------------------------------------------------------------------
# Postgres upsert
# ---------------------------------------------------------------------------


SQL_UPSERT = """
INSERT INTO blood_tests (user_id, test_date, test_type, values, file_path, status, created_at)
VALUES (%(user_id)s, %(test_date)s::date, %(test_type)s, %(values)s::jsonb, %(file_path)s, %(status)s, NOW())
ON CONFLICT (user_id, test_date, test_type) DO UPDATE SET
    values    = EXCLUDED.values,
    file_path = EXCLUDED.file_path,
    status    = EXCLUDED.status
RETURNING id, (xmax = 0) AS inserted;
"""

# blood_tests doesn't have a unique constraint by default — we add one.
SQL_ENSURE_UNIQUE = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'blood_tests'
          AND indexname = 'blood_tests_user_date_type_unique'
    ) THEN
        -- Two rows can share (user, date) if they're different analyses
        -- (e.g. CBC + lipid panel both on same day). Type disambiguates.
        CREATE UNIQUE INDEX blood_tests_user_date_type_unique
        ON blood_tests (user_id, test_date, test_type);
    END IF;
END $$;
"""


def _psql_exec(sql: str, *, capture: bool = False) -> str | None:
    """Run SQL on Hetzner postgres via ssh (key auth) + docker exec.

    Returns stdout if capture=True, else None.
    """
    import subprocess

    cmd = [
        "ssh",
        *SSH_OPTS,
        SERVER,
        f"docker exec -i healthvault_postgres psql -U healthvault -d healthvault -v ON_ERROR_STOP=1 {'-t -A ' if capture else ''}-c {json.dumps(sql)}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr}")
    return result.stdout if capture else None


_REMOTE_SCRIPT = '''
import json, os, sys
import psycopg2
from urllib.parse import urlparse

rows = json.loads(sys.stdin.read())
p = urlparse(os.environ["DATABASE_URL"])
conn = psycopg2.connect(host=p.hostname, port=p.port or 5432, dbname=p.path[1:], user=p.username, password=p.password)
cur = conn.cursor()
cur.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS blood_tests_user_date_type_unique "
    "ON blood_tests (user_id, test_date, test_type)"
)
inserted = updated = 0
for r in rows:
    cur.execute("""
        INSERT INTO blood_tests (user_id, test_date, test_type, values, file_path, status, created_at)
        VALUES (%(user_id)s, %(test_date)s::date, %(test_type)s, %(values)s::jsonb, %(file_path)s, %(status)s, NOW())
        ON CONFLICT (user_id, test_date, test_type) DO UPDATE SET
            values    = EXCLUDED.values,
            file_path = EXCLUDED.file_path,
            status    = EXCLUDED.status
        RETURNING (xmax = 0) AS was_inserted
    """, {
        "user_id": r["user_id"],
        "test_date": r["test_date"],
        "test_type": r["test_type"],
        "values": json.dumps(r["values"]),
        "file_path": r.get("file_path"),
        "status": r.get("status") or "current",
    })
    if cur.fetchone()[0]:
        inserted += 1
    else:
        updated += 1
conn.commit()
print(json.dumps({"inserted": inserted, "updated": updated}))
'''


def _psql_copy_via_python(rows: list[dict]) -> tuple[int, int]:
    """Run the remote upsert script on healthvault_bot, fed via stdin.

    Avoids inline `python3 -c "..."` shell-escaping hell by scp'ing the
    script to /tmp and running by filepath instead.
    """
    import subprocess
    import tempfile

    payload = json.dumps(rows, default=str, ensure_ascii=False)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(_REMOTE_SCRIPT)
        local_script = f.name

    base_ssh = ["ssh", *SSH_OPTS]
    base_scp = ["scp", *SSH_OPTS]

    try:
        subprocess.run(base_scp + [local_script, f"{SERVER}:/tmp/kb_upsert.py"], check=True, capture_output=True)
        subprocess.run(
            base_ssh + [SERVER, "docker cp /tmp/kb_upsert.py healthvault_bot:/tmp/kb_upsert.py"],
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            base_ssh + [SERVER, "docker exec -i healthvault_bot python3 /tmp/kb_upsert.py"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"sync failed: {result.stderr.strip()}")
        last = [line for line in result.stdout.strip().split("\n") if line.strip()][-1]
        counts = json.loads(last)
        return counts["inserted"], counts["updated"]
    finally:
        os.unlink(local_script)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", type=int, required=True, help="Postgres users.telegram_id")
    p.add_argument(
        "--folder",
        required=True,
        help="FamilyHealth folder name (e.g. 'Александр Лысковский — Здоровье')",
    )
    p.add_argument("--dry-run", action="store_true", help="Parse only, don't write to DB")
    args = p.parse_args(argv)

    kb = _load_kb(args.folder)
    rows = list(_extract_rows(kb, args.user_id))
    log.info(f"📋 Parsed {len(rows)} rows from {args.folder}/knowledge_base.json")
    log.info(
        "    blood_tests: %d  hormones: %d  vitamins: %d",
        len(kb.get("blood_tests", [])),
        len(kb.get("hormones", [])),
        len(kb.get("vitamins", [])),
    )

    if args.dry_run:
        log.info("🟡 dry-run — not writing")
        return 0

    inserted, updated = _psql_copy_via_python(rows)
    log.info(f"✅ Synced: {inserted} new, {updated} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
