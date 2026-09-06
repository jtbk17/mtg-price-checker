"""Live prices from TheTCGMarketplace (thetcgmarketplace.com) — a
Singapore-based TCG marketplace. It publishes no API docs, but its own
web app calls a public REST API at thetcgmarketplace.com:3501 that
needs no authentication for browsing/pricing (confirmed by inspecting
that app's network calls: robots.txt is fully open, and there's no
bot-detection wall).

Matching a card to their internal numeric product id costs two calls,
since their search endpoint (POST /product/filter, by name) returns
each result's human-readable set name but not its set code — so the
right printing is matched by (card name, set name) against the search
results, then GET /product/single/<id> fetches that one's actual price.
The id itself never changes once found, so it's cached to disk
permanently; a short negative-cache TTL covers a card that isn't listed
*yet* without hammering the search endpoint for it every single day.
The price is a live marketplace value that can change throughout the
day, so it's cached only briefly (in memory, not on disk).
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

logger = logging.getLogger("tcg-price-checker")

BASE_URL = "https://thetcgmarketplace.com:3501"
MTG_CATEGORY_ID = "3"
HEADERS = {"User-Agent": "tcg-price-checker/1.0 (local personal project)"}

ID_CACHE_FILE = Path(__file__).parent / "tcgmarketplace_id_cache.json"
NEGATIVE_CACHE_TTL_SECONDS = 7 * 24 * 3600  # re-check an unlisted card weekly, not every run
PRICE_CACHE_TTL_SECONDS = 4 * 3600

_id_cache = None
_id_cache_lock = threading.Lock()
_price_cache = {}
_price_cache_lock = threading.Lock()

_session = requests.Session()
_session.headers.update(HEADERS)
_session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=25, pool_maxsize=25))


def _load_id_cache():
    global _id_cache
    with _id_cache_lock:
        if _id_cache is None:
            _id_cache = json.loads(ID_CACHE_FILE.read_text()) if ID_CACHE_FILE.exists() else {}
        return _id_cache


def _save_id_cache():
    ID_CACHE_FILE.write_text(json.dumps(_id_cache))


def _cache_key(card_name, set_name):
    return f"{card_name}\x1f{set_name}"


def _normalize(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _search(card_name):
    resp = _session.post(
        f"{BASE_URL}/product/filter",
        json={"category_id": MTG_CATEGORY_ID, "name": card_name, "page": 1, "item": 50},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data", {}).get("data", [])


def find_id(card_name, set_name):
    """Find TheTCGMarketplace's internal product id for this exact card
    name + set, or None if not found/not carried there. Cached to disk —
    positive matches permanently, negative ones for NEGATIVE_CACHE_TTL_
    SECONDS — so repeated lookups (nightly refresh, re-imports) don't
    re-search every time."""
    cache = _load_id_cache()
    key = _cache_key(card_name, set_name)

    with _id_cache_lock:
        entry = cache.get(key)
    if entry is not None:
        found_id, cached_at = entry
        if found_id is not None:
            return found_id
        if time.time() - cached_at < NEGATIVE_CACHE_TTL_SECONDS:
            return None

    try:
        results = _search(card_name)
    except requests.RequestException as exc:
        logger.warning("TheTCGMarketplace search failed for %r: %s", card_name, exc)
        return None

    target_set = _normalize(set_name)
    match = next((r for r in results if _normalize(r.get("setname")) == target_set), None)
    found_id = match["id"] if match else None

    with _id_cache_lock:
        cache[key] = [found_id, time.time()]
        _save_id_cache()
    return found_id


def prefetch_ids(card_set_pairs, max_workers=10, on_progress=None):
    """Resolve (card_name, set_name) -> internal id for many cards
    concurrently, warming the id cache before a batch of get_price_for_
    card() calls. on_progress(phase, done, total), if given, is called
    as each pair finishes (order not guaranteed — these run concurrently)."""
    pairs = list(dict.fromkeys(card_set_pairs))
    total = len(pairs)
    if not total:
        return
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(find_id, name, set_name) for name, set_name in pairs]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                logger.warning("Unexpected error resolving a TheTCGMarketplace id: %s", exc)
            completed += 1
            if on_progress:
                on_progress("Looking up TheTCGMarketplace prices", completed, total)


def get_price(product_id):
    """Current reference price for this internal product id: their live
    lowest active listing, falling back to the most recent day's average
    sale price if nothing's currently listed. None if the id is None or
    the card has neither. Cached briefly in memory only (not on disk) —
    unlike the id, this is expected to actually change."""
    if product_id is None:
        return None
    with _price_cache_lock:
        cached = _price_cache.get(product_id)
    if cached and (time.time() - cached["fetched_at"] < PRICE_CACHE_TTL_SECONDS):
        return cached["price"]

    try:
        resp = _session.get(f"{BASE_URL}/product/single/{product_id}", timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("data")
    except requests.RequestException as exc:
        logger.warning("TheTCGMarketplace price fetch failed for id %s: %s", product_id, exc)
        return cached["price"] if cached else None

    price = None
    if data:
        entry = data[0]
        price_from = entry.get("price_from")
        day1 = entry.get("day1")
        if price_from not in (None, ""):
            price = float(price_from)
        elif day1 not in (None, ""):
            price = float(day1)

    with _price_cache_lock:
        _price_cache[product_id] = {"price": price, "fetched_at": time.time()}
    return price


def get_price_for_card(card_name, set_name):
    """Convenience: find_id + get_price in one call."""
    return get_price(find_id(card_name, set_name))


def prefetch_prices(product_ids, max_workers=10, on_progress=None):
    """Warm the (short-lived) price cache for many product ids
    concurrently. Resolving ids via prefetch_ids() alone isn't enough to
    keep a batch of get_price_for_card() calls fast — each still hits
    product/single once per id unless that's warmed too."""
    ids = list(dict.fromkeys(i for i in product_ids if i is not None))
    total = len(ids)
    if not total:
        return
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(get_price, product_id) for product_id in ids]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                logger.warning("Unexpected error prefetching a TheTCGMarketplace price: %s", exc)
            completed += 1
            if on_progress:
                on_progress("Fetching TheTCGMarketplace prices", completed, total)
