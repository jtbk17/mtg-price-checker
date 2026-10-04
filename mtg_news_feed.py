"""Pulls RSS feeds directly from known MTG news/strategy sites — no
per-card search. This is the "news -> cards" half of the pipeline (see
news_signals.py): scan a day's worth of articles once, rather than
searching per card (infeasible at a 9,000+ card watchlist).

Two tiers of source, by what their feed actually gives you:

- FEEDS: WordPress sites whose feeds embed each article's complete
  text via <content:encoded> — a plain, free HTTP fetch, no headless
  browser needed. fetch_recent_articles() handles these.
- EXCERPT_FEEDS: sites whose feed only exposes a short excerpt (no
  <content:encoded>). Getting real body text out of these needs an
  actual page load — fetch_excerpt_articles() + fetch_full_text(),
  which is real per-article cost, unlike the free tier above.

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
# These sites' feeds only expose a 1-2 sentence excerpt, not the full
# article (confirmed live — Quiet Speculation's <description> caps at
# ~800 chars, no <content:encoded> at all), so getting real body text
# out of them needs an actual page load (fetch_full_text, below), not
# a plain feed parse. That's real per-article cost (a headless browser
# page, not a free HTTP GET), so callers should only pay it for
# articles not already seen — see news_signals.py's dedup-before-fetch
# ordering and MAX_EXCERPT_FETCHES_PER_RUN cap.
EXCERPT_FEEDS = [
    ("Quiet Speculation", "https://www.quietspeculation.com/feed/"),
]
HEADERS = {"User-Agent": "tcg-price-checker/1.0 (personal project)"}
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
TIMEOUT = 15
PAGE_LOAD_TIMEOUT_MS = 20000
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"

TAG_RE = re.compile(r"<[^>]+>")
# Card names under this length (e.g. "Opt", "Shock") are common enough
# words that a substring match is nearly meaningless signal even with a
# word-boundary check — not worth the false-positive rate.
MIN_NAME_LENGTH = 4


def _parse_feed_items(source, feed_url):
    """Parses `feed_url`'s RSS items into [{source, title, link,
    published, raw_html}] — raw_html is the <content:encoded> body if
    the feed has one, else the (possibly excerpt-only) <description>.
    Never raises: a feed that's down is logged and skipped, not fatal
    to the others."""
    try:
        resp = requests.get(feed_url, timeout=TIMEOUT, headers=HEADERS)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        logger.warning("Could not fetch feed %r: %s", source, exc)
        return []

    items = []
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
        items.append({"source": source, "title": title, "link": link, "published": published, "raw_html": raw_html})
    return items


def fetch_recent_articles():
    """Return [{source, title, link, published, text}] for every item
    across the full-content feeds (FEEDS), newest first within each
    feed — callers should dedupe against already-seen URLs
    (db.record_article_if_new) before doing anything expensive with
    these."""
    articles = []
    for source, feed_url in FEEDS:
        for item in _parse_feed_items(source, feed_url):
            text = html_module.unescape(TAG_RE.sub(" ", item["raw_html"]))
            text = re.sub(r"\s+", " ", text).strip()
            articles.append(
                {"source": item["source"], "title": item["title"], "link": item["link"],
                 "published": item["published"], "text": text}
            )
    return articles


def fetch_excerpt_articles():
    """Return [{source, title, link, published}] — no `text` key, unlike
    fetch_recent_articles(): EXCERPT_FEEDS' own feeds only expose a
    short excerpt, not real body text, so there's nothing useful to
    extract here. Callers that want the real article need
    fetch_full_text(link, browser) — and should call it only for
    articles not already seen, since it's a real page load, not a free
    feed parse."""
    articles = []
    for source, feed_url in EXCERPT_FEEDS:
        for item in _parse_feed_items(source, feed_url):
            articles.append(
                {"source": item["source"], "title": item["title"], "link": item["link"],
                 "published": item["published"]}
            )
    return articles


def fetch_full_text(url, browser):
    """Loads `url` in `browser` (a Playwright browser, e.g. from
    mtg_news.BrowserSession) and returns its visible body text, or None
    if the page can't be loaded. Publishers that block headless
    browsers outright (a 403, or a real response that's just an empty
    page shell) are treated as "can't verify, skip" — the same
    treatment mtg_news.py uses for sites that block it there, never a
    target to defeat."""
    page = None
    try:
        page = browser.new_page(user_agent=BROWSER_USER_AGENT)
        page.goto(url, wait_until="load", timeout=PAGE_LOAD_TIMEOUT_MS)
        text = page.inner_text("body")
    except Exception as exc:
        logger.info("Could not load excerpt article %r: %s", url, exc)
        return None
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def find_mentioned_cards(text, known_names, require_multiword=True):
    """Returns the subset of `known_names` that appear as a whole-word
    match in `text` (case-insensitive), ordered by where each first
    appears in the text — not the arbitrary order `known_names` happens
    to come in (that's unrelated to relevance; it's whatever order the
    source database returns rows in). This matters because callers that
    truncate a long list (news_signals.py caps candidates per article
    for cost) end up keeping whichever names are actually prominent in
    the article — usually mentioned early — rather than an arbitrary
    slice. A cheap substring pre-filter (fast, exact-match C
    implementation, and a free source of the position) runs first so
    the slower regex word-boundary check — needed so "Bolt" doesn't
    match inside "Boltwing" — only ever runs on names that already
    passed it.

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
    found = []  # (first-occurrence position, name)
    for name in known_names:
        if len(name) < MIN_NAME_LENGTH:
            continue
        if require_multiword and " " not in name:
            continue
        name_lower = name.lower()
        position = text_lower.find(name_lower)
        if position == -1:
            continue
        if re.search(rf"\b{re.escape(name_lower)}\b", text_lower):
            found.append((position, name))
    found.sort(key=lambda pair: pair[0])
    return [name for _, name in found]
