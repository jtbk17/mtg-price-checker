"""Daily Telegram nudge about orders that have stalled: a buyer who hasn't
paid after AWAITING_DAYS, or paid copies not dropped off after PAID_DAYS.
Run once a night by nightly-refresh.yml, never retried (a Telegram send
can't be taken back), and at most once a day.

Orders live in the inventory sheets' Orders tabs, read through the same
Apps Script web app the dashboard uses (google_apps_script.js), with the
passcode from the SHEETS_PASSCODE secret.
"""

import json
import logging
import os
from datetime import datetime, timezone

import requests

import db
import listing_sales
import telegram_notify

logger = logging.getLogger("tcg-price-checker")

# Same as SHEETS_API_URL in docs/index.html.
SHEETS_API_URL = "https://script.google.com/macros/s/AKfycbyHzEPynfBNbjd6_ZawrW47MmlicWc0BZuznQYO-wJmZ5l49v0enPtAK6tbz0EUzXyT/exec"
AWAITING_DAYS = 2
PAID_DAYS = 3


def fetch_orders(passcode):
    resp = requests.post(SHEETS_API_URL, data=json.dumps({"action": "list", "passcode": passcode}), timeout=90)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Apps Script said: {data.get('error')}")
    return data["orders"]


def _days_since(iso, now):
    return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 86400 if iso else 0


def stalled(orders, now=None):
    """(orders awaiting payment too long, paid orders not dropped off)."""
    now = now or datetime.now(timezone.utc)
    unpaid = [o for o in orders if o["status"] == "Awaiting payment" and _days_since(o["offered"], now) >= AWAITING_DAYS]
    undropped = [o for o in orders if o["status"] == "Paid" and _days_since(o["paid"], now) >= PAID_DAYS]
    return unpaid, undropped


def message(unpaid, undropped, now=None):
    import html

    now = now or datetime.now(timezone.utc)

    def line(o, since):
        copies = f" ×{o['qty']}" if o["qty"] > 1 else ""
        to = " · to GOG" if o.get("type") == "GOG" else ""
        return f"• {html.escape(o['name'])}{copies} ({html.escape(o['owner'])}{to}) — {int(_days_since(o[since], now))} days"

    # A GOG sale waits on you pulling it, not on a buyer paying.
    unpulled = [o for o in unpaid if o.get("type") == "GOG"]
    unpaid = [o for o in unpaid if o.get("type") != "GOG"]
    parts = ["<b>⏰ Orders waiting on you</b>"]
    if unpaid:
        parts.append(f"\n<b>Buyer hasn't paid</b> ({AWAITING_DAYS}+ days):\n" + "\n".join(line(o, "offered") for o in unpaid))
    if unpulled:
        parts.append(f"\n<b>Not pulled for GOG yet</b> ({AWAITING_DAYS}+ days):\n" + "\n".join(line(o, "offered") for o in unpulled))
    if undropped:
        parts.append(f"\n<b>Paid, not dropped off</b> ({PAID_DAYS}+ days):\n" + "\n".join(line(o, "paid") for o in undropped))
    parts.append(f'\n<a href="{listing_sales.DASHBOARD_ORDERS_URL}">Open Orders on the dashboard</a>')
    return "\n".join(parts)


def send_reminders():
    passcode = os.environ.get("SHEETS_PASSCODE")
    if not passcode:
        logger.info("No SHEETS_PASSCODE — skipping order reminders")
        return
    if db.already_ran_today("order_reminders_sent"):
        logger.info("Order reminders already sent today")
        return
    unpaid, undropped = stalled(fetch_orders(passcode))
    if unpaid or undropped:
        telegram_notify.send_message(message(unpaid, undropped))
        logger.info("Reminded about %d unpaid and %d undropped order(s)", len(unpaid), len(undropped))
    db.mark_ran_today("order_reminders_sent")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    send_reminders()
