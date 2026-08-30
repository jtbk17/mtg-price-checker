"""Maps a Scryfall card id to its MTGJSON uuid, which is the join key
Card Kingdom pricing (see cardkingdom.py) is keyed by. MTGJSON publishes
one small JSON file per set (e.g. M10.json) containing each card's
`identifiers.scryfallId`, so sets are fetched and cached lazily as the
app encounters them rather than downloading MTGJSON's full ~200MB
identifiers dump up front.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

logger = logging.getLogger("tcg-price-checker")

BASE_URL = "https://mtgjson.com/api/v5"
CACHE_FILE = Path(__file__).parent / "mtgjson_crosswalk_cache.json"
HEADERS = {"User-Agent": "tcg-price-checker/1.0 (local personal project)"}

_cache = None
_lock = threading.Lock()
# A shared Session (thread-safe: urllib3's connection pool underneath
# handles concurrent use) reuses TCP/TLS connections across the many
# parallel requests prefetch_sets fires at the same host, instead of each
# one paying a fresh handshake.
_session = requests.Session()
_session.headers.update(HEADERS)
_session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=25, pool_maxsize=25))


def _load():
    global _cache
    with _lock:
        if _cache is None:
            if CACHE_FILE.exists():
                _cache = json.loads(CACHE_FILE.read_text())
            else:
                _cache = {"fetched_sets": [], "map": {}}
        return _cache


def _save():
    CACHE_FILE.write_text(json.dumps(_cache))


def _fetch_set(set_code):
    code = set_code.upper()
    cache = _load()
    with _lock:
        if code in cache["fetched_sets"]:
            return
    try:
        resp = _session.get(f"{BASE_URL}/{code}.json", timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("Could not fetch MTGJSON set %s: %s", code, exc)
        return

    with _lock:
        if code in cache["fetched_sets"]:
            return  # another thread fetched this concurrently while we were waiting
        for card in data.get("data", {}).get("cards", []):
            scryfall_id = card.get("identifiers", {}).get("scryfallId")
            if scryfall_id:
                cache["map"][scryfall_id] = card["uuid"]
        cache["fetched_sets"].append(code)
        _save()


def prefetch_sets(set_codes, max_workers=25):
    """Fetch multiple not-yet-cached sets concurrently. A search spanning
    many printings can touch dozens of sets never seen before — fetched
    sequentially (the old behavior) that's dozens of ~0.3-1.5s round trips
    added up serially (measured: 84s for one 69-printing search on a cold
    cache); fetched concurrently it's however long the slowest one takes."""
    cache = _load()
    with _lock:
        already_fetched = set(cache["fetched_sets"])
    codes = {c.upper() for c in set_codes if c} - already_fetched
    if not codes:
        return
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_fetch_set, codes))


def get_uuid(scryfall_id, set_code):
    cache = _load()
    if scryfall_id not in cache["map"]:
        _fetch_set(set_code)
    return cache["map"].get(scryfall_id)
