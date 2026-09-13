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
import mtgjson_crosswalk
import tcgmarketplace
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

    # TheTCGMarketplace has no bulk price file to cache from like MTGJSON —
    # every card needs a live call. Resolving ids then fetching prices
    # concurrently (same two-pass pattern as /api/search) turns what would
    # be one blocking round trip per card, sequentially, into however long
    # the slowest one takes.
    tcgmarketplace.prefetch_ids((item["name"], item["set_name"]) for item in items)
    tcgmarketplace.prefetch_prices(
        tcgmarketplace.find_id(item["name"], item["set_name"]) for item in items
    )

    alerts = []
    for item in items:
        # Independent of the Card Kingdom gate just below: TheTCGMarketplace
        # is matched by name+set, not mtgjson_id, so it applies even to
        # cards Card Kingdom doesn't carry.
        tcg_price = tcgmarketplace.get_price_for_card(item["name"], item["set_name"])
        if tcg_price is not None:
            db.update_tcgmarketplace_price(item["variant_id"], tcg_price)

        if not item.get("mtgjson_id"):
            continue

        mtgjson_id = item["mtgjson_id"]
        # Some split/adventure/double-faced cards have Card Kingdom's price
        # attached to a different MTGJSON uuid than the one this card was
        # originally crosswalked to (see mtgjson_crosswalk.py's module
        # docstring) — and which uuid that is can drift from one day's
        # price feed to the next. Re-check (cache-only: no network call
        # for a card already crosswalked, since set_code is omitted) and
        # self-heal the stored id if it's drifted.
        if item.get("card_id"):
            candidates = mtgjson_crosswalk.get_uuid_candidates(item["card_id"], set_code=None)
            if len(candidates) > 1:
                preferred = mtgjson_crosswalk.get_uuid(item["card_id"], set_code=None)
                if preferred and preferred != mtgjson_id:
                    db.update_mtgjson_id(item["variant_id"], preferred)
                    mtgjson_id = preferred

        ck_prices = cardkingdom.get_prices(mtgjson_id, foil=_is_foil(item.get("printing")))
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
            db.record_price(item["variant_id"], new_price, kind="market")
        if ck_prices["buylist"] is not None:
            db.record_price(item["variant_id"], ck_prices["buylist"], kind="buylist")
        db.update_cardkingdom_price(item["variant_id"], new_price, ck_prices["buylist"])

    return alerts


def export_snapshot():
    from datetime import datetime, timezone

    items = db.list_watchlist()
    for item in items:
        item["history"] = db.get_history(item["variant_id"], kind="market")
        item["buylist_history"] = db.get_history(item["variant_id"], kind="buylist")

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


TELEGRAM_MESSAGE_LIMIT = 4096


def _alert_line(a):
    owner_tag = f" ({a['owner']})" if a.get("owner") else ""
    return (
        f"{a['name']} [{a['set_name']}, {a['printing']}]{owner_tag}: "
        f"${a['price_before']:.2f} → ${a['price_now']:.2f} (+{a['pct_change']}%)"
    )


def _chunk_lines(lines, limit):
    """Group lines into as few messages as possible, each under `limit`
    characters — a single alert line is never anywhere close to Telegram's
    per-message cap on its own, so there's no need to split a line
    itself."""
    chunks = []
    current, current_len = [], 0
    for line in lines:
        extra = len(line) + (1 if current else 0)  # +1 for the joining newline
        if current and current_len + extra > limit:
            chunks.append(current)
            current, current_len, extra = [], 0, len(line)
        current.append(line)
        current_len += extra
    if current:
        chunks.append(current)
    return chunks


def notify_alerts(alerts):
    """Sends one or more Telegram messages listing every triggered
    watchlist alert, splitting across messages when there are enough that
    one would exceed Telegram's 4096-character-per-message limit —
    previously every alert was crammed into a single sendMessage call, so
    a big batch (163 alerts, once observed) failed outright and silently
    dropped every alert in it, not just the ones past the limit."""
    if not alerts:
        return
    lines = [_alert_line(a) for a in alerts]
    header = "<b>MTG price alerts</b>"
    chunks = _chunk_lines(lines, TELEGRAM_MESSAGE_LIMIT - len(header) - 20)  # headroom for the "(n/N)" suffix
    total = len(chunks)
    for i, chunk_lines in enumerate(chunks, start=1):
        title = header if total == 1 else f"{header} ({i}/{total})"
        telegram_notify.send_message(title + "\n" + "\n".join(chunk_lines))


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
