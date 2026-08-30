"""Look up any card's full price history from the all-cards database —
not just cards on your watchlist (used by the search results' "History"
button, keyed by the card's exact MTGJSON uuid). The database itself
lives as a public GitHub Release asset (see all_cards_history.py), so
it's downloaded here over plain HTTPS with no authentication needed, and
cached locally for a few hours so every lookup doesn't re-fetch a
multi-MB file.
"""

import logging
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

import requests

logger = logging.getLogger("tcg-price-checker")

# Overridable so a fork/renamed repo doesn't have to edit source to point
# lookups at its own GitHub Release asset.
REPO = os.environ.get("MTG_PRICE_CHECKER_REPO", "jtbk17/mtg-price-checker")
CACHE_DIR = Path(__file__).parent
CACHE_TTL_SECONDS = 4 * 3600
EPOCH = date(2020, 1, 1)


def _cache_path(year):
    return CACHE_DIR / f"all_cards_lookup_cache_{year}.db"


def _download(year, dest):
    url = f"https://github.com/{REPO}/releases/download/data/all_cards_{year}.db"
    logger.info("Downloading all-cards database for %s lookup...", year)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def _ensure_cached(year):
    dest = _cache_path(year)
    if dest.exists() and (time.time() - dest.stat().st_mtime < CACHE_TTL_SECONDS):
        return dest
    try:
        _download(year, dest)
    except requests.RequestException as exc:
        if dest.exists():
            logger.warning("Could not refresh all-cards database (%s), using stale cache", exc)
            return dest
        raise
    return dest


def get_by_uuid(mtgjson_uuid):
    """Return one card's full price history by its exact MTGJSON uuid, or
    None if it isn't in the all-cards database (e.g. Card Kingdom doesn't
    price it, or the nightly snapshot hasn't picked it up yet)."""
    year = date.today().year
    try:
        db_path = _ensure_cached(year)
    except requests.RequestException as exc:
        logger.warning("All-cards database not available yet (%s)", exc)
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        card = conn.execute(
            "SELECT id, mtgjson_uuid, name, set_code FROM cards WHERE mtgjson_uuid = ?",
            (mtgjson_uuid,),
        ).fetchone()
        if not card:
            return None

        history = conn.execute(
            "SELECT day, price_cents FROM price_history WHERE card_id = ? ORDER BY day ASC",
            (card["id"],),
        ).fetchall()
        return {
            "name": card["name"],
            "set": card["set_code"],
            "mtgjsonId": card["mtgjson_uuid"],
            "history": [
                {
                    "date": date.fromordinal(EPOCH.toordinal() + h["day"]).isoformat(),
                    "price": h["price_cents"] / 100,
                }
                for h in history
            ],
        }
    finally:
        conn.close()
