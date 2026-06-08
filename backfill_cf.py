"""
backfill_cf.py — Backfill Contracts Finder award notices month by month.

Pulls all CF award-stage notices from a start date to today, one calendar
month at a time.  Safe to interrupt and re-run: months already fully stored
are skipped automatically.

Usage:
    python backfill_cf.py                      # 5 years back (default)
    python backfill_cf.py --years 3            # 3 years back
    python backfill_cf.py --from 2021-01-01    # explicit start date
    python backfill_cf.py --status             # show progress and exit
"""

import argparse
import sys
from datetime import datetime, timezone, date
from dateutil.relativedelta import relativedelta

from db.schema import init_db
from db.store import upsert_releases
from connectors.contracts_finder import ContractsFinderClient, REGIME


def get_months(start: date, end: date) -> list[tuple[datetime, datetime]]:
    """Return list of (month_start, month_end) UTC datetime pairs."""
    months = []
    cur = start.replace(day=1)
    while cur <= end:
        month_start = datetime(cur.year, cur.month, 1, tzinfo=timezone.utc)
        next_month = cur + relativedelta(months=1)
        month_end = datetime(next_month.year, next_month.month, 1, tzinfo=timezone.utc)
        months.append((month_start, month_end))
        cur = next_month
    return months


def months_ingested(conn) -> set[str]:
    """Return set of 'YYYY-MM' strings for months that already have CF data."""
    rows = conn.execute("""
        SELECT DISTINCT strftime(release_date AT TIME ZONE 'UTC', '%Y-%m') AS ym
        FROM raw_releases
        WHERE regime = 'PCR2015_CF'
          AND release_date IS NOT NULL
    """).fetchall()
    return {r[0] for r in rows if r[0]}


def main():
    parser = argparse.ArgumentParser(description="Backfill CF award notices month by month")
    parser.add_argument("--years",  type=int, default=5,   help="Years back from today (default: 5)")
    parser.add_argument("--from",   dest="start_date",     help="Start date YYYY-MM-DD (overrides --years)")
    parser.add_argument("--db",     default="data.duckdb", help="DuckDB file path")
    parser.add_argument("--status", action="store_true",   help="Show progress and exit")
    args = parser.parse_args()

    conn = init_db(args.db)

    today = date.today()
    if args.start_date:
        start = date.fromisoformat(args.start_date)
    else:
        start = today - relativedelta(years=args.years)

    all_months = get_months(start, today)
    already_done = months_ingested(conn)

    if args.status:
        done = [m for m, _ in all_months if m.strftime("%Y-%m") in already_done]
        remaining = [m for m, _ in all_months if m.strftime("%Y-%m") not in already_done]
        print(f"Backfill range:  {start} → {today}  ({len(all_months)} months)")
        print(f"Already ingested: {len(done)} months")
        print(f"Still to do:      {len(remaining)} months")
        if remaining:
            print(f"Next up:          {remaining[0].strftime('%Y-%m')}")
        conn.close()
        return

    pending = [(s, e) for s, e in all_months if s.strftime("%Y-%m") not in already_done]

    print(f"CF backfill: {start} → {today}")
    print(f"Total months: {len(all_months)}  |  Already done: {len(all_months) - len(pending)}  |  To fetch: {len(pending)}")
    print()

    if not pending:
        print("Nothing to do — all months already ingested.")
        conn.close()
        return

    client = ContractsFinderClient()
    total_inserted = 0
    total_skipped = 0

    for i, (month_start, month_end) in enumerate(pending, 1):
        label = month_start.strftime("%Y-%m")
        print(f"[{i}/{len(pending)}] {label} ...", flush=True)

        try:
            month_inserted = 0
            month_skipped = 0
            for page_releases in client.fetch_releases(
                published_from=month_start,
                published_to=month_end,
                stages=["award"],
                max_pages=200,   # 20,000 records max per month — well above observed ~4,500
            ):
                ins, skip = upsert_releases(conn, page_releases, REGIME)
                month_inserted += ins
                month_skipped += skip

            total_inserted += month_inserted
            total_skipped += month_skipped
            print(f"         inserted {month_inserted:,}  skipped {month_skipped:,}  "
                  f"(running total: {total_inserted:,})")

        except KeyboardInterrupt:
            print(f"\nInterrupted during {label}. Progress saved. Re-run to continue.")
            sys.exit(0)
        except Exception as e:
            print(f"  ERROR on {label}: {e}")
            print("  Skipping month and continuing...")

    print(f"\nBackfill complete.")
    print(f"Inserted: {total_inserted:,}  |  Already existed: {total_skipped:,}")

    # Final count
    row = conn.execute("SELECT COUNT(*) FROM raw_releases WHERE regime = 'PCR2015_CF'").fetchone()
    print(f"Total CF releases in DB: {row[0]:,}")
    conn.close()


if __name__ == "__main__":
    main()
