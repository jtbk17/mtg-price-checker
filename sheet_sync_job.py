"""Every-30-minutes sync of both inventory sheets (sheet-sync.yml), so a
sale, pull or new batch shows on the dashboard within half an hour rather
than at the nightly refresh.

Cheap when nothing changed: both sheets are fetched and fingerprinted
first, and only if that differs from the last successful run's
(sheet_fingerprint.txt, kept beside the database on the `data` release)
is the database downloaded. The sync itself goes through release_sync's
compare-and-swap, the same as the local app's edits, so it never needs
the nightly job's queue: GitHub keeps only the newest waiting run per
queue, and a job every 30 minutes there could cancel a waiting nightly
refresh. The nightly and hourly marketplace jobs upload without that check,
so one landing after this run would undo it; they delete the fingerprint
after uploading, which makes the next run here re-apply both sheets.
"""

import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path

import release_sync
import sheet_sync

logger = logging.getLogger("tcg-price-checker")

FINGERPRINT_FILE = "sheet_fingerprint.txt"
DB_FILE = "tcg_prices.db"


def sheets():
    """(owner, sheet id) for every sheet set up here."""
    pairs = [("Shared", sheet_sync.SHEET_ID), ("Joel", os.environ.get("JOEL_SHEET_ID"))]
    return [(owner, sheet_id) for owner, sheet_id in pairs if sheet_id]


def _local_digest(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    texts = {owner: sheet_sync.fetch_csv(sheet_id) for owner, sheet_id in sheets()}
    fingerprint = hashlib.sha256("".join(f"{o}\n{t}" for o, t in sorted(texts.items())).encode()).hexdigest()
    try:
        previous = Path(FINGERPRINT_FILE).read_text().strip()
    except OSError:
        previous = None
    if fingerprint == previous:
        logger.info("Sheet sync: neither sheet changed since the last sync")
        return

    def operation():
        changed = False
        for owner, text in texts.items():
            changed = sheet_sync.sync(owner, sheet_sync.parse_rows(text)) or changed
        if changed:
            import refresh_job  # the dashboard's watchlist.json, from the synced database

            refresh_job.export_snapshot()
        return changed

    changed = release_sync.sync(DB_FILE, operation, should_upload=lambda changed: changed, require_download=True)
    # Only remember this fingerprint once the database on the release really
    # holds the result (or nothing needed uploading), so a failed upload is
    # retried next run rather than skipped as "unchanged".
    if changed and release_sync._asset_digest(DB_FILE) != _local_digest(DB_FILE):
        raise RuntimeError("Database upload didn't go through — will retry next run")
    Path(FINGERPRINT_FILE).write_text(fingerprint)
    subprocess.run(["gh", "release", "upload", release_sync.RELEASE_TAG, FINGERPRINT_FILE, "--clobber"], check=True, timeout=60)
    logger.info("Sheet sync: %s", "synced and uploaded" if changed else "sheets changed but nothing to update")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except Exception:
        logger.exception("Sheet sync failed")
        sys.exit(1)
