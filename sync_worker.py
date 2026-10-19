"""
sync_worker.py — Background sync orchestrator, called by the /admin/sync web endpoint.

Runs ingest.py --sync (live API delta) then the extractor (--only-missing),
writing output to sync.log. Removes sync.lock on completion/failure.
"""

import datetime
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
LOG  = BASE / "sync.log"
LOCK = BASE / "sync.lock"


def log(msg: str):
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}\n"
    print(line, end="")
    with open(LOG, "a") as f:
        f.write(line)


def run(cmd: list[str], label: str) -> int:
    log(f"--- {label} ---")
    with open(LOG, "a") as f:
        result = subprocess.run(cmd, stdout=f, stderr=f, text=True)
    return result.returncode


try:
    LOCK.write_text(str(os.getpid()))
    log(f"Sync started — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")

    rc1 = run([sys.executable, str(BASE / "ingest.py"), "--sync"], "ingest --sync")

    if rc1 != 0:
        log(f"ingest.py exited with code {rc1} — extraction will still run on any stored releases")

    rc2 = run([sys.executable, str(BASE / "db" / "extractor.py"), "--only-missing"], "extractor --only-missing")

    if rc2 != 0:
        log(f"extractor.py exited with code {rc2}")

    log("Sync complete.")

except Exception as e:
    with open(LOG, "a") as f:
        f.write(f"\n[sync_worker] Unhandled error: {e}\n")

finally:
    LOCK.unlink(missing_ok=True)
