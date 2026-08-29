"""Nightly price refresh, runnable standalone (e.g. from GitHub Actions)
without needing the Flask app or any background scheduler running.

Also exports static docs/watchlist.json and docs/alerts.json snapshots for
the read-only dashboard served by GitHub Pages, and sends a Telegram
notification for any triggered alerts (see telegram_notify.py — a no-op
if Telegram isn't configured).
"""

import json
import logging
from pathlib import Path

import cardkingdom
import db
import telegram_notify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tcg-price-checker")

DOCS_DIR = Path(__file__).parent / "docs"
SNAPSHOT_FILE = DOCS_DIR / "watchlist.json"
ALERTS_FILE = DOCS_DIR / "alerts.json"

ALERT_PCT_THRESHOLD = 10.0
ALERT_ABS_THRESHOLD = 5.0


def _is_foil(finish):
    return finish != "Normal"


def refresh_watchlist_prices():
    """Refresh every tracked card's Card Kingdom price, and return a list
    of cards whose market price rose by at least $5 or 10% since the last
    recorded price (increases only)."""
    items = db.list_watchlist()
    logger.info("Refreshing prices for %d watched card(s)", len(items))
    alerts = []
    for item in items:
        if not item.get("mtgjson_id"):
            continue
        ck_prices = cardkingdom.get_prices(item["mtgjson_id"], foil=_is_foil(item.get("printing")))
        if not ck_prices:
            continue

        new_price = ck_prices["market"]
        previous_price = item.get("latest_price")
        if new_price is not None:
            if previous_price:
                diff = new_price - previous_price
                pct = diff / previous_price * 100
                if diff >= ALERT_ABS_THRESHOLD or pct >= ALERT_PCT_THRESHOLD:
                    alerts.append(
                        {
                            "name": item["name"],
                            "set_name": item["set_name"],
                            "printing": item["printing"],
                            "owner": item.get("owner"),
                            "price_before": previous_price,
                            "price_now": new_price,
                            "pct_change": round(pct, 1),
                        }
                    )
            db.record_price(item["variant_id"], new_price)
        db.update_cardkingdom_price(item["variant_id"], new_price, ck_prices["buylist"])

    return alerts


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


def export_alerts(alerts):
    from datetime import datetime, timezone

    DOCS_DIR.mkdir(exist_ok=True)
    ALERTS_FILE.write_text(
        json.dumps(
            {"generated_at": datetime.now(timezone.utc).isoformat(), "alerts": alerts},
            indent=2,
        )
    )
    logger.info("Wrote %d alert(s) to %s", len(alerts), ALERTS_FILE)


def notify_alerts(alerts):
    if not alerts:
        return
    lines = ["<b>MTG price alerts</b>"]
    for a in alerts:
        owner_tag = f" ({a['owner']})" if a.get("owner") else ""
        lines.append(
            f"{a['name']} [{a['set_name']}, {a['printing']}]{owner_tag}: "
            f"${a['price_before']:.2f} → ${a['price_now']:.2f} (+{a['pct_change']}%)"
        )
    telegram_notify.send_message("\n".join(lines))


if __name__ == "__main__":
    db.init_db()
    alerts = refresh_watchlist_prices()
    export_snapshot()
    export_alerts(alerts)
    # Guarded here (the CI entrypoint) rather than inside notify_alerts()
    # itself, so a CI retry-replay after a git conflict can't double-send —
    # but app.py's manual "Refresh prices" button still notifies every time,
    # since a user-triggered refresh finding a real threshold crossing
    # should always alert regardless of how many times they've clicked it.
    if not db.already_ran_today("watchlist_alerts_sent"):
        notify_alerts(alerts)
        db.mark_ran_today("watchlist_alerts_sent")
