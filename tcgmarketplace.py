"""Live prices from TheTCGMarketplace (thetcgmarketplace.com) — a
Singapore-based TCG marketplace. It publishes no API docs, but its own
web app calls a public REST API at thetcgmarketplace.com:3501 that
needs no authentication for browsing/pricing (confirmed by inspecting
that app's network calls: robots.txt is fully open, and there's no
bot-detection wall).

Matching a card to their internal numeric product id costs two calls:
their search endpoint (POST /product/filter, by name) returns candidate
products, then GET /product/single/<id> fetches that one's actual price.
Search results carry no set code or collector number fields, but each
one's image filename is Scryfall-style "<set>_<collector number> <name>"
(e.g. "sta_90 Demonic Tutor.webp") — and that's what tells apart
printings sharing a set name. Matching by set name alone used to grab
whichever printing came first: e.g. the $55 English Mystical Archive
Demonic Tutor (sta_27) for a ~$350 Japanese one (sta_90). Etched foils
are listed as their own separate products, so those are told apart by
finish too. Regular foil and nonfoil copies share one product, though,
and product/single's price covers nonfoil listings only unless asked
for ?foil=1 — so a foil card needs that, or it gets the (usually much
cheaper) nonfoil price.
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
from urllib.parse import unquote

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


def _cache_key(card_name, set_name, set_code=None, collector_number=None, etched=False):
    key = f"{card_name}\x1f{set_name}"
    if set_code and collector_number:
        key += f"\x1f{set_code.lower()}_{collector_number}" + ("\x1fetched" if etched else "")
    return key


def _normalize(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


SEARCH_PAGE_SIZE = 100  # the most the endpoint returns per page
SEARCH_MAX_PAGES = 10


def _search(card_name):
    # Heavily-reprinted staples (e.g. Sol Ring: ~130 products) don't fit
    # in one page, and the printing we want can be on any of them.
    results = []
    for page in range(1, SEARCH_MAX_PAGES + 1):
        resp = _session.post(
            f"{BASE_URL}/product/filter",
            json={"category_id": MTG_CATEGORY_ID, "name": card_name, "page": page, "item": SEARCH_PAGE_SIZE},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json().get("data", {}).get("data", [])
        results.extend(batch)
        if len(batch) < SEARCH_PAGE_SIZE:
            break
    return results


def _printing_key(result):
    """The "sta_90" in an image URL ending ".../sta_90%20Demonic%20Tutor.webp",
    or None if the result has no image to read it from."""
    filename = unquote((result.get("image") or "").rsplit("/", 1)[-1])
    key = filename.split(" ", 1)[0]
    return key.lower() if "_" in key else None


def _is_etched(result):
    return "etched" in (result.get("crd_foil_type") or "").lower()


def _pick_match(results, set_name, set_code=None, collector_number=None, etched=False):
    target_set = _normalize(set_name)
    same_set = [r for r in results if _normalize(r.get("setname")) == target_set]
    if set_code and collector_number:
        target_key = f"{set_code}_{collector_number}".lower()
        exact = [r for r in same_set if _printing_key(r) == target_key]
        if not exact:
            # Couldn't confirm the exact printing. Still safe to fall back
            # on the set name if that set has only one printing listed —
            # but not if it has several, since guessing between them is
            # exactly what produced wildly wrong prices before.
            if len({_printing_key(r) for r in same_set}) != 1:
                return None
            exact = same_set
        same_set = exact
    same_finish = [r for r in same_set if _is_etched(r) == etched]
    return (same_finish or same_set or [None])[0]


def lookup_args(card, finish=None, name=None, set_name=None):
    """find_id()/get_price_for_card() arguments for a Scryfall card
    object, pinned to its exact printing and finish. `finish` ("nonfoil",
    "foil", "etched") defaults to the card's only finish if it has just
    one, else nonfoil."""
    if finish is None:
        finishes = card.get("finishes") or []
        finish = finishes[0] if len(finishes) == 1 else "nonfoil"
    return (
        name or card.get("name"),
        set_name or card.get("set_name"),
        card.get("set"),
        card.get("collector_number"),
        finish,
    )


def find_id(card_name, set_name, set_code=None, collector_number=None, finish=None):
    """Find TheTCGMarketplace's internal product id for this exact
    printing, or None if not found/not carried there. Pass set_code +
    collector_number whenever known; without them it can only match by
    set name, which is ambiguous for sets with several printings of the
    same card (see module docstring). Cached to disk —
    positive matches permanently, negative ones for NEGATIVE_CACHE_TTL_
    SECONDS — so repeated lookups (nightly refresh, re-imports) don't
    re-search every time."""
    etched = finish == "etched"
    cache = _load_id_cache()
    key = _cache_key(card_name, set_name, set_code, collector_number, etched)

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

    match = _pick_match(results, set_name, set_code, collector_number, etched)
    found_id = match["id"] if match else None

    with _id_cache_lock:
        cache[key] = [found_id, time.time()]
        _save_id_cache()
    return found_id


def prefetch_ids(lookups, max_workers=10, on_progress=None):
    """Resolve many find_id() argument tuples — (card_name, set_name,
    set_code, collector_number, finish), trailing ones optional — to
    internal ids concurrently, warming the id cache before a batch of
    get_price_for_card() calls. on_progress(phase, done, total), if given, is called
    as each pair finishes (order not guaranteed — these run concurrently)."""
    pairs = list(dict.fromkeys(tuple(lookup) for lookup in lookups))
    total = len(pairs)
    if not total:
        return
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(find_id, *lookup) for lookup in pairs]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                logger.warning("Unexpected error resolving a TheTCGMarketplace id: %s", exc)
            completed += 1
            if on_progress:
                on_progress("Looking up TheTCGMarketplace prices", completed, total)


def get_price(product_id, foil=False):
    """Current reference price for this internal product id, for foil or
    nonfoil copies: their live lowest active listing, falling back to the
    most recent day's average sale price if nothing's currently listed.
    None if the id is None or the card has neither. Cached briefly in
    memory only (not on disk) — unlike the id, this is expected to
    actually change."""
    if product_id is None:
        return None
    cache_key = (product_id, bool(foil))
    with _price_cache_lock:
        cached = _price_cache.get(cache_key)
    if cached and (time.time() - cached["fetched_at"] < PRICE_CACHE_TTL_SECONDS):
        return cached["price"]

    try:
        resp = _session.get(
            f"{BASE_URL}/product/single/{product_id}", params={"foil": 1 if foil else 0}, timeout=15
        )
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
        _price_cache[cache_key] = {"price": price, "fetched_at": time.time()}
    return price


def _is_foil(finish):
    return finish in ("foil", "etched")


def get_price_for_card(card_name, set_name, set_code=None, collector_number=None, finish=None):
    """Convenience: find_id + get_price in one call."""
    return get_price(find_id(card_name, set_name, set_code, collector_number, finish), foil=_is_foil(finish))


def prefetch_prices(lookups, max_workers=10, on_progress=None):
    """Warm the (short-lived) price cache for many find_id() argument
    tuples (see prefetch_ids()) concurrently. Resolving ids via
    prefetch_ids() alone isn't enough to keep a batch of get_price_for_
    card() calls fast — each still hits product/single once per id and
    finish unless that's warmed too."""
    keys = []
    for lookup in lookups:
        lookup = tuple(lookup)
        finish = lookup[4] if len(lookup) > 4 else None
        keys.append((find_id(*lookup), _is_foil(finish)))
    keys = list(dict.fromkeys(k for k in keys if k[0] is not None))
    total = len(keys)
    if not total:
        return
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(get_price, product_id, foil) for product_id, foil in keys]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                logger.warning("Unexpected error prefetching a TheTCGMarketplace price: %s", exc)
            completed += 1
            if on_progress:
                on_progress("Fetching TheTCGMarketplace prices", completed, total)
