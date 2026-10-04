"""Scans known MTG news/strategy sites (mtg_news_feed.py) for articles
mentioning cards by name, and classifies which mentions are genuine
signals about that card (vs. incidental decklist noise) using Claude on
a small daily batch of already-filtered candidates.

This does NOT send anything to Telegram — the signals recorded here
feed recommender.py's model as a feature (db.had_recent_news_signal),
so a mover's "Model confidence" score can reflect whether there was
real news coverage of it recently, rather than becoming its own
separate alert stream. An earlier version did alert directly on
"actionable" signal types, but that meant the same real event covered
by multiple sites could fire multiple messages, and even one bundled
message per article was still more noise than a trained model silently
factoring the same information in.

Deliberately avoids the "card -> news" shape (search per watchlist
card) that mtg_news.py uses for mover alerts — at ~9,000+ tracked cards
that doesn't scale, where scanning a day's worth of articles does.

Runs once nightly, dedup'd against db.news_articles so a re-run of the
pipeline (e.g. after a git-push conflict) never re-classifies the same
article twice — harmless to redo if it ever did, since nothing here is
an irreversible external action anymore.
"""

import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

import all_cards_history as ach
import claude_client
import db
import mtg_news_feed

logger = logging.getLogger("tcg-price-checker")

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


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(level=logging.INFO)
    db.init_db()
    check_for_signals()
