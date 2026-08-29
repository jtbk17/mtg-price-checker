"""Polls Telegram for taps on market-alert feedback buttons (see
market_alerts.py) and records them as labeled training data for the
recommender. Meant to run frequently (e.g. every ~20 minutes) via its own
scheduled GitHub Actions workflow — waiting for the next nightly run to
register a tap would feel broken.
"""

import logging

import db
import telegram_notify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tcg-price-checker")


def poll():
    offset = db.get_state("telegram_update_offset")
    offset = int(offset) + 1 if offset else None

    updates = telegram_notify.get_updates(offset=offset)
    if not updates:
        logger.info("No new Telegram updates")
        return

    latest_update_id = None
    handled = 0
    for update in updates:
        latest_update_id = update["update_id"]
        callback = update.get("callback_query")
        if not callback:
            continue

        parts = callback.get("data", "").split(":")
        if len(parts) != 3 or parts[0] != "fb":
            continue
        _, rec_id, verdict = parts
        feedback = "good" if verdict == "good" else "bad"

        db.set_recommendation_feedback(int(rec_id), feedback)
        handled += 1
        telegram_notify.answer_callback_query(
            callback["id"], "Thanks — noted!" if feedback == "good" else "Thanks — marked as false positive"
        )

        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        if chat_id and message_id:
            mark = "✅ Marked: good pick" if feedback == "good" else "❌ Marked: false positive"
            telegram_notify.edit_message_text(chat_id, message_id, f"{message.get('text', '')}\n\n{mark}")

        logger.info("Recorded feedback for recommendation %s: %s", rec_id, feedback)

    if latest_update_id is not None:
        db.set_state("telegram_update_offset", str(latest_update_id))
    logger.info("Processed %d update(s), %d feedback tap(s)", len(updates), handled)


if __name__ == "__main__":
    db.init_db()
    poll()
