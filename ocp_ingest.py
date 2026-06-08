"""
ocp_ingest.py — Bulk import PA23 notices from the OCP Data Registry.

Background
----------
The FTS OCDS API (ocdsReleasePackages) uses an `updatedFrom`/`updatedTo` filter
that operates on an internal `updatedAt` index. PA23 notices published before
approximately April 2026 were NOT indexed in this filter, so standard delta pulls
missed an estimated 14 months of PA23 Contract Details notices.

The Open Contracting Partnership (OCP) Data Registry independently collects FTS
data via a separate pipeline:
    https://data.open-contracting.org/en/publication/41

Their annual JSONL.GZ downloads contain compiled OCDS records (tag='compiled')
covering ALL FTS notices, including the PA23 ones absent from the date-filtered API.
This script downloads those files and imports any missing PA23 notices.

How it works
------------
1. Downloads the OCP annual JSONL file for each requested year.
2. Filters for PA23 records (legalBasis.id starts with '2023/54').
3. Extracts the original FTS notice ID from contracts[0].documents[0].id.
4. Inserts into raw_releases (regime='PA23_FTS') — skipping any already stored.
5. Runs the extractor on newly inserted releases to populate the notices table.

The compiled release format has the same OCDS field structure as individual FTS
releases, so the existing extractor handles them without changes beyond accepting
the 'compiled' tag (updated in db/extractor.py).

Usage
-----
    python ocp_ingest.py              # Import current year + previous year
    python ocp_ingest.py --sync       # Import current year only (used in delta sync)
    python ocp_ingest.py --year 2025  # Import a specific year
    python ocp_ingest.py --no-extract # Skip extractor step (raw import only)
"""

import argparse
import gzip
import json
import sys
from pathlib import Path

import requests
import duckdb

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from db.schema import init_db
from db.extractor import ensure_date_signed_column, extract_all

DB_PATH   = str(ROOT / "data.duckdb")
OCP_BASE  = "https://data.open-contracting.org/en/publication/41/download"
REGIME    = "PA23_FTS"
BATCH     = 500


def _extract_fts_id(record: dict) -> str | None:
    """
    Extract the FTS notice ID (e.g. '005962-2026') from contracts[].documents.

    PA23 UK7 compiled records embed the original FTS notice ID in:
        contracts[0].documents[0].id  (where documentType == 'contractNotice')

    Returns None if the ID cannot be found (e.g. planning/tender-stage records).
    """
    for c in record.get("contracts", []):
        for doc in c.get("documents", []):
            did = doc.get("id", "")
            if did and "-" in did:
                parts = did.split("-")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    return did
    return None


def ingest_year(year: int, conn: duckdb.DuckDBPyConnection) -> int:
    """Download and import PA23 contract details notices for a given year."""
    url = f"{OCP_BASE}?name={year}.jsonl.gz"
    print(f"\n[OCP] Downloading {year} data (~{55 if year == 2025 else 36}MB)...")

    try:
        resp = requests.get(url, timeout=600)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[OCP] Download failed: {e}")
        return 0

    try:
        data  = gzip.decompress(resp.content)
        lines = [l for l in data.decode("utf-8").split("\n") if l.strip()]
    except Exception as e:
        print(f"[OCP] Decompression failed: {e}")
        return 0

    print(f"[OCP] {len(lines):,} records in {year} dataset. Filtering for PA23 contract details...")

    inserted          = 0
    skipped_existing  = 0
    skipped_no_id     = 0
    skipped_not_pa23  = 0
    errors            = 0

    for i, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            errors += 1
            continue

        # Filter: PA23 legal basis only
        lb_id = record.get("tender", {}).get("legalBasis", {}).get("id", "")
        if not lb_id.startswith("2023/54"):
            skipped_not_pa23 += 1
            continue

        # Filter: must have a recoverable FTS notice ID in contracts.documents
        fts_id = _extract_fts_id(record)
        if not fts_id:
            skipped_no_id += 1
            continue

        # Skip if already stored (idempotent)
        existing = conn.execute(
            "SELECT 1 FROM raw_releases WHERE regime = ? AND release_id = ?",
            [REGIME, fts_id],
        ).fetchone()
        if existing:
            skipped_existing += 1
            continue

        # Insert the compiled record into raw_releases
        ocid         = record.get("ocid", "")
        release_date = record.get("date")
        tags         = ",".join(record.get("tag", ["compiled"]))

        try:
            conn.execute(
                """
                INSERT INTO raw_releases
                    (ocid, release_id, regime, release_tags, release_date, raw_json)
                VALUES (?, ?, ?, ?, ?::TIMESTAMPTZ, ?)
                ON CONFLICT DO NOTHING
                """,
                [ocid, fts_id, REGIME, tags, release_date, json.dumps(record)],
            )
            inserted += 1
        except Exception as e:
            print(f"\n  [OCP] Insert error for {fts_id}: {e}")
            errors += 1
            continue

        if inserted % BATCH == 0:
            conn.commit()

        if i % 5000 == 0:
            print(
                f"  [OCP] {i:,}/{len(lines):,} checked | "
                f"inserted={inserted:,} skipped_existing={skipped_existing:,}"
            )

    conn.commit()

    print(
        f"[OCP] {year} complete: "
        f"inserted={inserted:,} | "
        f"already_stored={skipped_existing:,} | "
        f"no_fts_id={skipped_no_id:,} | "
        f"not_pa23={skipped_not_pa23:,} | "
        f"errors={errors}"
    )
    return inserted


def main():
    from datetime import datetime as _dt

    parser = argparse.ArgumentParser(
        description="Import missing PA23 notices from OCP Data Registry bulk downloads"
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Import a specific year (e.g. 2025). Default: current and previous year.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Delta-sync mode: import current year only (used by the web sync trigger).",
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Skip the extractor step — only import raw releases.",
    )
    args = parser.parse_args()

    conn = init_db(DB_PATH)
    ensure_date_signed_column(conn)

    current_year = _dt.now().year

    if args.year:
        years = [args.year]
    elif args.sync:
        # Delta-sync mode: only pull the current year's OCP file.
        # Already-stored records are skipped cheaply, so this is fast
        # after the initial backfill and catches any notices FTS adds
        # to its OCDS index late (OCP picks them up within a week).
        years = [current_year]
    else:
        # Full run: current year + previous year, in case the previous
        # year's OCP dataset received late additions from the FTS backfill.
        years = [current_year - 1, current_year]

    total_inserted = 0

    for year in years:
        total_inserted += ingest_year(year, conn)

    print(f"\n[OCP] Total new releases stored: {total_inserted:,}")

    if total_inserted > 0 and not args.no_extract:
        print("\n[OCP] Running extractor on newly imported releases...")
        summary = extract_all(conn)
        print("\n[extractor] Summary:")
        for k, v in summary.items():
            print(f"  {k:<40} {v:>6,}")
    elif not args.no_extract:
        print("[OCP] Nothing new to extract.")

    conn.close()
    print("\n[OCP] Done.")


if __name__ == "__main__":
    main()
