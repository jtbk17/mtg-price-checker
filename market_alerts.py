"""Sends a Telegram alert, with 'Good pick' / 'False positive' feedback
buttons, for every general-market mover found by all_cards_history.py's
nightly snapshot — not just watchlist cards. Each alert is logged as a
recommendation; feedback collected by poll_telegram_feedback.py trains
recommender.py's model to annotate future alerts with a confidence score.
"""

import json
import logging
from pathlib import Path

import db
import recommender
import telegram_notify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tcg-price-checker")

MOVERS_FILE = Path(__file__).parent / "docs" / "movers.json"


SOURCES = [
    ("daily_gainers", "today"),
    ("trend_gainers", "88-day trend"),
]


def send_market_alerts():
    if db.already_ran_today("market_alerts_sent"):
        logger.info("Market alerts already sent today — skipping (safe to re-run the pipeline)")
        return
    if not MOVERS_FILE.exists():
        logger.info("No movers.json yet — nothing to alert on")
        return

    data = json.loads(MOVERS_FILE.read_text())
    model = recommender.train()

    sent_count = 0
    seen = set()  # dedupe a card that qualifies as both a daily and trend gainer tonight
    for key, label in SOURCES:
        for mover in data.get(key, []):
            dedupe_key = (mover["name"], mover["set"], mover["price_now"])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            rec_id = db.record_recommendation(
                card_name=mover["name"],
                set_name=mover["set"],
                price_before=mover["price_before"],
                price_now=mover["price_now"],
                pct_change=mover["pct_change"],
            )

            confidence = recommender.score(model, mover["price_before"], mover["pct_change"])
            confidence_line = f"\nModel confidence: {confidence}% good pick" if confidence is not None else ""

            text = (
                f"<b>Market mover ({label})</b>\n"
                f"{mover['name']} ({mover['set']}): ${mover['price_before']:.2f} → ${mover['price_now']:.2f} "
                f"(+{mover['pct_change']}%){confidence_line}"
            )
            buttons = [("👍 Good pick", f"fb:{rec_id}:good"), ("👎 False positive", f"fb:{rec_id}:bad")]
            sent = telegram_notify.send_message_with_buttons(text, buttons)
            if sent:
                chat_id, message_id = sent
                db.set_recommendation_telegram_info(rec_id, chat_id, message_id)
            sent_count += 1

    db.mark_ran_today("market_alerts_sent")
    if sent_count:
        logger.info("Sent %d market alert(s)", sent_count)
    else:
        logger.info("No general-market movers today")


if __name__ == "__main__":
    db.init_db()
    send_market_alerts()
