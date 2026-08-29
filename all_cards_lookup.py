"""Look up any card's full price history from the all-cards database —
not just cards on your watchlist. The database itself lives as a public
GitHub Release asset (see all_cards_history.py), so it's downloaded here
over plain HTTPS with no authentication needed, and cached locally for a
few hours so every lookup doesn't re-fetch a multi-MB file.
"""

import logging
import sqlite3
import time
from datetime import date
from pathlib import Path

import requests

logger = logging.getLogger("tcg-price-checker")

REPO = "jtbk17/mtg-price-checker"
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


def search(name, limit=15):
    """Return up to `limit` cards whose name matches `name` (case-
    insensitive substring), each with its full price history, most
    recent snapshots first within a card's own history. Returns an empty
    list (rather than raising) if the all-cards database isn't published
    yet — e.g. before the nightly job has ever run."""
    year = date.today().year
    try:
        db_path = _ensure_cached(year)
    except requests.RequestException as exc:
        logger.warning("All-cards database not available yet (%s)", exc)
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cards = conn.execute(
            "SELECT id, mtgjson_uuid, name, set_code FROM cards "
            "WHERE name LIKE ? AND is_token = 0 ORDER BY name LIMIT ?",
            (f"%{name}%", limit),
        ).fetchall()

        results = []
        for card in cards:
            history = conn.execute(
                "SELECT day, price_cents FROM price_history WHERE card_id = ? ORDER BY day ASC",
                (card["id"],),
            ).fetchall()
            results.append(
                {
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
            )
        return results
    finally:
        conn.close()
