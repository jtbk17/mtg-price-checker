"""Sends price alerts via Telegram's free Bot API. Configured with the
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables — if either
is unset, sending is silently skipped (e.g. for local runs where you
haven't set these up), so this is always safe to call.
"""

import logging
import os

import requests

logger = logging.getLogger("tcg-price-checker")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.info("Telegram not configured — skipping notification")
        return

    try:
        resp = requests.post(
            API_URL.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if not resp.ok:
            logger.warning("Telegram send failed (%s): %s", resp.status_code, resp.text[:300])
    except requests.RequestException as exc:
        logger.warning("Telegram send error: %s", exc)
