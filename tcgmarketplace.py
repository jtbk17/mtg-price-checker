"""Live prices from TheTCGMarketplace (thetcgmarketplace.com) — a
Singapore-based TCG marketplace. It publishes no API docs, but its own
web app calls a public REST API at thetcgmarketplace.com:3501 that
needs no authentication for browsing/pricing (confirmed by inspecting
that app's network calls: robots.txt is fully open, and there's no
bot-detection wall).

Pricing a card costs two calls: their search endpoint (POST
/product/filter, by name) returns candidate products to match against,
then that product's listings give its actual price.
Search results carry no set code or collector number fields, but each
one's image filename is Scryfall-style "<set>_<collector number> <name>"
(e.g. "sta_90 Demonic Tutor.webp") — and that's what tells apart
printings sharing a set name. Matching by set name alone used to grab
whichever printing came first: e.g. the $55 English Mystical Archive
Demonic Tutor (sta_27) for a ~$350 Japanese one (sta_90). Etched foils
are listed as their own separate products, so those are told apart by
finish too. Regular foil and nonfoil copies share one product, though,
as do every language's copies — so the price is worked out from the
product's individual listings (POST /product/listed_item_filter), keeping
only those in the card's own finish and language. Otherwise e.g. an
English foil Year of the Dragon Dragon Tempest got the $18 price of a
Simplified Chinese copy, when the cheapest English one was $43. Listings
in a worse condition than the card's are left out too.
The id itself never changes once found, so it's cached to disk
permanently; a short negative-cache TTL covers a card that isn't listed
*yet* without hammering the search endpoint for it every single day.
Listings are live marketplace data that can change throughout the
day, so they're cached only briefly (in memory, not on disk).
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


def _printing_number(result):
    """The collector number "90" from an image URL ending
    ".../sta_90%20Demonic%20Tutor.webp", or None if the result has no
    image named that way. Only the number is used: the set prefix doesn't
    always match Scryfall's set code (The List is "plist" here, "plst" on
    Scryfall), so the set is checked via _set_code() instead."""
    filename = unquote((result.get("image") or "").rsplit("/", 1)[-1])
    key = filename.split(" ", 1)[0]
    return key.split("_", 1)[1].lower() if "_" in key else None


def _set_code(result):
    """"HOC" from a product name like " [HOC] The One Ring (V2 - ...)"."""
    match = re.match(r"\s*\[([^\]]+)\]", result.get("name") or "")
    return match.group(1).lower() if match else None


def _is_etched(result):
    return "etched" in (result.get("crd_foil_type") or "").lower()


def _fetch_printing_number(product_id):
    """The collector number from product/single's card_id ("hoc_84") —
    the fallback for newer products whose image is named by an internal
    id (e.g. "aeee2b19-....webp") rather than the printing. Matters for
    e.g. The Hobbit Commander, whose surge foil One Ring (#84, ~$790) is
    its own printing next to the regular one (#44, ~$200)."""
    try:
        resp = _session.get(f"{BASE_URL}/product/single/{product_id}", timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("data")
    except requests.RequestException as exc:
        logger.warning("TheTCGMarketplace product fetch failed for id %s: %s", product_id, exc)
        return None
    card_id = (data[0].get("card_id") if data else None) or ""
    return card_id.split("_", 1)[1].lower() if "_" in card_id else None


def _pick_match(results, set_name, set_code=None, collector_number=None, etched=False):
    # Set names don't always agree with Scryfall's (e.g. Scryfall's "The
    # Hobbit Eternal" is "The Hobbit Commander" here), so a product's
    # bracketed set code counts as a match too.
    target_set = _normalize(set_name)
    same_set = [
        r for r in results
        if _normalize(r.get("setname")) == target_set or (set_code and _set_code(r) == set_code.lower())
    ]
    if set_code and collector_number:
        target_number = str(collector_number).lower()
        numbers = [_printing_number(r) for r in same_set]
        if target_number not in numbers and None in numbers:
            numbers = [n or _fetch_printing_number(r["id"]) for r, n in zip(same_set, numbers)]
        exact = [r for r, n in zip(same_set, numbers) if n == target_number]
        if not exact:
            # Couldn't confirm the exact printing. Still safe to fall back
            # on the set if it has only one printing listed — but not if
            # it has several (or any we couldn't identify), since guessing
            # between them is exactly what produced wildly wrong prices
            # before.
            if len(same_set) != 1 and (len(set(numbers)) != 1 or None in numbers):
                return None
            exact = same_set
        same_set = exact
    same_finish = [r for r in same_set if _is_etched(r) == etched]
    return (same_finish or same_set or [None])[0]


def lookup_args(card, finish=None, name=None, set_name=None, condition=None):
    """find_id()/get_price_for_card() arguments for a Scryfall card
    object, pinned to its exact printing, finish, language and condition.
    `finish` ("nonfoil", "foil", "etched") defaults to the card's only
    finish if it has just one, else nonfoil; `condition` (one of
    db.CONDITIONS) defaults to Near Mint."""
    if finish is None:
        finishes = card.get("finishes") or []
        finish = finishes[0] if len(finishes) == 1 else "nonfoil"
    return (
        name or card.get("name"),
        set_name or card.get("set_name"),
        card.get("set"),
        card.get("collector_number"),
        finish,
        card.get("lang"),
        condition or "Near Mint",
    )


def find_id(card_name, set_name, set_code=None, collector_number=None, finish=None, lang=None, condition=None):
    """Find TheTCGMarketplace's internal product id for this exact
    printing, or None if not found/not carried there. Pass set_code +
    collector_number whenever known; without them it can only match by
    set name, which is ambiguous for sets with several printings of the
    same card (see module docstring). `lang` and `condition` don't
    affect the match (every language and condition shares one product) —
    they're accepted only so a lookup_args() tuple can be passed straight
    through. Cached to disk —
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
    set_code, collector_number, finish, lang, condition), trailing ones optional — to
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


def _get_listings(product_id):
    """Every active listing for this product id — foil and nonfoil, all
    languages — or None if they couldn't be fetched. Cached briefly in
    memory only (not on disk): unlike the id, these actually change."""
    with _price_cache_lock:
        cached = _price_cache.get(product_id)
    if cached and (time.time() - cached["fetched_at"] < PRICE_CACHE_TTL_SECONDS):
        return cached["listings"]

    try:
        # foil "0" returns every listing; "1" would return foil ones only.
        resp = _session.post(
            f"{BASE_URL}/product/listed_item_filter",
            json={"product_id": str(product_id), "foil": "0"},
            timeout=15,
        )
        resp.raise_for_status()
        listings = resp.json().get("data", {}).get("data") or []
    except requests.RequestException as exc:
        logger.warning("TheTCGMarketplace listings fetch failed for id %s: %s", product_id, exc)
        return cached["listings"] if cached else None

    with _price_cache_lock:
        _price_cache[product_id] = {"listings": listings, "fetched_at": time.time()}
    return listings


def _is_foil_listing(listing):
    # "0"/"1" for most listings, but some surge foils say "Surge Foil".
    return str(listing.get("crd_foil") or "0") != "0"


# TheTCGMarketplace's condition grades, best first, and the app's names
# for the same grades (db.CONDITIONS — "Lightly Played" is their "SP").
CONDITION_RANKS = {"NM": 0, "SP": 1, "MP": 2, "HP": 3, "DMG": 4}
APP_CONDITION_RANKS = {
    "Near Mint": 0,
    "Lightly Played": 1,
    "Moderately Played": 2,
    "Heavily Played": 3,
    "Damaged": 4,
}


def get_price(product_id, foil=False, lang=None, condition=None):
    """Lowest live listing price for this product id among copies in the
    given finish (foil or not), language (a Scryfall language code like
    "en" or "ja"; any language if None) and condition or better (one of
    db.CONDITIONS; any condition if None). None if nothing matching is
    listed right now — deliberately not falling back to recent sale
    prices, which mix every language, finish and condition together."""
    prices = [price for price, _ in matching_listings(product_id, foil, lang, condition)]
    return min(prices) if prices else None


def matching_listings(product_id, foil=False, lang=None, condition=None):
    """[(price, quantity)] for this product id's live listings in the given
    finish, language and condition or better (see get_price())."""
    if product_id is None:
        return []
    worst_rank = APP_CONDITION_RANKS.get(condition) if condition else None
    matches = []
    for listing in _get_listings(product_id) or []:
        if listing.get("suspended") or _is_foil_listing(listing) != bool(foil):
            continue
        if lang and (listing.get("crd_language") or "").lower() != lang.lower():
            continue
        if worst_rank is not None:
            # An unrecognised grade is treated as worst, never as NM.
            rank = CONDITION_RANKS.get((listing.get("crd_condition") or "").upper(), len(CONDITION_RANKS))
            if rank > worst_rank:
                continue
        try:
            price = float(listing["price"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            quantity = max(int(listing.get("quantity") or 1), 1)
        except (TypeError, ValueError):
            quantity = 1
        matches.append((price, quantity))
    return matches


def _is_foil(finish):
    return finish in ("foil", "etched")


def get_price_for_card(
    card_name, set_name, set_code=None, collector_number=None, finish=None, lang=None, condition=None
):
    """Convenience: find_id + get_price in one call."""
    product_id = find_id(card_name, set_name, set_code, collector_number, finish)
    return get_price(product_id, foil=_is_foil(finish), lang=lang, condition=condition)


def prefetch_prices(lookups, max_workers=10, on_progress=None):
    """Warm the (short-lived) listings cache for many find_id() argument
    tuples (see prefetch_ids()) concurrently. Resolving ids via
    prefetch_ids() alone isn't enough to keep a batch of get_price_for_
    card() calls fast — each still fetches its product's listings once
    unless that's warmed too."""
    ids = list(dict.fromkeys(find_id(*tuple(lookup)[:5]) for lookup in lookups))
    ids = [i for i in ids if i is not None]
    total = len(ids)
    if not total:
        return
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_get_listings, product_id) for product_id in ids]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                logger.warning("Unexpected error prefetching a TheTCGMarketplace price: %s", exc)
            completed += 1
            if on_progress:
                on_progress("Fetching TheTCGMarketplace prices", completed, total)
