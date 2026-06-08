"""
db/extract_pcr2015_fts.py — Targeted re-extraction for PCR2015_FTS notices.

Re-processes only the ~821 PCR2015_FTS candidates from raw_releases and
INSERT OR REPLACEs them into notices, populating is_dps correctly.

Run after migrate_v3.py.  Much faster than a full re-extract because it:
  - uses a DuckDB-native WHERE clause to pre-filter candidates
  - batches JSON parsing in Python but only over the small result set
"""

import json
import logging
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from extractor import classify_release, _INSERT_SQL

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def run(db_path: str = "data.duckdb") -> None:
    conn = duckdb.connect(db_path)

    # Fetch only the small set of PCR2015_FTS candidates.
    # release_tags filter first (indexed-friendly), then JSON legalBasis check.
    rows = conn.execute("""
        SELECT release_id, regime, raw_json
        FROM raw_releases
        WHERE regime = 'PA23_FTS'
          AND release_tags LIKE '%award%'
          AND json_extract_string(raw_json, '$.tender.legalBasis.id') = '32014L0024'
    """).fetchall()

    log.info("PCR2015_FTS candidates: %d", len(rows))

    extracted = 0
    dps_count = 0
    errors = 0
    batch = []

    for release_id, regime, raw_json in rows:
        try:
            data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            records = classify_release(release_id, regime, data)
            if records:
                batch.extend(records)
                extracted += len(records)
                if any(r.get("is_dps") for r in records):
                    dps_count += 1
            if len(batch) >= 200:
                conn.executemany(_INSERT_SQL, batch)
                conn.commit()
                batch = []
        except Exception as e:
            errors += 1
            log.warning("Error %s: %s", release_id, e)

    if batch:
        conn.executemany(_INSERT_SQL, batch)
        conn.commit()

    conn.close()
    log.info(
        "Done: %d rows extracted, %d DPS establishments, %d errors",
        extracted, dps_count, errors,
    )


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data.duckdb")
    args = p.parse_args()
    run(args.db)
