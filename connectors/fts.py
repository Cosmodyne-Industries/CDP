"""
connectors/fts.py — Find a Tender Service (FTS) API client.

Pulls OCDS release packages from the PA23-era enhanced FTS (launched Feb 2025).
Also handles PCR2015 notices still published on FTS (pre-Feb 2025 procurements).

API base: https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages
Auth: CDP-Api-Key header (obtained from your FTS organisation profile)
Format: OCDS 1.1.5 JSON

Key query params:
  updatedFrom   ISO 8601 — releases updated at or after this time (delta sync)
  updatedTo     ISO 8601 — releases updated before this time
  stages        comma-separated OCDS stages: planning,tender,award,implementation
  cursor        pagination token returned in each response

Rate limit: back off on 429 responses (Retry-After header).
"""

import time
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Iterator

BASE_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
REGIME = "PA23_FTS"


class FTSClient:
    def __init__(self, raw_dir: Optional[Path] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
        })
        self.raw_dir = raw_dir
        if self.raw_dir:
            self.raw_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, params: dict, retries: int = 8) -> dict:
        """Make a single GET request with retry/backoff on rate limit or timeout."""
        for attempt in range(retries):
            try:
                resp = self.session.get(BASE_URL, params=params, timeout=120)
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
                wait = 15 * (attempt + 1)
                print(f"  [FTS] Network error ({e.__class__.__name__}), attempt {attempt+1}/{retries}. Retrying in {wait}s...")
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                print(f"  [FTS] Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue

            if resp.status_code == 401:
                raise ValueError(
                    "[FTS] 401 Unauthorised - check your CDP-Api-Key is correct and "
                    "your organisation account is active on FTS."
                )

            # Other errors — log and raise
            raise requests.HTTPError(
                f"[FTS] HTTP {resp.status_code}: {resp.text[:300]}"
            )

        raise RuntimeError(f"[FTS] Failed after {retries} attempts")

    def fetch_releases(
        self,
        updated_from: Optional[datetime] = None,
        updated_to: Optional[datetime] = None,
        stages: Optional[list[str]] = None,
        max_pages: int = 500,
    ) -> Iterator[list[dict]]:
        """
        Yield pages of OCDS releases.

        Each page is a list of release dicts. The caller decides what to do with them
        (store, inspect, etc.). This keeps memory usage low for large pulls.

        Args:
            updated_from:  only fetch releases updated at or after this time
            updated_to:    only fetch releases updated before this time
            stages:        list of OCDS stages to filter (default: all)
            max_pages:     safety cap on pagination to avoid runaway pulls
        """
        params = {}

        if updated_from:
            params["updatedFrom"] = updated_from.strftime("%Y-%m-%dT%H:%M:%S")
        if updated_to:
            params["updatedTo"] = updated_to.strftime("%Y-%m-%dT%H:%M:%S")
        if stages:
            params["stages"] = ",".join(stages)

        page = 0
        total_releases = 0
        cursor = None

        while page < max_pages:
            if cursor:
                params["cursor"] = cursor

            page_label = f"page {page+1}" + (f" (cursor={cursor[:20]}...)" if cursor else "")
            print(f"  [FTS] Fetching {page_label}...", end=" ", flush=True)

            data = self._get(params)

            releases = data.get("releases", [])
            count = len(releases)
            total_releases += count
            print(f"{count} releases (total so far: {total_releases})")

            # Save raw response page if a raw directory is configured
            if self.raw_dir and releases:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                out_path = self.raw_dir / f"fts_page{page+1:04d}_{ts}.json"
                with open(out_path, "w") as f:
                    json.dump(data, f, indent=2)

            if releases:
                yield releases

            # Pagination: FTS returns a 'links.next' URL or cursor in response
            # Check both patterns seen in documentation
            links = data.get("links", {})
            next_url = links.get("next", "")
            if next_url:
                # Extract cursor from the next URL query string
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(next_url).query)
                cursor = qs.get("cursor", [None])[0]
            else:
                # Also check for cursor directly in response root (some API versions)
                cursor = data.get("cursor")

            if not cursor or count == 0:
                print(f"  [FTS] Pagination complete. Total releases: {total_releases}")
                break

            page += 1
            # Polite delay between pages
            time.sleep(0.5)

        if page >= max_pages:
            print(f"  [FTS] WARNING: hit max_pages={max_pages} limit. Pull may be incomplete.")

    def fetch_by_cpv(
        self,
        cpv_code: str,
        days_back: int = 365,
        stages: Optional[list[str]] = None,
    ) -> Iterator[list[dict]]:
        """
        Convenience wrapper: fetch releases updated in the last N days.

        NOTE: The FTS OCDS API does not support server-side CPV filtering —
        the CPV filter must be applied client-side after fetching. This method
        fetches all releases in the date window; the caller filters by CPV.

        For targeted pulls, use a narrow date window and filter locally.
        Alternatively pull all data once and query the local store by CPV.
        """
        updated_from = datetime.now(timezone.utc) - timedelta(days=days_back)
        print(f"  [FTS] Fetching all stages from {updated_from.date()} (CPV filter applied client-side)")
        yield from self.fetch_releases(updated_from=updated_from, stages=stages)

    def fetch_delta(self, last_pulled_at: datetime) -> Iterator[list[dict]]:
        """Fetch only releases updated since last_pulled_at (for incremental sync)."""
        print(f"  [FTS] Delta pull: releases updated since {last_pulled_at.isoformat()}")
        yield from self.fetch_releases(updated_from=last_pulled_at)
