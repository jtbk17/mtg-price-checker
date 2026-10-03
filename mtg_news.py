"""Finds recent news coverage for a card via Google News' public RSS
search — free, no API key, no LLM involved — so market-mover alerts can
link to a plausible "why" without paying for an AI service to guess at
one.

Google's own relevance ranking for a quoted card-name search falls back
to generic, long-stale set-guide content when nothing specific exists
for that card: confirmed by searching an obscure bulk card and getting
back only 2+-year-old "limited guide" articles that never mention it by
name. A recency filter is what actually separates a real, timely hit
from that fallback noise — trying to parse true relevance out of the
headline text alone is unreliable, since even a genuinely relevant
article (e.g. about a card's commander deck) often doesn't put the
card's exact name in its title.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

logger = logging.getLogger("tcg-price-checker")

RSS_URL = "https://news.google.com/rss/search"
RECENCY_DAYS = 45
MAX_RESULTS = 2
TIMEOUT = 10
HEADERS = {"User-Agent": "tcg-price-checker/1.0 (personal project)"}


def find_news(card_name, max_results=MAX_RESULTS):
    """Return up to `max_results` recent {title, link, source, published}
    news items plausibly about this card, newest first, or [] if there's
    nothing recent enough (or on any error) — this is supplementary
    context, not a core feature, so it always fails soft."""
    try:
        resp = requests.get(
            RSS_URL,
            params={"q": f'"{card_name}" magic the gathering', "hl": "en-US", "gl": "US", "ceid": "US:en"},
            timeout=TIMEOUT,
            headers=HEADERS,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        logger.warning("MTG news lookup failed for %r: %s", card_name, exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS)
    results = []
    for item in root.findall("./channel/item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub_date_text = item.findtext("pubDate")
        if not title or not link or not pub_date_text:
            continue
        try:
            pub_date = parsedate_to_datetime(pub_date_text)
        except (TypeError, ValueError):
            continue
        if pub_date < cutoff:
            continue
        source_el = item.find("source")
        source = source_el.text if source_el is not None else None
        # Google appends " - <source>" to every title itself, so showing
        # `source` again alongside it would just repeat the same name.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")]
        results.append(
            {
                "title": title,
                "link": link,
                "source": source,
                "published": pub_date.date().isoformat(),
            }
        )
        if len(results) >= max_results:
            break
    return results
