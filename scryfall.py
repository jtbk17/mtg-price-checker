"""Card search and images from Scryfall's free public API
(https://scryfall.com/docs/api) — the canonical Magic card database.
No API key required.
"""

import logging
import time

import requests

logger = logging.getLogger("tcg-price-checker")

SEARCH_URL = "https://api.scryfall.com/cards/search"
AUTOCOMPLETE_URL = "https://api.scryfall.com/cards/autocomplete"
COLLECTION_URL = "https://api.scryfall.com/cards/collection"
COLLECTION_CHUNK_SIZE = 75  # the most ids the collection endpoint accepts per request
# Scryfall rate-limits /cards/collection more tightly than its general
# ~10 requests/second: 0.1s pacing got the nightly job (~130 requests for
# ~9,500 cards) a 429 Too Many Requests partway through.
REQUEST_PACING_SECONDS = 0.5
MAX_RATE_LIMIT_RETRIES = 5
HEADERS = {
    "User-Agent": "tcg-price-checker/1.0 (local personal project)",
    "Accept": "application/json",
}


class ScryfallError(Exception):
    pass


def search_cards(query):
    resp = requests.get(
        SEARCH_URL,
        headers=HEADERS,
        params={"q": query, "unique": "prints", "order": "released", "dir": "desc"},
        timeout=15,
    )
    if resp.status_code == 404:
        return []  # Scryfall uses 404 to mean "no cards matched"
    if not resp.ok:
        raise ScryfallError(f"Scryfall search failed ({resp.status_code}): {resp.text[:300]}")
    # No slicing here: even the most-reprinted staples (e.g. Lightning
    # Bolt's ~70 printings) fit in a single Scryfall page (up to 175
    # results), so a name-specific search never needs pagination. Slicing
    # to an arbitrary cap previously hid older printings behind the
    # release-date-desc ordering.
    return resp.json().get("data", [])


def autocomplete(query):
    """Card name suggestions for the given partial query, using Scryfall's
    dedicated typeahead endpoint (much faster and cheaper than running the
    full search on every keystroke). Fails soft (empty list) rather than
    raising, since a broken suggestion list shouldn't block searching."""
    try:
        resp = requests.get(AUTOCOMPLETE_URL, headers=HEADERS, params={"q": query}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except requests.RequestException as exc:
        logger.warning("Scryfall autocomplete failed: %s", exc)
        return []


def extract_image(card):
    image_uris = card.get("image_uris")
    if not image_uris and card.get("card_faces"):
        image_uris = card["card_faces"][0].get("image_uris")
    return (image_uris or {}).get("normal")


def get_cards_by_ids(scryfall_ids, on_progress=None):
    """Return {scryfall_id: card_object} via Scryfall's bulk collection
    endpoint (up to 75 ids per request). on_progress(phase, done, total),
    if given, is called after each chunk for progress reporting."""
    result = {}
    ids = list(dict.fromkeys(i for i in scryfall_ids if i))
    total = len(ids)
    for start in range(0, len(ids), COLLECTION_CHUNK_SIZE):
        if start > 0:
            time.sleep(REQUEST_PACING_SECONDS)
        chunk = ids[start : start + COLLECTION_CHUNK_SIZE]
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            resp = requests.post(
                COLLECTION_URL,
                headers=HEADERS,
                json={"identifiers": [{"id": i} for i in chunk]},
                timeout=30,
            )
            if resp.status_code != 429 or attempt == MAX_RATE_LIMIT_RETRIES:
                break
            try:
                wait = float(resp.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2 ** (attempt + 1)
            logger.info("Scryfall rate limit hit; retrying in %.0fs", wait)
            time.sleep(wait)
        resp.raise_for_status()
        for card in resp.json().get("data", []):
            result[card["id"]] = card
        if on_progress:
            on_progress("Fetching card data from Scryfall", min(start + COLLECTION_CHUNK_SIZE, total), total)
    return result
