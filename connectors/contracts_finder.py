"""
connectors/contracts_finder.py — Contracts Finder (CF) API client.

Pulls PCR2015-era notices. CF is being phased out but still holds the historical
record for all procurements run under the old regime, and new call-offs on PCR2015
frameworks will continue to appear here until those frameworks expire.

API base: https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search
Auth: none required (public endpoint)
Format: OCDS 1.1.5 JSON

Key query params:
  publishedFrom  ISO 8601 — lower bound on publication date
  publishedTo    ISO 8601 — upper bound on publication date
  stages         comma-separated: planning, tender, award, implementation
  limit          page size (max 100)
  cursor         pagination token

Rate limit: 403 returned if exceeded — wait 5 minutes before retrying.
Note: 'published' date on CF is the date of the last edit OR first publication
in the current stage, not necessarily the original publication date.
"""

import time
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Iterator

BASE_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
REGIME = "PCR2015_CF"
PAGE_SIZE = 100  # CF max


class ContractsFinderClient:
    def __init__(self, raw_dir: Optional[Path] = None):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.raw_dir = raw_dir
        if self.raw_dir:
            self.raw_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, params: dict, retries: int = 3) -> dict:
        """Make a single GET with retry on rate limit (403 = wait 5 min)."""
        for attempt in range(retries):
            resp = self.session.get(BASE_URL, params=params, timeout=30)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 403:
                # CF returns 403 for rate limit exceeded (not 429)
                wait = 300  # 5 minutes per API docs
                print(f"  [CF] Rate limited (403). Waiting {wait}s...")
                time.sleep(wait)
                continue

            raise requests.HTTPError(
                f"[CF] HTTP {resp.status_code}: {resp.text[:300]}"
            )

        raise RuntimeError(f"[CF] Failed after {retries} attempts")

    def fetch_releases(
        self,
        published_from: Optional[datetime] = None,
        published_to: Optional[datetime] = None,
        stages: Optional[list[str]] = None,
        max_pages: int = 500,
    ) -> Iterator[list[dict]]:
        """
        Yield pages of OCDS releases from Contracts Finder.

        Args:
            published_from:  lower bound on CF publication date
            published_to:    upper bound (default: now)
            stages:          list from: planning, tender, award, implementation
                             (default: all stages)
            max_pages:       safety cap
        """
        params = {"limit": PAGE_SIZE}

        if published_from:
            params["publishedFrom"] = published_from.strftime("%Y-%m-%dT%H:%M:%S")
        if published_to:
            params["publishedTo"] = published_to.strftime("%Y-%m-%dT%H:%M:%S")
        if stages:
            params["stages"] = ",".join(stages)

        page = 0
        total_releases = 0
        cursor = None

        while page < max_pages:
            if cursor:
                params["cursor"] = cursor

            page_label = f"page {page+1}" + (f" (cursor={cursor[:20]}...)" if cursor else "")
            print(f"  [CF] Fetching {page_label}...", end=" ", flush=True)

            data = self._get(params)

            releases = data.get("releases", [])
            count = len(releases)
            total_releases += count
            print(f"{count} releases (total so far: {total_releases})")

            # Save raw response page
            if self.raw_dir and releases:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                out_path = self.raw_dir / f"cf_page{page+1:04d}_{ts}.json"
                with open(out_path, "w") as f:
                    json.dump(data, f, indent=2)

            if releases:
                yield releases

            # CF pagination: cursor embedded in links.next URL (same pattern as FTS)
            from urllib.parse import urlparse, parse_qs
            next_url = data.get("links", {}).get("next", "")
            if next_url:
                qs = parse_qs(urlparse(next_url).query)
                cursor = qs.get("cursor", [None])[0]
            else:
                cursor = None

            if not cursor or count == 0:
                print(f"  [CF] Pagination complete. Total releases: {total_releases}")
                break

            page += 1
            time.sleep(0.3)  # polite delay

        if page >= max_pages:
            print(f"  [CF] WARNING: hit max_pages={max_pages} limit.")

    def fetch_by_date_range(
        self,
        days_back: int = 365,
        stages: Optional[list[str]] = None,
    ) -> Iterator[list[dict]]:
        """
        Convenience wrapper: fetch all releases in the last N days.

        Like FTS, CF OCDS API does not support server-side CPV filtering.
        Pull by date range and filter client-side.
        """
        published_from = datetime.now(timezone.utc) - timedelta(days=days_back)
        print(f"  [CF] Fetching from {published_from.date()} (CPV filter applied client-side)")
        yield from self.fetch_releases(published_from=published_from, stages=stages)

    def fetch_delta(self, last_pulled_at: datetime) -> Iterator[list[dict]]:
        """Fetch only releases published since last_pulled_at (incremental sync)."""
        print(f"  [CF] Delta pull: releases published since {last_pulled_at.isoformat()}")
        yield from self.fetch_releases(published_from=last_pulled_at)
