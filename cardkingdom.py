"""Card Kingdom retail prices for Magic cards, sourced from MTGJSON's
AllPricesToday feed (https://mtgjson.com), which republishes Card Kingdom's
pricing keyed by MTGJSON card uuid. JustTCG returns that same uuid as
`mtgjsonId` on Magic cards, so it's used here as the join key.
"""

import gzip
import json
import logging
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("tcg-price-checker")

SOURCE_URL = "https://mtgjson.com/api/v5/AllPricesToday.json.gz"
CACHE_FILE = Path(__file__).parent / "cardkingdom_cache.json"
CACHE_TTL_SECONDS = 20 * 3600  # MTGJSON refreshes this feed roughly daily

_memory_cache = {"data": None, "loaded_at": 0}


def _download_and_distill():
    logger.info("Downloading Card Kingdom prices from MTGJSON...")
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "tcg-price-checker/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    full = json.loads(gzip.decompress(raw))

    distilled = {}
    for uuid, entry in full.get("data", {}).items():
        ck = entry.get("paper", {}).get("cardkingdom")
        if not ck:
            continue
        retail = ck.get("retail", {})
        buylist = ck.get("buylist", {})
        retail_normal = list(retail.get("normal", {}).values())
        retail_foil = list(retail.get("foil", {}).values())
        buylist_normal = list(buylist.get("normal", {}).values())
        buylist_foil = list(buylist.get("foil", {}).values())
        distilled[uuid] = {
            "retail_normal": retail_normal[-1] if retail_normal else None,
            "retail_foil": retail_foil[-1] if retail_foil else None,
            "buylist_normal": buylist_normal[-1] if buylist_normal else None,
            "buylist_foil": buylist_foil[-1] if buylist_foil else None,
        }

    CACHE_FILE.write_text(json.dumps(distilled))
    logger.info("Cached Card Kingdom prices for %d cards", len(distilled))
    return distilled


def _load():
    now = time.time()
    if _memory_cache["data"] is not None and (now - _memory_cache["loaded_at"] < CACHE_TTL_SECONDS):
        return _memory_cache["data"]

    if CACHE_FILE.exists() and (now - CACHE_FILE.stat().st_mtime < CACHE_TTL_SECONDS):
        data = json.loads(CACHE_FILE.read_text())
    else:
        try:
            data = _download_and_distill()
        except Exception as exc:  # network hiccup, stale site, etc.
            logger.warning("Could not refresh Card Kingdom prices: %s", exc)
            data = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

    _memory_cache["data"] = data
    _memory_cache["loaded_at"] = now
    return data


def get_prices(mtgjson_id, foil=False):
    """Return {"market": ..., "buylist": ...} Card Kingdom prices for a
    Magic card, or None if the card isn't in the feed (e.g. non-Magic
    cards, or cards Card Kingdom doesn't stock)."""
    if not mtgjson_id:
        return None
    entry = _load().get(mtgjson_id)
    if not entry:
        return None
    if foil:
        return {"market": entry["retail_foil"], "buylist": entry["buylist_foil"]}
    return {"market": entry["retail_normal"], "buylist": entry["buylist_normal"]}
