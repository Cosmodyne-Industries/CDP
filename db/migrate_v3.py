"""
db/migrate_v3.py — Add is_dps and is_dynamic_market columns to notices table.

Run once; safe to re-run (ALTER TABLE IF NOT EXISTS pattern via try/except).
No data is lost. After running, re-extract PCR2015_FTS notices to populate is_dps.
"""

import duckdb
import argparse


def migrate(db_path: str = "data.duckdb") -> None:
    conn = duckdb.connect(db_path)

    for col, ddl in [
        ("is_dps",            "ALTER TABLE notices ADD COLUMN is_dps BOOLEAN DEFAULT FALSE"),
        ("is_dynamic_market", "ALTER TABLE notices ADD COLUMN is_dynamic_market BOOLEAN DEFAULT FALSE"),
    ]:
        try:
            conn.execute(ddl)
            print(f"Added column: {col}")
        except Exception as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                print(f"Column already exists, skipping: {col}")
            else:
                raise

    conn.commit()
    conn.close()
    print("Migration v3 complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data.duckdb")
    args = parser.parse_args()
    migrate(args.db)
