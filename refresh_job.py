"""Nightly price refresh, runnable standalone (e.g. from GitHub Actions)
without needing the Flask app or any background scheduler running.

Also exports a static docs/watchlist.json snapshot (each tracked card plus
its full price history) for the read-only dashboard served by GitHub Pages.
"""

import json
import logging
from pathlib import Path

import cardkingdom
import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tcg-price-checker")

DOCS_DIR = Path(__file__).parent / "docs"
SNAPSHOT_FILE = DOCS_DIR / "watchlist.json"


def _is_foil(finish):
    return finish != "Normal"


def refresh_watchlist_prices():
    items = db.list_watchlist()
    logger.info("Refreshing prices for %d watched card(s)", len(items))
    for item in items:
        if not item.get("mtgjson_id"):
            continue
        ck_prices = cardkingdom.get_prices(item["mtgjson_id"], foil=_is_foil(item.get("printing")))
        if not ck_prices:
            continue
        if ck_prices["market"] is not None:
            db.record_price(item["variant_id"], ck_prices["market"])
        db.update_cardkingdom_price(item["variant_id"], ck_prices["market"], ck_prices["buylist"])


def export_snapshot():
    from datetime import datetime, timezone

    items = db.list_watchlist()
    for item in items:
        item["history"] = db.get_history(item["variant_id"])

    DOCS_DIR.mkdir(exist_ok=True)
    SNAPSHOT_FILE.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "items": items,
            },
            indent=2,
        )
    )
    logger.info("Wrote snapshot for %d card(s) to %s", len(items), SNAPSHOT_FILE)


if __name__ == "__main__":
    db.init_db()
    refresh_watchlist_prices()
    export_snapshot()
