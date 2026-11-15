"""Spots likely sales from our TheTCGMarketplace shop's listings.

auction_deals.py re-reads the shop every run (docs/my_listings.json).
Whenever a listing's quantity drops, or the listing disappears, between
two reads, that's recorded in docs/listing_sales.json for the
dashboard's Orders section to ask about ("Looks sold?"). Public listing
data can't tell a sale from a delisting or an edit, so nothing here
records an order: the dashboard does that only once you confirm.

Each drop carries the matching dashboard card(s) as they were when the
drop was seen, since a sold card can leave the watchlist (and with it
the only listing-key -> card link) once it's pulled.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("tcg-price-checker")

DOCS_DIR = Path(__file__).parent / "docs"
SALES_FILE = DOCS_DIR / "listing_sales.json"
WATCHLIST_FILE = DOCS_DIR / "watchlist.json"
# Same window as the dashboard's recently closed orders (and the Apps
# Script's), so a drop is never shown after its order has aged out.
RECENT_DAYS = 14

CARD_FIELDS = ("owner", "card_id", "name", "set_name", "printing", "condition", "image_url")


def find_drops(previous, current):
    """Listings whose quantity went down from `previous` to `current`
    (both {listing_key: {"quantity", "price"}}), with the price they were
    listed at."""
    drops = []
    for key, before in previous.items():
        after = current.get(key, {}).get("quantity", 0)
        if after < before["quantity"]:
            drops.append({"key": key, "qty_before": before["quantity"], "qty_after": after, "price": before["price"]})
    return drops


def _cards_by_key(watchlist_items):
    cards = {}
    for item in watchlist_items:
        key = item.get("tcgmarketplace_listing_key")
        if key:
            cards.setdefault(key, []).append({field: item.get(field) for field in CARD_FIELDS})
    return cards


def record_drops(previous, current, now=None, sales_file=SALES_FILE, watchlist_file=WATCHLIST_FILE):
    """Adds any drops from `previous` to `current` to `sales_file`, and
    prunes entries older than RECENT_DAYS. Returns the new drops.

    A read that finds the whole shop empty is held for one more read
    before anything is flagged: it's as likely an outage answering with an
    empty list as a real sell-out. If the next read has listings again,
    they're compared against the listings from before the empty one (so a
    sale in between still shows); if it's empty again, the sell-out is
    believed."""
    now = now or datetime.now(timezone.utc)
    try:
        saved = json.loads(Path(sales_file).read_text())
    except (OSError, ValueError):
        saved = {}
    existing = saved.get("drops", [])
    held = saved.get("held_before_empty")

    if held is not None:
        previous, held = held, None
    elif previous and not current:
        logger.warning("Shop read came back empty (had %d listings) — waiting for the next read", len(previous))
        held, previous = previous, current
    drops = find_drops(previous, current)

    cutoff = now - timedelta(days=RECENT_DAYS)
    kept = [d for d in existing if datetime.fromisoformat(d["detected_at"]) >= cutoff]

    if drops:
        try:
            cards = _cards_by_key(json.loads(Path(watchlist_file).read_text()).get("items", []))
        except (OSError, ValueError):
            cards = {}
        stamp = now.isoformat()
        for drop in drops:
            drop.update(id=f"{drop['key']}@{stamp}", detected_at=stamp, cards=cards.get(drop["key"], []))
            logger.info("Listing %s dropped %d -> %d", drop["key"], drop["qty_before"], drop["qty_after"])

    changed = drops or len(kept) != len(existing) or held != saved.get("held_before_empty")
    if changed or not Path(sales_file).exists():
        out = {"drops": kept + drops}
        if held is not None:
            out["held_before_empty"] = held
        Path(sales_file).write_text(json.dumps(out, separators=(",", ":")))
    return drops
