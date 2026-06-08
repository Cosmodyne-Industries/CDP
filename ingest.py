"""
ingest.py — Main entry point for pulling notices into the local store.

Usage:
    # Initial pull: all notices for a CPV code in the last N days
    python ingest.py --cpv 79710000 --days 365

    # Initial pull: all notices (no CPV filter) in the last 30 days — good for sampling
    python ingest.py --days 30

    # Delta sync: only pull what's changed since last run
    python ingest.py --sync

    # Pull a specific source only
    python ingest.py --days 30 --source fts
    python ingest.py --days 30 --source cf

    # Pull specific stages only (useful for targeted sampling)
    python ingest.py --days 30 --stages award
    python ingest.py --days 30 --stages planning,tender
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional — can set env vars directly

from db.schema import init_db
from db.store import (
    upsert_releases,
    get_sync_state,
    set_sync_state,
    release_count,
)
from connectors.fts import FTSClient, REGIME as FTS_REGIME
from connectors.contracts_finder import ContractsFinderClient, REGIME as CF_REGIME
from utils.delta import get_pull_window


RAW_DIR = Path("raw")


def filter_by_cpv(releases: list[dict], cpv_code: str) -> list[dict]:
    """
    Client-side CPV filter. Returns releases where the CPV code appears in
    any of the items.additionalClassifications fields (OCDS standard location).

    CPV matching is prefix-friendly: '79000000' will match '79710000'.
    Pass the full 8-digit code for exact CPV matching.
    """
    matched = []
    for r in releases:
        tender = r.get("tender", {})
        items = tender.get("items", [])
        if not items:
            # Some releases put classification at tender level
            classifications = tender.get("additionalClassifications", [])
            items = [{"additionalClassifications": classifications}]

        for item in items:
            for cls in item.get("additionalClassifications", []):
                if cls.get("scheme", "").upper() == "CPV":
                    if cls.get("id", "").startswith(cpv_code[:4]):  # division-level match
                        matched.append(r)
                        break  # don't double-count a release

    return matched


def run_fts(
    pull_from,
    pull_to,
    conn,
    cpv_filter: str | None,
    stages: list[str] | None,
):
    print("\n--- Find a Tender Service (FTS / PA23) ---")
    try:
        client = FTSClient(raw_dir=RAW_DIR / "fts")
    except ValueError as e:
        print(f"  [FTS] Skipping: {e}")
        return 0

    total_inserted = 0
    total_skipped = 0

    try:
        for page_releases in client.fetch_releases(
            updated_from=pull_from,
            updated_to=pull_to,
            stages=stages,
        ):
            if cpv_filter:
                page_releases = filter_by_cpv(page_releases, cpv_filter)
                print(f"  [FTS] After CPV filter ({cpv_filter}): {len(page_releases)} releases")

            if page_releases:
                ins, skip = upsert_releases(conn, page_releases, FTS_REGIME)
                total_inserted += ins
                total_skipped += skip

        print(f"  [FTS] Done. Inserted: {total_inserted}, already existed: {total_skipped}")
        set_sync_state(conn, "FTS", pull_to, total_inserted, pulled_from=pull_from)

    except Exception as e:
        print(f"\n  [FTS] Pull failed after {total_inserted} insertions: {e}")
        print(f"  [FTS] Saving partial progress to sync_state.")
        set_sync_state(conn, "FTS", pull_to, total_inserted, pulled_from=pull_from)
        print(f"  [FTS] Re-run the same command to resume — already-stored records will be skipped.")

    return total_inserted


def run_cf(
    pull_from,
    pull_to,
    conn,
    cpv_filter: str | None,
    stages: list[str] | None,
):
    print("\n--- Contracts Finder (CF / PCR2015) ---")
    client = ContractsFinderClient(raw_dir=RAW_DIR / "cf")

    total_inserted = 0
    total_skipped = 0

    for page_releases in client.fetch_releases(
        published_from=pull_from,
        published_to=pull_to,
        stages=stages,
    ):
        if cpv_filter:
            page_releases = filter_by_cpv(page_releases, cpv_filter)
            print(f"  [CF] After CPV filter ({cpv_filter}): {len(page_releases)} releases")

        if page_releases:
            ins, skip = upsert_releases(conn, page_releases, CF_REGIME)
            total_inserted += ins
            total_skipped += skip

    print(f"  [CF] Done. Inserted: {total_inserted}, already existed: {total_skipped}")
    set_sync_state(conn, "CF", pull_to, total_inserted, pulled_from=pull_from)
    return total_inserted


def _print_status(conn) -> None:
    """Print a summary of what data we have and what we've pulled."""
    print("\n=== Data coverage ===")

    sync = conn.execute(
        "SELECT source, earliest_coverage::DATE, last_pulled_at::DATE FROM sync_state ORDER BY source"
    ).fetchall()

    if not sync:
        print("  No pulls recorded yet.")
    else:
        for source, earliest, latest in sync:
            if earliest:
                print(f"  {source:<6}  pulled from {earliest} to {latest}")
            else:
                print(f"  {source:<6}  last pulled {latest}  (no earliest_coverage recorded)")

    print("\n=== Notices in database ===")
    rows = conn.execute("""
        SELECT regime,
               COUNT(*)                     AS notices,
               MIN(published_date)::DATE    AS earliest,
               MAX(published_date)::DATE    AS latest
        FROM notices
        GROUP BY regime
        ORDER BY regime
    """).fetchall()

    if not rows:
        print("  No notices extracted yet. Run: python -m db.extractor")
    else:
        for regime, count, earliest, latest in rows:
            print(f"  {regime:<15}  {count:>6,} notices  {earliest} to {latest}")

    print("\n=== Raw releases in database ===")
    rows = conn.execute("""
        SELECT regime, COUNT(*) AS count
        FROM raw_releases
        GROUP BY regime ORDER BY regime
    """).fetchall()
    for regime, count in rows:
        print(f"  {regime:<15}  {count:>6,} raw releases")
    print()


def main():
    parser = argparse.ArgumentParser(description="Procurement Intelligence — data ingestion")
    parser.add_argument("--cpv",    help="CPV code to filter on (e.g. 79710000)", default=None)
    parser.add_argument("--days",   help="Days back for initial pull (default: 365)", type=int, default=365)
    parser.add_argument("--sync",   help="Delta sync mode: pull only since last run", action="store_true")
    parser.add_argument("--source", help="Pull from 'fts', 'cf', or 'both' (default: both)", default="both")
    parser.add_argument("--stages", help="Comma-separated OCDS stages e.g. planning,tender,award", default=None)
    parser.add_argument("--db",     help="Path to DuckDB file (default: data.duckdb)", default="data.duckdb")
    parser.add_argument("--status", help="Show data coverage and exit", action="store_true")
    args = parser.parse_args()

    # Initialise database
    conn = init_db(args.db)

    if args.status:
        _print_status(conn)
        conn.close()
        return

    # Parse stages
    stages = [s.strip() for s in args.stages.split(",")] if args.stages else None

    # Determine pull window
    if args.sync:
        # Use the earliest last_pulled_at across sources (or fall back to initial window)
        fts_last = get_sync_state(conn, "FTS")
        cf_last = get_sync_state(conn, "CF")
        earliest = min(filter(None, [fts_last, cf_last]), default=None)
        pull_from, pull_to = get_pull_window(earliest, initial_days_back=args.days)
    else:
        pull_from, pull_to = get_pull_window(None, initial_days_back=args.days)

    print(f"\n[ingest] Pull window: {pull_from.date()} to {pull_to.date()}")
    if args.cpv:
        print(f"[ingest] CPV filter:  {args.cpv} (applied client-side)")
    if stages:
        print(f"[ingest] Stages:      {stages}")

    total = 0

    if args.source in ("fts", "both"):
        total += run_fts(pull_from, pull_to, conn, args.cpv, stages)

    if args.source in ("cf", "both"):
        total += run_cf(pull_from, pull_to, conn, args.cpv, stages)

    print(f"\n[ingest] Complete. Total new releases stored: {total}")
    print(f"[ingest] Database totals:")
    print(f"  FTS (PA23):      {release_count(conn, FTS_REGIME):>8,} releases")
    print(f"  CF  (PCR2015):   {release_count(conn, CF_REGIME):>8,} releases")
    print(f"  Total:           {release_count(conn):>8,} releases")

    conn.close()


if __name__ == "__main__":
    main()
