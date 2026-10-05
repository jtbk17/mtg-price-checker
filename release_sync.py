"""Compare-and-swap helper for syncing a SQLite db file to a GitHub
Release asset (see app.py's watchlist-edit sync and record_feedback.py —
both can write to the same tcg_prices.db release asset the nightly
pipeline also writes to).

tcg_prices.db used to be committed straight into git, which gave a free
conflict signal: a rejected push meant someone else had moved main
since you last pulled, so you could fetch, replay your (idempotent)
write on top of the fresh state, and retry. It stopped being
git-tracked once it grew past GitHub's 100MB per-file push limit (see
nightly-refresh.yml) and became a release asset instead — but a plain
download-then-upload against a release asset has no such signal: two
writers racing just means whichever uploads last silently wins,
discarding the other's write.

sync() rebuilds that same conflict signal using the asset's content
digest: note it before downloading, do the (idempotent) local write,
then check the digest again right before uploading. If it's unchanged,
nobody else touched the asset while we worked, so it's safe to upload.
If it changed, someone else's write landed in between — pull their
version, redo our write on top of it, and check again.

Only safe for operations that are themselves idempotent to replay (a
single INSERT/UPDATE, or a batch of them) — never for anything with an
irreversible external side effect (a Telegram send), since replaying
would repeat that side effect. The nightly pipeline's alert-sending
steps deliberately don't go through this for exactly that reason; see
nightly-refresh.yml's own comments.
"""

import json
import logging
import subprocess

logger = logging.getLogger("tcg-price-checker")

RELEASE_TAG = "data"
MAX_RETRIES = 3
_TIMEOUT = 60


def _asset_digest(asset_name):
    """Current sha256 digest of `asset_name` in the release, or None if
    the release or asset doesn't exist yet."""
    try:
        result = subprocess.run(
            ["gh", "release", "view", RELEASE_TAG, "--json", "assets"],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        assets = json.loads(result.stdout)["assets"]
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, KeyError):
        return None
    return next((a["digest"] for a in assets if a["name"] == asset_name), None)


def _download(asset_name):
    # Non-fatal if the asset doesn't exist yet (e.g. a brand new release)
    # — leaves whatever local file (or lack of one) is already there,
    # same as the `|| true` pattern the workflows use for this.
    subprocess.run(
        ["gh", "release", "download", RELEASE_TAG, "--pattern", asset_name, "--clobber"],
        capture_output=True, timeout=_TIMEOUT,
    )


def _upload(asset_name):
    view = subprocess.run(["gh", "release", "view", RELEASE_TAG], capture_output=True, timeout=30)
    if view.returncode != 0:
        subprocess.run(
            [
                "gh", "release", "create", RELEASE_TAG,
                "--title", "Price history data",
                "--notes", "Persistent tcg_prices.db and all-cards price history "
                           "(kept out of git history; not the same as source code releases)",
            ],
            capture_output=True, timeout=30, check=True,
        )
    subprocess.run(
        ["gh", "release", "upload", RELEASE_TAG, asset_name, "--clobber"],
        capture_output=True, timeout=_TIMEOUT, check=True,
    )


def sync(asset_name, operation, max_retries=MAX_RETRIES):
    """Downloads `asset_name` fresh, runs `operation()` (which mutates
    the local file of that name and returns its own result), then
    uploads it back — redoing the whole cycle if another writer's
    upload landed on the server while we were working. Returns
    `operation()`'s result from whichever attempt's upload actually
    went through, or the last attempt's result if every retry raced and
    none of the uploads could be confirmed conflict-free (logged, not
    raised — this is best-effort sync, not a transaction)."""
    result = None
    for attempt in range(1, max_retries + 1):
        digest_before = _asset_digest(asset_name)
        _download(asset_name)
        result = operation()

        if _asset_digest(asset_name) != digest_before:
            logger.info(
                "%s changed on the server while we were working (attempt %d/%d) — "
                "pulling the newer version and redoing the update",
                asset_name, attempt, max_retries,
            )
            continue

        try:
            _upload(asset_name)
            return result
        except subprocess.SubprocessError as exc:
            logger.warning("Upload of %s failed (attempt %d/%d): %s", asset_name, attempt, max_retries, exc)

    logger.warning("Could not sync %s after %d attempt(s) — you may need to sync manually", asset_name, max_retries)
    return result
