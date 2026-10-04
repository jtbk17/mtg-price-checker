"""Sends a Telegram alert, with 'Good pick' / 'False positive' feedback
buttons, for every general-market mover found by all_cards_history.py's
nightly snapshot — not just watchlist cards. Each alert is logged as a
recommendation; feedback collected by poll_telegram_feedback.py trains
recommender.py's model to annotate future alerts with a confidence score.

Also links any recent news coverage found for the card (mtg_news.py),
so the alert hints at *why* it might be moving without needing a paid
LLM to guess at a reason.
"""

import html
import json
import logging
from pathlib import Path

import db
import mtg_news
import recommender
import telegram_notify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tcg-price-checker")

MOVERS_FILE = Path(__file__).parent / "docs" / "movers.json"


def _news_lines(news_items):
    """Formats found news items as HTML `<a>` lines to append to a
    mover's alert text, one per item, each prefixed with a blank line.
    Escaped (unlike the other fields in send_market_alerts, which come
    from the tightly-controlled Scryfall/MTGJSON pipeline) since article
    titles are third-party text — a stray "&" or "<" in one would
    otherwise produce malformed HTML and silently drop the whole alert."""
    return "".join(
        f"\n📰 <a href=\"{html.escape(n['link'])}\">{html.escape(n['title'])}</a> ({html.escape(n['source'] or 'news')})"
        for n in news_items
    )


SOURCES = [
    ("daily_gainers", "today"),
    ("weekly_gainers", "7-day trend"),
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

    # One shared browser for every news lookup this run, instead of
    # find_news() launching and tearing one down per card — meaningful at
    # ~20-30 movers/night. If Playwright/chromium isn't available at all,
    # fall back to None rather than letting that take down alert-sending
    # entirely: each find_news() call will then just fail the same way
    # on its own and return [], same as any other soft failure here.
    try:
        news_browser_session = mtg_news.BrowserSession()
        news_browser = news_browser_session.__enter__()
    except Exception as exc:
        logger.warning("Could not start a browser for news lookups this run: %s", exc)
        news_browser_session = None
        news_browser = None

    try:
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
                    set_name=mover.get("set_full_name", mover["set"]),
                    price_before=mover["price_before"],
                    price_now=mover["price_now"],
                    pct_change=mover["pct_change"],
                )

                confidence = recommender.score(model, mover["price_before"], mover["pct_change"], mover["name"])
                confidence_line = f"\nModel confidence: {confidence}% good pick" if confidence is not None else ""

                news_lines = _news_lines(mtg_news.find_news(mover["name"], browser=news_browser))

                text = (
                    f"<b>Market mover ({label})</b>\n"
                    f"{mover['name']} ({mover.get('set_full_name', mover['set'])}): "
                    f"${mover['price_before']:.2f} → ${mover['price_now']:.2f} "
                    f"(+{mover['pct_change']}%){confidence_line}{news_lines}"
                )
                buttons = [("👍 Good pick", f"fb:{rec_id}:good"), ("👎 False positive", f"fb:{rec_id}:bad")]
                sent = telegram_notify.send_photo_with_buttons(mover.get("image_url"), text, buttons)
                if sent:
                    chat_id, message_id = sent
                    db.set_recommendation_telegram_info(rec_id, chat_id, message_id)
                sent_count += 1
    finally:
        if news_browser_session is not None:
            news_browser_session.__exit__(None, None, None)

    db.mark_ran_today("market_alerts_sent")
    if sent_count:
        logger.info("Sent %d market alert(s)", sent_count)
    else:
        logger.info("No general-market movers today")


if __name__ == "__main__":
    db.init_db()
    send_market_alerts()
