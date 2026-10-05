"""Records one piece of recommendation feedback. Invoked with a
recommendation id and a verdict ("good"/"bad") — triggered by the
Cloudflare Worker (cloudflare_worker.js) via a repository_dispatch event
the instant someone taps a feedback button on Telegram, not on a schedule.
"""

import logging
import sys

import db
import release_sync

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tcg-price-checker")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        logger.error("Usage: record_feedback.py <rec_id> <good|bad>")
        sys.exit(1)

    rec_id = int(sys.argv[1])
    feedback = "good" if sys.argv[2] == "good" else "bad"

    def _record():
        db.init_db()
        db.set_recommendation_feedback(rec_id, feedback)
        return feedback

    # A single UPDATE keyed by rec_id is idempotent to replay, so this is
    # safe to compare-and-swap against the nightly pipeline's own writes
    # to the same tcg_prices.db release asset (see release_sync.py).
    release_sync.sync("tcg_prices.db", _record)
    logger.info("Recorded feedback for recommendation %d: %s", rec_id, feedback)
