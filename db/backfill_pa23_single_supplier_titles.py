"""
db/backfill_pa23_single_supplier_titles.py — Populate award_title for PA23 single-supplier
framework notices that were skipped by extract_pa23_framework_suppliers.py.

The PA23 expander only rewrites notices with 2+ suppliers per award.  For single-supplier
lots (award_count <= 1 per award), the notices rows still exist but have award_title = NULL.
This script reads the raw JSON and UPDATEs those rows in place — no delete/re-insert needed.

Usage
-----
  Stop the server first (DuckDB read-only connections block writes).
  python db/backfill_pa23_single_supplier_titles.py [--db path/to/data.duckdb] [--dry-run]
"""

import argparse
import json
import logging
import re

import duckdb

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_SUFFIX_RE = re.compile(r"_\d+$")


def run(db_path: str = "data.duckdb", dry_run: bool = False) -> None:
    conn = duckdb.connect(db_path)

    log.info("Finding PA23 framework notices with no award_title…")
    release_ids = conn.execute("""
        SELECT DISTINCT n.release_id
        FROM notices n
        JOIN raw_releases r ON r.release_id = n.release_id
        WHERE n.regime = 'PA23_FTS'
          AND n.is_framework_agreement = TRUE
          AND n.award_title IS NULL
        ORDER BY n.release_id
    """).fetchall()
    log.info("Releases to backfill: %d", len(release_ids))

    updated_releases = 0
    updated_rows     = 0
    skipped          = 0
    errors           = 0

    for (release_id,) in release_ids:
        try:
            raw_row = conn.execute(
                "SELECT raw_json FROM raw_releases WHERE release_id = ?", [release_id]
            ).fetchone()
            if not raw_row:
                skipped += 1
                continue

            data   = json.loads(raw_row[0]) if isinstance(raw_row[0], str) else raw_row[0]
            awards = data.get("awards", [])
            if not awards:
                skipped += 1
                continue

            # Build tender lot-id → title map for relatedLots fallback
            tender_lots = {
                str(lot["id"]): lot.get("title", "")
                for lot in data.get("tender", {}).get("lots", [])
            }

            # Build award_id → title map
            title_map: dict[str, str] = {}
            for award in awards:
                aid = str(award.get("id", ""))
                if not aid:
                    continue
                title = award.get("title")
                if not title:
                    related = award.get("relatedLots") or []
                    if related:
                        title = tender_lots.get(str(related[0])) or f"Lot {aid}"
                    else:
                        title = f"Lot {aid}"
                title_map[aid] = title

            notice_rows = conn.execute(
                "SELECT award_id FROM notices WHERE release_id = ? AND award_title IS NULL",
                [release_id],
            ).fetchall()

            if not notice_rows:
                skipped += 1
                continue

            params = []
            for (award_id,) in notice_rows:
                title = title_map.get(award_id) or title_map.get(_SUFFIX_RE.sub("", award_id))
                if title:
                    params.append((title, release_id, award_id))

            if not params:
                skipped += 1
                continue

            if not dry_run:
                conn.executemany(
                    "UPDATE notices SET award_title = ? WHERE release_id = ? AND award_id = ?",
                    params,
                )
                conn.commit()

            updated_releases += 1
            updated_rows     += len(params)

        except Exception as exc:
            errors += 1
            log.warning("Error on %s: %s", release_id, exc)

    conn.close()
    log.info(
        "Done%s: %d releases → %d rows updated | %d skipped | %d errors",
        " [DRY RUN]" if dry_run else "",
        updated_releases, updated_rows, skipped, errors,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db",      default="data.duckdb", help="Path to DuckDB file")
    p.add_argument("--dry-run", action="store_true",   help="Report counts without writing")
    args = p.parse_args()
    run(db_path=args.db, dry_run=args.dry_run)
