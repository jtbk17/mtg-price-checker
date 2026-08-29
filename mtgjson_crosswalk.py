"""Maps a Scryfall card id to its MTGJSON uuid, which is the join key
Card Kingdom pricing (see cardkingdom.py) is keyed by. MTGJSON publishes
one small JSON file per set (e.g. M10.json) containing each card's
`identifiers.scryfallId`, so sets are fetched and cached lazily as the
app encounters them rather than downloading MTGJSON's full ~200MB
identifiers dump up front.
"""

import json
import logging
from pathlib import Path

import requests

logger = logging.getLogger("tcg-price-checker")

BASE_URL = "https://mtgjson.com/api/v5"
CACHE_FILE = Path(__file__).parent / "mtgjson_crosswalk_cache.json"
HEADERS = {"User-Agent": "tcg-price-checker/1.0 (local personal project)"}

_cache = None


def _load():
    global _cache
    if _cache is None:
        if CACHE_FILE.exists():
            _cache = json.loads(CACHE_FILE.read_text())
        else:
            _cache = {"fetched_sets": [], "map": {}}
    return _cache


def _save():
    CACHE_FILE.write_text(json.dumps(_cache))


def _fetch_set(set_code):
    cache = _load()
    code = set_code.upper()
    if code in cache["fetched_sets"]:
        return
    try:
        resp = requests.get(f"{BASE_URL}/{code}.json", headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("Could not fetch MTGJSON set %s: %s", code, exc)
        return

    for card in data.get("data", {}).get("cards", []):
        scryfall_id = card.get("identifiers", {}).get("scryfallId")
        if scryfall_id:
            cache["map"][scryfall_id] = card["uuid"]
    cache["fetched_sets"].append(code)
    _save()


def get_uuid(scryfall_id, set_code):
    cache = _load()
    if scryfall_id not in cache["map"]:
        _fetch_set(set_code)
    return cache["map"].get(scryfall_id)
