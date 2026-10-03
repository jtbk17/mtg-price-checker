"""Pulls full-content RSS feeds directly from known MTG news/strategy
sites — no per-card search, no Google News redirect, no headless
browser needed here: these sites' own WordPress feeds embed each
article's complete text via <content:encoded>, so this is a plain, free
HTTP fetch. This is the "news -> cards" half of the pipeline (see
news_signals.py): scan a day's worth of articles once, rather than
searching per card (infeasible at a 9,000+ card watchlist).

Card-mention matching runs against the full set of known card names
from the all-cards history database — since these sources are already
MTG-dedicated publications, a name match here doesn't have the
ambiguity problem mtg_news.py had to work around (a generic word like
"Island", or a crossover card sharing its name with a real person/
character, showing up in a totally unrelated context): the article
itself guarantees MTG relevance, only the specific interpretation of a
card mention still needs judgment (that's what Claude classification
in news_signals.py is for).
"""

import html as html_module
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

logger = logging.getLogger("tcg-price-checker")

FEEDS = [
    ("MTG Rocks", "https://mtgrocks.com/feed/"),
    ("Draftsim", "https://draftsim.com/feed/"),
    ("Star City Games", "https://articles.starcitygames.com/feed/"),
]
HEADERS = {"User-Agent": "tcg-price-checker/1.0 (personal project)"}
TIMEOUT = 15
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"

TAG_RE = re.compile(r"<[^>]+>")
# Card names under this length (e.g. "Opt", "Shock") are common enough
# words that a substring match is nearly meaningless signal even with a
# word-boundary check — not worth the false-positive rate.
MIN_NAME_LENGTH = 4


def fetch_recent_articles():
    """Return [{source, title, link, published, text}] for every item
    across all known feeds, newest first within each feed — callers
    should dedupe against already-seen URLs (db.record_article_if_new)
    before doing anything expensive with these. Never raises: a feed
    that's down is logged and skipped, not fatal to the others."""
    articles = []
    for source, feed_url in FEEDS:
        try:
            resp = requests.get(feed_url, timeout=TIMEOUT, headers=HEADERS)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as exc:
            logger.warning("Could not fetch feed %r: %s", source, exc)
            continue

        for item in root.findall("./channel/item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            if not title or not link:
                continue
            pub_date_text = item.findtext("pubDate")
            try:
                published = parsedate_to_datetime(pub_date_text) if pub_date_text else None
            except (TypeError, ValueError):
                published = None

            content_el = item.find(CONTENT_NS)
            raw_html = content_el.text if content_el is not None and content_el.text else (
                item.findtext("description") or ""
            )
            text = html_module.unescape(TAG_RE.sub(" ", raw_html))
            text = re.sub(r"\s+", " ", text).strip()

            articles.append(
                {"source": source, "title": title, "link": link, "published": published, "text": text}
            )
    return articles


def find_mentioned_cards(text, known_names, require_multiword=True):
    """Returns the subset of `known_names` that appear as a whole-word
    match in `text` (case-insensitive). A cheap substring pre-filter
    (fast, exact-match C implementation) runs first so the slower regex
    word-boundary check — needed so "Bolt" doesn't match inside
    "Boltwing" — only ever runs on names that already passed it.

    `require_multiword` defaults on because single-word card names
    collide heavily with ordinary MTG vocabulary: live testing against
    real articles found "Exile", "Sacrifice", "Flash", "Lifelink",
    "Treasure", "Library", and "Landfall" all matching constantly as
    keyword/rules terms, not as deliberate references to the handful of
    cards that happen to share those one-word names — while multi-word
    names ("Darklight Phoenix", "Goblin Bombardment", "Surgical
    Extraction") came through clean in the same test. Turning this off
    would need a real false-positive filter (e.g. a keyword blocklist)
    to be worth the extra recall."""
    text_lower = text.lower()
    found = []
    for name in known_names:
        if len(name) < MIN_NAME_LENGTH:
            continue
        if require_multiword and " " not in name:
            continue
        name_lower = name.lower()
        if name_lower not in text_lower:
            continue
        if re.search(rf"\b{re.escape(name_lower)}\b", text_lower):
            found.append(name)
    return found
