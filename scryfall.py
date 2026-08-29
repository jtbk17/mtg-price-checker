"""Card search and images from Scryfall's free public API
(https://scryfall.com/docs/api) — the canonical Magic card database.
No API key required.
"""

import logging

import requests

logger = logging.getLogger("tcg-price-checker")

SEARCH_URL = "https://api.scryfall.com/cards/search"
HEADERS = {
    "User-Agent": "tcg-price-checker/1.0 (local personal project)",
    "Accept": "application/json",
}
RESULT_LIMIT = 20


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
    return resp.json().get("data", [])[:RESULT_LIMIT]


def extract_image(card):
    image_uris = card.get("image_uris")
    if not image_uris and card.get("card_faces"):
        image_uris = card["card_faces"][0].get("image_uris")
    return (image_uris or {}).get("normal")
