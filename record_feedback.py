"""Records one piece of recommendation feedback. Invoked with a
recommendation id and a verdict ("good"/"bad") — triggered by the
Cloudflare Worker (cloudflare_worker.js) via a repository_dispatch event
the instant someone taps a feedback button on Telegram, not on a schedule.
"""

import logging
import sys

import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tcg-price-checker")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        logger.error("Usage: record_feedback.py <rec_id> <good|bad>")
        sys.exit(1)

    rec_id = int(sys.argv[1])
    feedback = "good" if sys.argv[2] == "good" else "bad"

    db.init_db()
    db.set_recommendation_feedback(rec_id, feedback)
    logger.info("Recorded feedback for recommendation %d: %s", rec_id, feedback)
