"""
db/extract_pa23_framework_suppliers.py — Expand multi-supplier awards for PA23 UK7 framework notices.

PA23 framework establishment notices can have multiple suppliers per lot/award in the
raw OCDS JSON, but the original extractor only stores one supplier per award (the first).
This script re-processes every PA23 framework establishment notice and writes one row per
supplier per award, giving accurate lot/supplier counts in the framework report.

Strategy
--------
Rather than re-parsing the full OCDS notice from scratch, we:
  1. Read the existing row(s) from notices as a base template (all notice-level fields
     are already correctly parsed — authority, CPV, dates, etc.).
  2. Read the raw JSON to get the full suppliers list per award.
  3. Delete the existing rows for that release_id.
  4. Re-insert one row per (award, supplier) with the correct award_id suffix.

Award-id format (consistent with PCR2015_FTS expanded rows):
  Original single-supplier row:  award_id = "1"   (OCDS award.id)
  Expanded rows:                 award_id = "1_0", "1_1", "1_2", ...

award_count is set to the total supplier count across ALL lots for the notice.
lot_count retains the original value (number of distinct awards/lots).

Usage
-----
  Stop the server first (DuckDB read-only connections block writes).
  python db/extract_pa23_framework_suppliers.py [--db path/to/data.duckdb] [--dry-run]
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import duckdb

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# All column names in notices table (must match schema exactly)
_COLS = [
    "regime", "release_id", "award_id", "award_count",
    "ocid", "release_tags", "notice_type",
    "title", "description", "cpv_main", "cpv_additional",
    "authority_name", "authority_id", "authority_type",
    "contact_name", "contact_email", "contact_phone",
    "proc_route", "proc_method", "is_framework_agreement", "framework_ref",
    "has_lots", "lot_count",
    "value_estimated", "value_awarded", "value_gross", "currency", "above_threshold",
    "published_date", "award_date", "tender_deadline",
    "contract_start", "contract_end", "contract_max_extent",
    "supplier_name", "supplier_id", "tenders_received",
    "is_termination", "is_performance", "is_psr", "status",
    "is_dps", "is_dynamic_market",
    "award_title",
]

_INSERT_SQL = f"""
INSERT OR REPLACE INTO notices ({', '.join(_COLS)})
VALUES ({', '.join(f'${c}' for c in _COLS)})
"""


def _row_to_dict(description, row: tuple) -> dict:
    return {description[i][0]: row[i] for i in range(len(description))}


def run(db_path: str = "data.duckdb", dry_run: bool = False) -> None:
    conn = duckdb.connect(db_path)

    # Find every PA23 framework establishment notice that has raw JSON available.
    # We exclude DPS and dynamic markets — their multi-supplier situation is different.
    log.info("Fetching PA23 framework notice list…")
    candidates = conn.execute("""
        SELECT DISTINCT n.release_id
        FROM notices n
        JOIN raw_releases r ON r.release_id = n.release_id
        WHERE n.regime = 'PA23_FTS'
          AND n.is_framework_agreement = TRUE
          AND (n.is_dps IS NULL OR n.is_dps = FALSE)
          AND (n.is_dynamic_market IS NULL OR n.is_dynamic_market = FALSE)
        ORDER BY n.release_id
    """).fetchall()
    log.info("Candidates: %d", len(candidates))

    expanded_notices = 0
    skipped_single   = 0
    errors           = 0
    total_new_rows   = 0
    batch            = []

    for (release_id,) in candidates:
        try:
            # ── 1. Read raw JSON ──────────────────────────────────────────
            raw_row = conn.execute(
                "SELECT raw_json FROM raw_releases WHERE release_id = ?",
                [release_id],
            ).fetchone()
            if not raw_row:
                skipped_single += 1
                continue

            data = json.loads(raw_row[0]) if isinstance(raw_row[0], str) else raw_row[0]
            awards = data.get("awards", [])

            # Check whether any award has more than one supplier
            max_suppliers = max((len(a.get("suppliers", [])) for a in awards), default=0)
            if max_suppliers <= 1:
                skipped_single += 1
                continue   # nothing to expand

            # ── 2. Read base row from notices (notice-level fields already parsed) ──
            cur = conn.execute(
                "SELECT * FROM notices WHERE release_id = ? ORDER BY award_id LIMIT 1",
                [release_id],
            )
            base_row = cur.fetchone()
            if not base_row:
                skipped_single += 1
                continue
            base = _row_to_dict(cur.description, base_row)

            # ── 3. Build expanded rows ────────────────────────────────────
            total_suppliers = sum(len(a.get("suppliers", [])) for a in awards)
            new_rows = []

            # Build tender lot-id → title map for relatedLots fallback (PA23)
            tender_lots = {
                str(lot["id"]): lot.get("title", "")
                for lot in data.get("tender", {}).get("lots", [])
            }

            for award in awards:
                award_id_base = str(award.get("id", ""))
                suppliers = award.get("suppliers", [])
                if not suppliers:
                    continue

                # Resolve lot title: award.title → relatedLots → tender.lots → fallback
                lot_title = award.get("title")
                if not lot_title:
                    related = award.get("relatedLots") or []
                    if related:
                        lot_title = tender_lots.get(str(related[0])) or f"Lot {award_id_base}"
                    else:
                        lot_title = f"Lot {award_id_base}"

                for j, supplier in enumerate(suppliers):
                    row = dict(base)                          # copy all notice-level fields
                    row["award_id"]      = f"{award_id_base}_{j}"
                    row["award_count"]   = total_suppliers
                    row["award_title"]   = lot_title
                    row["supplier_name"] = supplier.get("name")
                    row["supplier_id"]   = (supplier.get("identifier") or {}).get("id") \
                                           or supplier.get("id")
                    new_rows.append(row)

            if not new_rows:
                skipped_single += 1
                continue

            # ── 4. Delete old rows and queue new ones ─────────────────────
            if not dry_run:
                conn.execute("DELETE FROM notices WHERE release_id = ?", [release_id])

            batch.extend(new_rows)
            expanded_notices += 1
            total_new_rows   += len(new_rows)

            if len(batch) >= 200 and not dry_run:
                conn.executemany(_INSERT_SQL, [{c: r[c] for c in _COLS} for r in batch])
                conn.commit()
                log.info("  Progress: %d notices expanded, %d new rows, %d skipped",
                         expanded_notices, total_new_rows, skipped_single)
                batch = []

        except Exception as exc:
            errors += 1
            log.warning("Error on %s: %s", release_id, exc)

    # Flush remaining batch
    if batch and not dry_run:
        conn.executemany(_INSERT_SQL, [{c: r[c] for c in _COLS} for r in batch])
        conn.commit()

    conn.close()

    log.info(
        "Done%s: %d notices expanded → %d new rows | %d skipped (single-supplier) | %d errors",
        " [DRY RUN]" if dry_run else "",
        expanded_notices, total_new_rows, skipped_single, errors,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db",      default="data.duckdb", help="Path to DuckDB file")
    p.add_argument("--dry-run", action="store_true",   help="Report counts without writing")
    args = p.parse_args()
    run(db_path=args.db, dry_run=args.dry_run)
