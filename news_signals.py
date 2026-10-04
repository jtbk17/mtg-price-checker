"""Scans known MTG news/strategy sites (mtg_news_feed.py) for articles
mentioning cards by name, and classifies which mentions are genuine
signals about that card (vs. incidental decklist noise) using Claude on
a small daily batch of already-filtered candidates.

check_for_signals() does NOT send anything to Telegram — it just
records what it finds. Most of that (recommender.VALIDATION_SIGNAL_TYPES
— price_movement, deck_tech_feature, combo_discovery) only ever feeds
recommender.py's model as a feature (db.had_recent_news_signal), since
those types tend to appear alongside or after a move and are only
useful for scoring a mover that's already been detected. An earlier
version alerted directly on signal type regardless of this distinction,
which meant the same real event covered by multiple sites could fire
multiple messages, and even a bundled one-message-per-article version
was still more noise than a trained model silently factoring the same
validation signal in.

LEADING_SIGNAL_TYPES (spoiler_preview, banned_restricted,
reprint_announcement) are different: they tend to *precede* a move and
hint at a future direction, for a card that may not be a mover yet —
recommender.py has no mover to attach that hint to. send_early_warnings()
covers exactly that gap: a bundled, once-a-day Telegram alert for
leading-type signals on cards that AREN'T already in tonight's
movers.json (those already get a reactive alert, with the leading
signal already factored into its confidence score). It must be called
once, separately from check_for_signals(), after all_cards_history.py
has written today's movers.json — see nightly-refresh.yml: like
market_alerts.py, a Telegram send here is irreversible, so it's never
inside the retry-safe loop check_for_signals() runs in.

Deliberately avoids the "card -> news" shape (search per watchlist
card) that mtg_news.py uses for mover alerts — at ~9,000+ tracked cards
that doesn't scale, where scanning a day's worth of articles does.

check_for_signals() runs dedup'd against db.news_articles so a re-run
of the pipeline (e.g. after a git-push conflict) never re-classifies
the same article twice — harmless to redo if it ever did, since nothing
in that function is an irreversible external action.
"""

import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

import all_cards_history as ach
import claude_client
import db
import mtg_news_feed
import recommender
import telegram_notify

logger = logging.getLogger("tcg-price-checker")

MOVERS_FILE = Path(__file__).parent / "docs" / "movers.json"
_DIRECTION_HINTS = {
    "reprint_announcement": "often means more supply is coming — price may actually drop",
    "banned_restricted": "bans/restrictions can crash the affected card or lift its rivals — direction isn't automatic",
    "spoiler_preview": "a new card reveal — too early to know which way this goes",
}

MAX_TEXT_CHARS = 3000
MAX_CANDIDATES_PER_ARTICLE = 25  # also keeps the classification response bounded (see OUTPUT_TOKENS_PER_CANDIDATE)
OUTPUT_TOKENS_PER_CANDIDATE = 40  # headroom per candidate for max_tokens, not an exact measurement
BASE_OUTPUT_TOKENS = 200

SYSTEM_PROMPT = """You help a Magic: The Gathering price-tracking tool figure out which card mentions in a news/strategy article are genuine signals about that card, versus incidental noise (e.g. one entry in a long decklist with no special emphasis).

You will be given an article's title and text, plus a list of candidate card names found in it. For EACH candidate, decide:

- is_genuine_interest: true if the article specifically highlights, discusses, or draws attention to this card as notable — e.g. "powers this combo", "key piece", "surprisingly good", "spiking in price", "newly spoiled/revealed", "getting banned/restricted", "being reprinted". False if it's just one of many cards listed without special emphasis (a plain decklist entry, a passing mention, a card listed among many deal/product bundle contents).

- signal_type: exactly one of:
  "combo_discovery" (a new deck tech, combo, or synergy piece is highlighted)
  "spoiler_preview" (a new card reveal/preview)
  "banned_restricted" (a banned/restricted/suspended announcement)
  "reprint_announcement" (a reprint, Secret Lair, or similar announcement)
  "price_movement" (the article is explicitly about a price spike/drop)
  "deck_tech_feature" (the card is a notable build-around/highlight within a deck)
  "routine_mention" (no special signal — just listed)
  "other"

Respond with ONLY a JSON array, one object per candidate card name given, each with exactly these keys: "card_name", "is_genuine_interest", "signal_type". No markdown fences, no explanation outside the JSON array."""


def _load_known_card_names():
    """Known card names come from the all-cards history database (see
    all_cards_history.py) rather than a separately maintained list."""
    db_path = ach.db_path_for_year(date.today().year)
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        return [
            row[0]
            for row in conn.execute("SELECT DISTINCT name FROM cards WHERE name IS NOT NULL AND is_token = 0")
        ]
    finally:
        conn.close()


def _strip_markdown_fence(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: -len("```")]
    return text.strip()


def _classify_mentions(article, card_names):
    """Returns [{"card_name", "is_genuine_interest", "signal_type"}, ...]
    — only for names Claude was actually given, ignoring anything it
    hallucinates beyond that. Capped at MAX_CANDIDATES_PER_ARTICLE (an
    article with more mentions than that is almost always a long
    decklist/roundup where most are routine anyway) both to bound cost
    and because live testing found a fixed max_tokens too small for a
    busy article silently truncated the JSON mid-string, losing the
    whole batch to a parse error instead of just the overflow. Returns
    [] if Claude isn't configured, the call fails, or the response can't
    be parsed — this is a supplementary feature, so it always fails
    soft."""
    if not claude_client.configured() or not card_names:
        return []

    card_names = card_names[:MAX_CANDIDATES_PER_ARTICLE]
    prompt = (
        f"Article title: {article['title']}\n"
        f"Article source: {article['source']}\n"
        f"Article text: {article['text'][:MAX_TEXT_CHARS]}\n\n"
        f"Candidate card names mentioned: {json.dumps(card_names)}"
    )
    try:
        client = claude_client.get_client()
        response = client.messages.create(
            model=claude_client.MODEL,
            max_tokens=BASE_OUTPUT_TOKENS + OUTPUT_TOKENS_PER_CANDIDATE * len(card_names),
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        parsed = json.loads(_strip_markdown_fence(text))
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict) and item.get("card_name") in card_names]
    except Exception as exc:
        logger.warning("Claude classification failed for %r: %s", article["title"], exc)
        return []


def check_for_signals():
    """Scans and classifies new articles, recording a news_signals row
    per classified mention for recommender.py to use. Returns nothing —
    this is pure data collection, not an alert."""
    known_names = _load_known_card_names()
    if not known_names:
        logger.info("No known card names available (all-cards db missing?) — skipping news signal scan")
        return

    articles = mtg_news_feed.fetch_recent_articles()
    classified_count = 0
    genuine_count = 0

    for article in articles:
        published = article["published"].isoformat() if article["published"] else None
        article_id = db.record_article_if_new(article["link"], article["source"], article["title"], published)
        if article_id is None:
            continue  # already processed in an earlier run

        mentions = mtg_news_feed.find_mentioned_cards(article["text"], known_names)
        if not mentions:
            continue

        for item in _classify_mentions(article, mentions):
            classified_count += 1
            signal_type = item.get("signal_type", "other")
            is_genuine = bool(item.get("is_genuine_interest"))
            db.record_news_signal(article_id, item["card_name"], signal_type, is_genuine)
            if is_genuine:
                genuine_count += 1

    logger.info(
        "Processed %d article(s), classified %d candidate mention(s), %d genuine signal(s) recorded",
        len(articles),
        classified_count,
        genuine_count,
    )


def _load_today_movers():
    """Card names already flagged as a mover tonight (see
    all_cards_history.py's movers.json) — early warnings skip these: a
    reactive mover alert already covers them, with the leading signal
    already factored into its confidence score (recommender.py), so a
    separate early-warning message would just be redundant."""
    if not MOVERS_FILE.exists():
        return set()
    try:
        data = json.loads(MOVERS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {
        mover["name"]
        for key in ("daily_gainers", "daily_losers", "weekly_gainers", "weekly_losers")
        for mover in data.get(key, [])
    }


def _early_warning_text(article, items):
    """One message per article, not per card — the same reason
    market_alerts.py-adjacent bundling exists elsewhere here: a single
    announcement can legitimately name many cards."""
    lines = [f"<b>\U0001f52e Early signal: {article['title']}</b> ({article['source']})"]
    for item in items:
        label = item["signal_type"].replace("_", " ").title()
        hint = _DIRECTION_HINTS.get(item["signal_type"])
        line = f"• {item['card_name']} — {label}"
        if hint:
            line += f" ({hint})"
        lines.append(line)
    lines.append(article["link"])
    lines.append("Not a confirmed move — just a heads-up based on recent coverage.")
    return "\n".join(lines)


def send_early_warnings():
    """Sends one bundled Telegram message per article for genuinely
    leading-type signals (recommender.LEADING_SIGNAL_TYPES) recorded
    *today*, for cards that aren't already a mover tonight. Call this
    once — never inside a retry loop, same reasoning as
    market_alerts.py: a Telegram send here is irreversible, and the
    "already sent today" guard it writes to tcg_prices.db would
    otherwise get wiped by a `git reset --hard` on a later retry,
    causing a resend (this is exactly the bug a previous version of
    this pipeline hit for real)."""
    if db.already_ran_today("news_early_warnings_sent"):
        logger.info("Early warnings already sent today — skipping (safe to re-run the pipeline)")
        return

    today_movers = _load_today_movers()
    conn = db.get_connection()
    try:
        placeholders = ",".join("?" * len(recommender.LEADING_SIGNAL_TYPES))
        rows = conn.execute(
            f"""
            SELECT ns.card_name, ns.signal_type, na.id AS article_id, na.title, na.source, na.url
            FROM news_signals ns
            JOIN news_articles na ON na.id = ns.article_id
            WHERE ns.is_genuine_interest = 1
              AND ns.signal_type IN ({placeholders})
              AND date(na.seen_at) = date('now')
            """,
            recommender.LEADING_SIGNAL_TYPES,
        ).fetchall()
    finally:
        conn.close()

    by_article = {}
    for r in rows:
        if r["card_name"] in today_movers:
            continue
        entry = by_article.setdefault(
            r["article_id"], {"title": r["title"], "source": r["source"], "link": r["url"], "items": []}
        )
        entry["items"].append({"card_name": r["card_name"], "signal_type": r["signal_type"]})

    message_count = 0
    signal_count = 0
    for article in by_article.values():
        telegram_notify.send_message(_early_warning_text(article, article["items"]))
        message_count += 1
        signal_count += len(article["items"])

    db.mark_ran_today("news_early_warnings_sent")
    logger.info("Sent %d early-warning signal(s) across %d message(s)", signal_count, message_count)


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(level=logging.INFO)
    db.init_db()
    if "--early-warnings" in sys.argv:
        send_early_warnings()
    else:
        check_for_signals()
