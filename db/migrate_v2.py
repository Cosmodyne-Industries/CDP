"""
db/migrate_v2.py — Recreate notices table with (release_id, award_id) composite PK.

Run once before re-extracting. Drops all existing notice rows — the extractor
will repopulate everything from raw_releases.
"""

import duckdb
import argparse

CREATE_NOTICES = """
CREATE TABLE notices (
    regime                  VARCHAR,
    release_id              VARCHAR,
    award_id                VARCHAR NOT NULL DEFAULT '0',
    award_count             INTEGER,
    ocid                    VARCHAR,
    release_tags            VARCHAR,
    notice_type             VARCHAR,
    title                   VARCHAR,
    description             VARCHAR,
    cpv_main                VARCHAR,
    cpv_additional          VARCHAR,
    authority_name          VARCHAR,
    authority_id            VARCHAR,
    authority_type          VARCHAR,
    contact_name            VARCHAR,
    contact_email           VARCHAR,
    contact_phone           VARCHAR,
    proc_route              VARCHAR,
    proc_method             VARCHAR,
    is_framework_agreement  BOOLEAN,
    framework_ref           VARCHAR,
    has_lots                BOOLEAN,
    lot_count               INTEGER,
    value_estimated         DECIMAL(18,2),
    value_awarded           DECIMAL(18,2),
    value_gross             DECIMAL(18,2),
    currency                VARCHAR,
    above_threshold         BOOLEAN,
    published_date          TIMESTAMPTZ,
    award_date              TIMESTAMPTZ,
    tender_deadline         TIMESTAMPTZ,
    contract_start          DATE,
    contract_end            DATE,
    contract_max_extent     DATE,
    supplier_name           VARCHAR,
    supplier_id             VARCHAR,
    tenders_received        INTEGER,
    is_termination          BOOLEAN,
    is_performance          BOOLEAN,
    is_psr                  BOOLEAN,
    status                  VARCHAR,
    is_dps                  BOOLEAN DEFAULT FALSE,
    is_dynamic_market       BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (release_id, award_id)
)
"""


def migrate(db_path: str = "data.duckdb") -> None:
    conn = duckdb.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS notices")
    conn.execute(CREATE_NOTICES)
    conn.commit()
    conn.close()
    print("Migration complete: notices table recreated with (release_id, award_id) PK.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data.duckdb")
    args = parser.parse_args()
    migrate(args.db)
