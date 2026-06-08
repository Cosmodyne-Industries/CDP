"""
utils/delta.py — Helpers for determining what time window to pull.

Delta sync strategy:
- On first run: pull the configured lookback window (e.g. 365 days)
- On subsequent runs: pull from (last_pulled_at - 1 hour) to now
  The 1-hour overlap guards against clock skew and notices that may be
  indexed slightly after their published timestamp.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional


OVERLAP_HOURS = 1  # Safety overlap to catch any borderline records


def get_pull_window(
    last_pulled_at: Optional[datetime],
    initial_days_back: int = 365,
) -> tuple[datetime, datetime]:
    """
    Calculate the (from, to) window for the next pull.

    Args:
        last_pulled_at:     timestamp of last successful pull, or None for first run
        initial_days_back:  how far back to go on first run

    Returns:
        (pull_from, pull_to) as timezone-aware UTC datetimes
    """
    now = datetime.now(timezone.utc)

    if last_pulled_at is None:
        pull_from = now - timedelta(days=initial_days_back)
        print(f"[delta] First run - pulling from {pull_from.date()} to {now.date()}")
    else:
        pull_from = last_pulled_at - timedelta(hours=OVERLAP_HOURS)
        print(
            f"[delta] Delta run - pulling from {pull_from.isoformat()} "
            f"(last pull: {last_pulled_at.isoformat()})"
        )

    return pull_from, now
