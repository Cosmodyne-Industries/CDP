"""
import_cf_bulk.py — Import Contracts Finder historical data from OCP Data Registry.

Downloads yearly JSONL.gz files (compiled releases) from:
  https://data.open-contracting.org/en/publication/128/download?name=YEAR.jsonl.gz

Coverage: 2021–present (5-year window). Pre-2021 data is omitted as field
structures changed significantly around the move from OJEU to TED.

Only award records (releases with a non-empty `awards` array) are stored.
Already-imported records are skipped automatically (ON CONFLICT DO NOTHING),
so the script is safe to interrupt and re-run.

Usage:
    python import_cf_bulk.py                     # import all years (2021–current)
    python import_cf_bulk.py --years 2022 2023   # specific years only
    python import_cf_bulk.py --clear             # delete existing CF data first, then import all
    python import_cf_bulk.py --status            # show DB counts per year and exit
"""

import argparse
import gzip
import io
import json
import sys
import urllib.request
from datetime import datetime, timezone

from db.schema import init_db
from db.store import upsert_releases, set_sync_state

REGIME = "PCR2015_CF"
OCP_URL = "https://data.open-contracting.org/en/publication/128/download?name={year}.jsonl.gz"
FIRST_YEAR = 2021
CURRENT_YEAR = datetime.now().year

BATCH_SIZE = 500   # records to upsert per commit


def fetch_year(year: int) -> tuple[int, int]:
    """
    Download and import one year's JSONL.gz file.

    Returns (award_records_stored, total_records_read).
    Already-existing records count toward stored (they're just silently skipped
    by ON CONFLICT) but we can't distinguish them here — resuming is safe.
    """
    url = OCP_URL.format(year=year)
    print(f"  Downloading {url} ...", flush=True)

    req = urllib.request.Request(url, headers={"User-Agent": "fts-researcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            compressed = resp.read()
    except Exception as e:
        print(f"  ERROR downloading {year}: {e}")
        return 0, 0

    size_mb = len(compressed) / 1_048_576
    print(f"  Downloaded {size_mb:.1f} MB compressed. Parsing ...", flush=True)

    total = 0
    awards_count = 0
    batch = []
    inserted_total = 0

    with gzip.open(io.BytesIO(compressed), "rb") as gz:
        for raw_line in gz:
            total += 1
            try:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                release = json.loads(line)
            except Exception:
                continue

            # Only keep award records
            if not release.get("awards"):
                continue
            awards_count += 1
            batch.append(release)

            if len(batch) >= BATCH_SIZE:
                ins, _ = upsert_releases(conn, batch, REGIME)
                inserted_total += ins
                batch = []

    if batch:
        ins, _ = upsert_releases(conn, batch, REGIME)
        inserted_total += ins

    return inserted_total, total


def status(conn, years: list[int]) -> None:
    rows = conn.execute(
        """
        SELECT strftime(release_date, '%Y') AS yr, COUNT(*) AS n
        FROM raw_releases
        WHERE regime = ?
          AND release_date IS NOT NULL
        GROUP BY yr
        ORDER BY yr
        """,
        [REGIME],
    ).fetchall()
    by_year = {r[0]: r[1] for r in rows}
    unknown = conn.execute(
        "SELECT COUNT(*) FROM raw_releases WHERE regime = ? AND release_date IS NULL",
        [REGIME],
    ).fetchone()[0]

    total = sum(by_year.values()) + unknown
    print(f"\nCF records in DB: {total:,}")
    print(f"{'Year':<6}  {'Records':>10}")
    print("-" * 20)
    for yr in [str(y) for y in years]:
        n = by_year.get(yr, 0)
        print(f"{yr:<6}  {n:>10,}")
    if unknown:
        print(f"{'(none)':<6}  {unknown:>10,}")
    print("-" * 20)
    print(f"{'Total':<6}  {total:>10,}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="Import CF bulk data from OCP Data Registry")
parser.add_argument("--years",  nargs="+", type=int,
                    help="Specific years to import (default: all 2016–current)")
parser.add_argument("--db",    default="data.duckdb", help="DuckDB file path")
parser.add_argument("--clear", action="store_true",
                    help="Delete all existing CF records before importing")
parser.add_argument("--status", action="store_true",
                    help="Show DB counts per year and exit")
args = parser.parse_args()

conn = init_db(args.db)

years = args.years if args.years else list(range(FIRST_YEAR, CURRENT_YEAR + 1))

if args.status:
    status(conn, years)
    conn.close()
    sys.exit(0)

if args.clear:
    print("Clearing existing CF data from raw_releases and notices ...", flush=True)
    conn.execute("DELETE FROM notices WHERE regime = ?", [REGIME])
    conn.execute("DELETE FROM raw_releases WHERE regime = ?", [REGIME])
    conn.commit()
    print("Done.\n")

print(f"CF bulk import: years {years[0]}–{years[-1]}")
print()

grand_total_read = 0
grand_total_awards = 0

for year in years:
    print(f"[{year}]", flush=True)
    try:
        stored, read = fetch_year(year)
        grand_total_read += read
        grand_total_awards += stored
        print(f"  {year}: {stored:,} award records stored  ({read:,} total in file)")
    except KeyboardInterrupt:
        print(f"\nInterrupted during {year}. Progress saved. Re-run to continue.")
        conn.close()
        sys.exit(0)
    except Exception as e:
        print(f"  ERROR on {year}: {e}")
        print("  Skipping year and continuing ...")

# Update sync state
set_sync_state(
    conn,
    source="PCR2015_CF_bulk",
    pulled_at=datetime.now(timezone.utc),
    release_count=grand_total_awards,
    pulled_from=datetime(years[0], 1, 1, tzinfo=timezone.utc),
)

print()
print(f"Import complete.")
print(f"Total records read across all years: {grand_total_read:,}")
print(f"Award records stored/skipped:        {grand_total_awards:,}")

row = conn.execute("SELECT COUNT(*) FROM raw_releases WHERE regime = ?", [REGIME]).fetchone()
print(f"CF raw_releases total in DB:         {row[0]:,}")

conn.close()
