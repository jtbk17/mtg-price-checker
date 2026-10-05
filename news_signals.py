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

It also pulls mtg_news_feed.EXCERPT_FEEDS (sites whose RSS feed only
exposes a short excerpt, not the full article) via
_process_excerpt_articles(): these need an actual headless-browser page
load per new article to get real text, unlike the free full-content
feeds, so dedup happens *before* that fetch (never pay for a page load
on an article already seen) and MAX_EXCERPT_FETCHES_PER_RUN caps how
many new pages get loaded in one run.
"""

import html
import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

import all_cards_history as ach
import claude_client
import db
import mtg_news
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
# mtg_news_feed.EXCERPT_FEEDS articles need a real headless-browser page
# load each (fetch_full_text), unlike the free full-content feeds — this
# bounds that cost per run. Articles beyond the cap are simply left
# unrecorded (not marked seen) so they're picked up on a later run
# rather than lost.
MAX_EXCERPT_FETCHES_PER_RUN = 5

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
    whole batch to a parse error instead of just the overflow.
    `card_names` is expected pre-ordered by first appearance in the
    article (see mtg_news_feed.find_mentioned_cards), so truncating
    keeps the names that are actually prominent rather than an
    arbitrary slice — truncation is logged either way. Returns [] if
    Claude isn't configured, the call fails, or the response can't be
    parsed — this is a supplementary feature, so it always fails
    soft."""
    if not claude_client.configured() or not card_names:
        return []

    if len(card_names) > MAX_CANDIDATES_PER_ARTICLE:
        # Previously silent — card_names is now ordered by first
        # appearance in the article (mtg_news_feed.find_mentioned_cards),
        # so this keeps whichever names are actually prominent rather
        # than an arbitrary slice, but it's still real signal being
        # dropped and worth knowing how often it happens.
        logger.info(
            "%r mentioned %d candidate card(s) — classifying only the first %d (by order of appearance)",
            article["title"],
            len(card_names),
            MAX_CANDIDATES_PER_ARTICLE,
        )
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


def _classify_and_record(article_id, article, known_names):
    """Matches, classifies, and records signals for one already-deduped
    article (which must have a 'text' key). Returns (classified_count,
    genuine_count)."""
    mentions = mtg_news_feed.find_mentioned_cards(article["text"], known_names)
    if not mentions:
        return 0, 0

    classified_count = 0
    genuine_count = 0
    for item in _classify_mentions(article, mentions):
        classified_count += 1
        signal_type = item.get("signal_type", "other")
        is_genuine = bool(item.get("is_genuine_interest"))
        db.record_news_signal(article_id, item["card_name"], signal_type, is_genuine)
        if is_genuine:
            genuine_count += 1
    return classified_count, genuine_count


def _dedup_excerpt_articles(items):
    """Checks EXCERPT_FEEDS metadata against db.news_articles and
    returns up to MAX_EXCERPT_FETCHES_PER_RUN genuinely-new (article_id,
    item) pairs. Deliberately stops *before* recording more than the
    cap — not after — so articles beyond it are left unrecorded and
    picked up on a later run, rather than marked seen without ever
    having their real text fetched."""
    new_items = []
    for item in items:
        if len(new_items) >= MAX_EXCERPT_FETCHES_PER_RUN:
            break
        published = item["published"].isoformat() if item["published"] else None
        article_id = db.record_article_if_new(item["link"], item["source"], item["title"], published)
        if article_id is None:
            continue  # already processed in an earlier run
        new_items.append((article_id, item))
    return new_items


def _process_excerpt_articles(known_names):
    """Handles mtg_news_feed.EXCERPT_FEEDS: dedup first (cheap, no
    browser), then fetch real body text only for articles that turned
    out to be new — a headless-browser page load is real per-article
    cost, unlike the free full-content feeds. Returns (classified_count,
    genuine_count, fetched_count)."""
    new_items = _dedup_excerpt_articles(mtg_news_feed.fetch_excerpt_articles())
    if not new_items:
        return 0, 0, 0

    # Same lazy-start, fail-soft pattern as market_alerts.py's shared
    # browser for mtg_news lookups: this is a supplementary feature, so
    # a browser that won't start just means skipping these articles
    # this run, not failing the whole scan.
    try:
        browser_session = mtg_news.BrowserSession()
        browser = browser_session.__enter__()
    except Exception as exc:
        logger.warning("Could not start a browser for excerpt-feed articles this run: %s", exc)
        browser_session, browser = None, None

    classified_count = genuine_count = fetched_count = 0
    try:
        if browser is not None:
            for article_id, item in new_items:
                text = mtg_news_feed.fetch_full_text(item["link"], browser)
                if not text:
                    continue
                fetched_count += 1
                c, g = _classify_and_record(article_id, {**item, "text": text}, known_names)
                classified_count += c
                genuine_count += g
    finally:
        if browser_session is not None:
            browser_session.__exit__(None, None, None)

    return classified_count, genuine_count, fetched_count


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
        c, g = _classify_and_record(article_id, article, known_names)
        classified_count += c
        genuine_count += g

    excerpt_classified, excerpt_genuine, excerpt_fetched = _process_excerpt_articles(known_names)
    classified_count += excerpt_classified
    genuine_count += excerpt_genuine

    logger.info(
        "Processed %d full-content article(s) + %d excerpt article(s) fetched, "
        "classified %d candidate mention(s), %d genuine signal(s) recorded",
        len(articles),
        excerpt_fetched,
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
    announcement can legitimately name many cards.

    Escaped throughout (unlike the tightly-controlled numeric/Scryfall
    fields used elsewhere in this app): article titles, card names, and
    source names are all third-party text here, and a stray "&" or "<"
    in one would otherwise produce malformed HTML that Telegram just
    rejects outright — market_alerts.py's _news_lines() hit exactly
    this and added escaping for the same reason."""
    lines = [
        f"<b>\U0001f52e Early signal: {html.escape(article['title'])}</b> ({html.escape(article['source'])})"
    ]
    for item in items:
        label = item["signal_type"].replace("_", " ").title()
        hint = _DIRECTION_HINTS.get(item["signal_type"])
        line = f"• {html.escape(item['card_name'])} — {html.escape(label)}"
        if hint:
            line += f" ({html.escape(hint)})"
        lines.append(line)
    lines.append(html.escape(article["link"]))
    lines.append("Not a confirmed move — just a heads-up based on recent coverage.")
    return "\n".join(lines)


EARLY_WARNING_DEDUP_DAYS = 7


def send_early_warnings():
    """Sends one bundled Telegram message per article for genuinely
    leading-type signals (recommender.LEADING_SIGNAL_TYPES) recorded
    *today*, for cards that aren't already a mover tonight and haven't
    already had an early warning for this same (card, signal_type)
    combo in the last EARLY_WARNING_DEDUP_DAYS — confirmed live that a
    single real event (one Secret Lair drop) can be covered by several
    separate articles on different nights, which would otherwise alert
    on the same cards repeatedly. Call this once — never inside a retry
    loop, same reasoning as market_alerts.py: a Telegram send here is
    irreversible, so re-running it on a retry just resends everything
    (this is exactly the bug a previous version of this pipeline hit for
    real — three nights' worth of alerts went out in one run)."""
    if db.already_ran_today("news_early_warnings_sent"):
        logger.info("Early warnings already sent today — skipping (safe to re-run the pipeline)")
        return

    today_movers = _load_today_movers()
    conn = db.get_connection()
    try:
        placeholders = ",".join("?" * len(recommender.LEADING_SIGNAL_TYPES))
        rows = conn.execute(
            f"""
            SELECT ns.id AS signal_id, ns.card_name, ns.signal_type,
                   na.id AS article_id, na.title, na.source, na.url
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
    deduped_count = 0
    for r in rows:
        if r["card_name"] in today_movers:
            continue
        if db.had_recent_early_warning(r["card_name"], r["signal_type"], EARLY_WARNING_DEDUP_DAYS):
            deduped_count += 1
            continue
        entry = by_article.setdefault(
            r["article_id"], {"title": r["title"], "source": r["source"], "link": r["url"], "items": []}
        )
        entry["items"].append({"signal_id": r["signal_id"], "card_name": r["card_name"], "signal_type": r["signal_type"]})

    message_count = 0
    signal_count = 0
    for article in by_article.values():
        telegram_notify.send_message(_early_warning_text(article, article["items"]))
        for item in article["items"]:
            db.mark_news_signal_sent(item["signal_id"])
        message_count += 1
        signal_count += len(article["items"])

    db.mark_ran_today("news_early_warnings_sent")
    logger.info(
        "Sent %d early-warning signal(s) across %d message(s) (%d skipped as recent repeats)",
        signal_count,
        message_count,
        deduped_count,
    )


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
