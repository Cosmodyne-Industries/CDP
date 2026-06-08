"""
db/schema.py — DuckDB schema for the procurement intelligence store.

Design principles:
- raw_releases: verbatim OCDS JSON for every release, regime-tagged. This is the
  source of truth and allows us to re-extract fields as our understanding improves.
- notices: normalised key fields extracted from raw_releases. Populated by the
  extractor (built later once we've inspected real data).
- sync_state: tracks last successful pull per source so delta sync knows where to resume.

We keep the normalised table deliberately sparse at this stage — we'll add columns
once we've seen what fields are actually populated in real notices.
"""

import duckdb


CREATE_STATEMENTS = [
    # -------------------------------------------------------------------------
    # Raw storage: one row per OCDS release, verbatim JSON preserved
    # -------------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS raw_releases (
        -- Stable procurement process identifier (links all stages of one procurement)
        ocid            VARCHAR NOT NULL,

        -- Unique release identifier (one per notice publication event)
        release_id      VARCHAR NOT NULL,

        -- Which service this came from
        regime          VARCHAR NOT NULL CHECK (regime IN ('PA23_FTS', 'PCR2015_CF')),

        -- OCDS release tag(s): planning, tender, award, implementation, etc.
        -- Stored as comma-separated string for easy filtering without JSON parsing
        release_tags    VARCHAR,

        -- Publication / last-updated timestamp (from the release itself)
        release_date    TIMESTAMPTZ,

        -- When we ingested this record
        ingested_at     TIMESTAMPTZ DEFAULT now(),

        -- The full OCDS release as JSON
        raw_json        JSON NOT NULL,

        PRIMARY KEY (regime, release_id)
    )
    """,

    # -------------------------------------------------------------------------
    # Normalised notices: key fields extracted from raw_releases.
    # Populated by extractor module (to be built after data inspection).
    # Nullable across the board — different notice types populate different fields.
    # -------------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS notices (
        -- Links back to raw_releases
        regime          VARCHAR NOT NULL,
        release_id      VARCHAR NOT NULL,
        ocid            VARCHAR NOT NULL,

        -- Notice classification
        release_tags    VARCHAR,               -- e.g. 'tender', 'award'
        notice_type     VARCHAR,               -- PA23: e.g. 'Contract Details Notice'
                                               -- PCR2015: derived from tag

        -- Procurement subject
        title           VARCHAR,
        description     VARCHAR,
        cpv_main        VARCHAR,               -- e.g. '79710000'
        cpv_additional  VARCHAR,               -- comma-separated additional CPVs

        -- Authority / buyer
        authority_name  VARCHAR,
        authority_id    VARCHAR,               -- PPON or org identifier
        authority_type  VARCHAR,               -- e.g. 'local authority', 'NHS trust'
        contact_name    VARCHAR,
        contact_email   VARCHAR,
        contact_phone   VARCHAR,

        -- Procurement route and method
        proc_route      VARCHAR,               -- e.g. 'open', 'restricted', 'framework'
        proc_method     VARCHAR,               -- more detailed method if available
        is_framework_agreement  BOOLEAN,
        framework_ref   VARCHAR,               -- framework agreement identifier

        -- Lot structure
        has_lots        BOOLEAN,
        lot_count       INTEGER,

        -- Estimated / confirmed values (NULL until an award notice confirms them)
        value_estimated DECIMAL(18,2),
        value_awarded   DECIMAL(18,2),         -- net / ex-VAT
        value_gross     DECIMAL(18,2),         -- gross / inc-VAT (amountGross)
        currency        VARCHAR DEFAULT 'GBP',

        -- PA23: whether the contract is above or below threshold (both are UK7)
        above_threshold BOOLEAN,

        -- Timeline
        published_date  TIMESTAMPTZ,       -- when the notice was published/indexed on the source platform
        award_date      TIMESTAMPTZ,       -- when the contract was actually awarded (awards[0].date)
        tender_deadline TIMESTAMPTZ,
        contract_start  DATE,
        contract_end    DATE,

        -- Award-specific (only populated on award/contract-details notices)
        supplier_name   VARCHAR,
        supplier_id     VARCHAR,               -- Companies House / charity number
        tenders_received INTEGER,              -- number of bids received

        -- Performance / termination flags
        is_termination  BOOLEAN DEFAULT false,
        is_performance  BOOLEAN DEFAULT false,

        -- PSR flag: text-match based, imperfect but best available
        -- True if "Provider Selection Regime" or " PSR " found in title/description
        is_psr          BOOLEAN DEFAULT false,

        -- Status
        status          VARCHAR,               -- active, complete, cancelled, etc.

        PRIMARY KEY (regime, release_id),
        FOREIGN KEY (regime, release_id) REFERENCES raw_releases(regime, release_id)
    )
    """,

    # -------------------------------------------------------------------------
    # Sync state: tracks the high-water mark for delta pulls per source
    # -------------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS sync_state (
        source              VARCHAR PRIMARY KEY,
        last_pulled_at      TIMESTAMPTZ,       -- upper bound of last pull window
        earliest_coverage   TIMESTAMPTZ,       -- oldest data we've ever pulled for this source
        last_release_count  INTEGER,
        updated_at          TIMESTAMPTZ DEFAULT now()
    )
    """,

    # Indexes for common query patterns
    "CREATE INDEX IF NOT EXISTS idx_notices_cpv     ON notices(cpv_main)",
    "CREATE INDEX IF NOT EXISTS idx_notices_supplier ON notices(supplier_name)",
    "CREATE INDEX IF NOT EXISTS idx_notices_authority ON notices(authority_name)",
    "CREATE INDEX IF NOT EXISTS idx_notices_ocid    ON notices(ocid)",
    "CREATE INDEX IF NOT EXISTS idx_raw_ocid        ON raw_releases(ocid)",
    "CREATE INDEX IF NOT EXISTS idx_raw_regime_date ON raw_releases(regime, release_date)",
]


def init_db(db_path: str = "data.duckdb") -> duckdb.DuckDBPyConnection:
    """Create the database and all tables/indexes if they don't exist."""
    conn = duckdb.connect(db_path)
    for stmt in CREATE_STATEMENTS:
        conn.execute(stmt)
    conn.commit()
    print(f"[schema] Database ready: {db_path}")
    return conn


if __name__ == "__main__":
    init_db()
    print("[schema] Tables created successfully.")
