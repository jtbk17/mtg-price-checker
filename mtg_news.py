"""Finds recent news coverage for a card via Google News' public RSS
search — free, no API key, no LLM involved — so market-mover alerts can
link to a plausible "why" without paying for an AI service to guess at
one.

Two filtering passes, both learned from backtesting against real mover
history, not assumed upfront:

1. Recency. Google's own relevance ranking for a quoted card-name search
   falls back to generic, long-stale set-guide content when nothing
   specific exists for that card — confirmed by searching an obscure
   bulk card and getting back only 2+-year-old "limited guide" articles
   that never mention it by name. RECENCY_DAYS rejects that fallback.

2. Body verification. Even within the recency window, a headline-level
   match can be a pure coincidence — confirmed cases: "Island" (the MTG
   card) matched a Red Dead Redemption article about an in-game island,
   "Zack Fair" (an FF7 crossover card) matched a Final Fantasy VII
   Rebirth deals roundup. Google's own redirect links can't be resolved
   with a plain HTTP request (the redirect is client-side JS, not an
   HTTP 3xx), so a headless browser (Playwright) is used to load the
   real destination and confirm the card's exact name actually appears
   in its body text before trusting the match.

Some publishers block headless browsers outright (observed: a 403, and
a real response body that's just an empty <html><body></body></html>
shell — the same kind of bot-detection this project has already decided
not to try to defeat for other sources, e.g. PriceCharting's Cloudflare
challenge). Those are treated as "can't verify" and skipped, same as a
genuine non-match — this trades some recall for not chasing anti-bot
measures, and for not ever showing a link that turned out to be wrong.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

logger = logging.getLogger("tcg-price-checker")

# Body-text verification gives no real discriminative power for these —
# they're common English words that show up constantly in totally
# unrelated articles (confirmed live: "Island" matched a Red Dead
# Redemption article and a Hawaiian powwow story, neither about Magic).
# A specific valuable printing (e.g. a championship-deck promo) can still
# be a genuine mover; it just can't be verified this way, so skip
# straight to no result rather than risk a confident-looking wrong link.
BASIC_LAND_NAMES = {"island", "mountain", "forest", "swamp", "plains"}

RSS_URL = "https://news.google.com/rss/search"
RECENCY_DAYS = 45
MAX_RESULTS = 2
MAX_CANDIDATES_TO_VERIFY = 6  # headline-level hits to try before giving up on this card
SEARCH_TIMEOUT = 10
PAGE_LOAD_TIMEOUT_MS = 20000
REDIRECT_POLL_SECONDS = 8
HEADERS = {"User-Agent": "tcg-price-checker/1.0 (personal project)"}
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _search_candidates(card_name):
    """Headline-level RSS hits within RECENCY_DAYS, newest first — not
    yet verified against the real article body."""
    try:
        resp = requests.get(
            RSS_URL,
            params={"q": f'"{card_name}" magic the gathering', "hl": "en-US", "gl": "US", "ceid": "US:en"},
            timeout=SEARCH_TIMEOUT,
            headers=HEADERS,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        logger.warning("MTG news search failed for %r: %s", card_name, exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS)
    candidates = []
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
        candidates.append((pub_date, title, link))
        if len(candidates) >= MAX_CANDIDATES_TO_VERIFY:
            break
    return candidates


def _clean_title(title, source_hint):
    """Google appends " - <source>" to every title itself; strip it so it
    isn't shown twice alongside the caller's own source label. The
    human-readable site name in the title ("MTG Rocks") rarely matches
    the bare domain exactly ("mtgrocks.com"), so compare normalized
    (lowercased, punctuation/spaces/TLD stripped) forms instead of the
    raw strings."""
    if " - " not in title:
        return title
    prefix, _, suffix = title.rpartition(" - ")
    norm_suffix = re.sub(r"[^a-z0-9]", "", suffix.lower())
    norm_source = re.sub(r"[^a-z0-9]", "", (source_hint or "").lower())
    norm_source = re.sub(r"(com|net|org|co)$", "", norm_source)
    if norm_suffix and norm_source and (norm_suffix in norm_source or norm_source in norm_suffix):
        return prefix
    return title


def _verify(browser, card_name, google_link):
    """Load the Google redirect in a real (headless) browser, since the
    redirect itself only resolves via client-side JS, then confirm the
    card's exact name appears in the destination page's body text.
    Returns the real article URL if verified, or None — covering both a
    genuine non-match and a blocked/failed fetch alike, since neither
    should ever be reported as a found match."""
    page = None
    try:
        page = browser.new_page(user_agent=BROWSER_USER_AGENT)
        page.goto(google_link, wait_until="load", timeout=PAGE_LOAD_TIMEOUT_MS)
        # The redirect away from news.google.com is client-side JS, not an
        # HTTP 3xx, and isn't always fast — poll for it rather than trust a
        # fixed sleep. This matters for correctness, not just speed:
        # observed live, Google's own interstitial page renders a preview
        # of the target headline before redirecting, so reading the page
        # too early risks matching the card's name against Google's *own*
        # page rather than the real article.
        deadline = time.monotonic() + REDIRECT_POLL_SECONDS
        while "news.google.com" in page.url and time.monotonic() < deadline:
            page.wait_for_timeout(500)
        if "news.google.com" in page.url:
            logger.info("Google redirect never resolved for %r — skipping", card_name)
            return None
        real_url = page.url
        body = page.inner_text("body")
    except Exception as exc:
        logger.info("Could not load a news candidate for %r: %s", card_name, exc)
        return None
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    if card_name.lower() not in body.lower():
        return None
    return real_url


class BrowserSession:
    """Launches one headless browser for find_news() to reuse across many
    cards in a single run, instead of paying browser-startup cost (and
    OS process overhead) once per card — meaningful at market_alerts.py's
    scale of ~20-30 movers checked per night. Use as a context manager:

        with mtg_news.BrowserSession() as browser:
            for card_name in cards:
                find_news(card_name, browser=browser)

    find_news() still works without one (passing browser=None, the
    default) by opening and closing a one-off session internally — only
    worth sharing when calling it many times in a row."""

    def __init__(self):
        self._playwright = None
        self.browser = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=True)
        return self.browser

    def __exit__(self, exc_type, exc_value, traceback):
        if self.browser is not None:
            self.browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        return False


def find_news(card_name, max_results=MAX_RESULTS, browser=None):
    """Return up to `max_results` recent, verified {title, link, source,
    published} news items actually about this card, newest first, or []
    if there's nothing recent and verifiable (or on any error) — this is
    supplementary context, not a core feature, so it always fails soft.

    Pass `browser` (from BrowserSession) when calling this many times in
    a row, to reuse one browser instead of launching one per call."""
    if card_name.strip().lower() in BASIC_LAND_NAMES:
        return []

    candidates = _search_candidates(card_name)
    if not candidates:
        return []

    if browser is not None:
        return _verify_candidates(card_name, candidates, browser, max_results)

    try:
        with BrowserSession() as owned_browser:
            return _verify_candidates(card_name, candidates, owned_browser, max_results)
    except Exception as exc:
        logger.warning("News verification unavailable for %r: %s", card_name, exc)
        return []


def _verify_candidates(card_name, candidates, browser, max_results):
    results = []
    for pub_date, title, link in candidates:
        real_url = _verify(browser, card_name, link)
        if not real_url:
            continue
        source = urlparse(real_url).netloc.removeprefix("www.")
        results.append(
            {
                "title": _clean_title(title, source),
                "link": real_url,
                "source": source,
                "published": pub_date.date().isoformat(),
            }
        )
        if len(results) >= max_results:
            break
    return results
