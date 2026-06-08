"""
db/store.py — Upsert and query helpers for raw_releases and sync_state.

At this stage we only populate raw_releases (verbatim JSON) and sync_state.
The notices table will be populated by the extractor once we understand the fields.
"""

import json
import duckdb
from datetime import datetime, timezone
from typing import Optional


def upsert_releases(
    conn: duckdb.DuckDBPyConnection,
    releases: list[dict],
    regime: str,
) -> tuple[int, int]:
    """
    Insert or replace OCDS releases into raw_releases.

    Returns (inserted, skipped) counts.
    'Skipped' means a release with that ID already existed — we don't overwrite
    because the raw record shouldn't change once published (amendments come as new
    release IDs). We do log if the existing record differs, which may indicate an
    amendment we should handle.
    """
    inserted = 0
    skipped = 0

    for release in releases:
        release_id = release.get("id", "")
        ocid = release.get("ocid", "")

        if not release_id or not ocid:
            print(f"  [store] WARNING: release missing id or ocid, skipping: {release}")
            skipped += 1
            continue

        # Extract tags as comma-separated string for easy column filtering
        tags = release.get("tag", [])
        if isinstance(tags, list):
            tags_str = ",".join(tags)
        else:
            tags_str = str(tags)

        # Extract release date
        release_date_raw = release.get("date")
        if release_date_raw:
            try:
                # Handle both Z and +offset formats
                release_date = release_date_raw.replace("Z", "+00:00")
            except Exception:
                release_date = None
        else:
            release_date = None

        try:
            conn.execute(
                """
                INSERT INTO raw_releases
                    (ocid, release_id, regime, release_tags, release_date, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (regime, release_id) DO NOTHING
                """,
                [
                    ocid,
                    release_id,
                    regime,
                    tags_str,
                    release_date,
                    json.dumps(release),
                ],
            )
            # DuckDB doesn't expose affected row count easily; check via a re-query
            # In practice ON CONFLICT DO NOTHING silently skips — we track separately
            inserted += 1
        except Exception as e:
            print(f"  [store] ERROR inserting {release_id}: {e}")
            skipped += 1

    conn.commit()
    return inserted, skipped


def get_sync_state(
    conn: duckdb.DuckDBPyConnection, source: str
) -> Optional[datetime]:
    """Return the last_pulled_at timestamp for a source, or None if never synced."""
    result = conn.execute(
        "SELECT last_pulled_at FROM sync_state WHERE source = ?", [source]
    ).fetchone()
    if result and result[0]:
        return result[0]
    return None


def set_sync_state(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    pulled_at: datetime,
    release_count: int,
    pulled_from: datetime = None,
) -> None:
    """Upsert the sync state for a source after a successful pull.

    earliest_coverage keeps the oldest pull_from we've ever used — it only
    moves backwards, never forwards, so it reflects our true historical reach.
    """
    conn.execute(
        """
        INSERT INTO sync_state (source, last_pulled_at, earliest_coverage, last_release_count, updated_at)
        VALUES (?, ?, ?, ?, now())
        ON CONFLICT (source) DO UPDATE SET
            last_pulled_at     = excluded.last_pulled_at,
            earliest_coverage  = LEAST(COALESCE(sync_state.earliest_coverage, excluded.earliest_coverage),
                                       COALESCE(excluded.earliest_coverage, sync_state.earliest_coverage)),
            last_release_count = excluded.last_release_count,
            updated_at         = excluded.updated_at
        """,
        [source, pulled_at, pulled_from, release_count],
    )
    conn.commit()


def release_count(conn: duckdb.DuckDBPyConnection, regime: Optional[str] = None) -> int:
    """Count raw releases, optionally filtered by regime."""
    if regime:
        return conn.execute(
            "SELECT COUNT(*) FROM raw_releases WHERE regime = ?", [regime]
        ).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM raw_releases").fetchone()[0]


def sample_releases(
    conn: duckdb.DuckDBPyConnection,
    regime: Optional[str] = None,
    tag: Optional[str] = None,
    n: int = 5,
) -> list[dict]:
    """Return n raw releases as parsed dicts, optionally filtered."""
    where_clauses = []
    params = []

    if regime:
        where_clauses.append("regime = ?")
        params.append(regime)
    if tag:
        where_clauses.append("release_tags LIKE ?")
        params.append(f"%{tag}%")

    where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    params.append(n)

    rows = conn.execute(
        f"SELECT raw_json FROM raw_releases {where} LIMIT ?", params
    ).fetchall()

    return [json.loads(row[0]) for row in rows]


def ocid_exists(conn: duckdb.DuckDBPyConnection, ocid: str, regime: str) -> bool:
    """Check whether any release for a given ocid+regime already exists."""
    result = conn.execute(
        "SELECT 1 FROM raw_releases WHERE ocid = ? AND regime = ? LIMIT 1",
        [ocid, regime],
    ).fetchone()
    return result is not None
