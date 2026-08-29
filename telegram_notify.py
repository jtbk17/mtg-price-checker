"""Sends price alerts via Telegram's free Bot API, including alerts with
"Good pick" / "False positive" feedback buttons for the recommender to
learn from. Configured with the TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
environment variables — if either is unset, every function here is a
silent no-op, so this module is always safe to call.

Button taps are handled by a Cloudflare Worker webhook (see
cloudflare_worker.js), not by anything in this Python codebase — Telegram
delivers a tap to at most one place (a webhook, if registered, otherwise
long-polling), so once a webhook is set there's nothing left for this
process to poll.
"""

import logging
import os

import requests

logger = logging.getLogger("tcg-price-checker")

API_URL = "https://api.telegram.org/bot{token}/{method}"


def _token():
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def _configured():
    return bool(_token() and os.environ.get("TELEGRAM_CHAT_ID"))


def send_message(text):
    if not _configured():
        logger.info("Telegram not configured — skipping notification")
        return
    try:
        resp = requests.post(
            API_URL.format(token=_token(), method="sendMessage"),
            json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if not resp.ok:
            logger.warning("Telegram send failed (%s): %s", resp.status_code, resp.text[:300])
    except requests.RequestException as exc:
        logger.warning("Telegram send error: %s", exc)


def send_photo_with_buttons(photo_url, caption, buttons):
    """Like send_message_with_buttons, but sends the card's image with the
    text as a caption underneath — this is the whole point of including a
    photo, so you can visually confirm which printing an alert is about.
    Falls back to a plain text message if the photo send fails (e.g. no
    scryfall_id was available, or Scryfall is briefly unreachable)."""
    if not _configured():
        logger.info("Telegram not configured — skipping notification")
        return None

    reply_markup = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
    if photo_url:
        try:
            resp = requests.post(
                API_URL.format(token=_token(), method="sendPhoto"),
                json={
                    "chat_id": os.environ["TELEGRAM_CHAT_ID"],
                    "photo": photo_url,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup,
                },
                timeout=15,
            )
            if resp.ok:
                result = resp.json()["result"]
                return result["chat"]["id"], result["message_id"]
            logger.warning("Telegram sendPhoto failed (%s): %s — falling back to text", resp.status_code, resp.text[:300])
        except requests.RequestException as exc:
            logger.warning("Telegram sendPhoto error: %s — falling back to text", exc)

    return send_message_with_buttons(caption, buttons)


def send_message_with_buttons(text, buttons):
    """buttons: list of (label, callback_data) tuples, shown as one row of
    inline buttons. Returns (chat_id, message_id) of the sent message, or
    None if Telegram isn't configured or the send failed."""
    if not _configured():
        logger.info("Telegram not configured — skipping notification")
        return None

    reply_markup = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
    try:
        resp = requests.post(
            API_URL.format(token=_token(), method="sendMessage"),
            json={
                "chat_id": os.environ["TELEGRAM_CHAT_ID"],
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
            timeout=15,
        )
        if not resp.ok:
            logger.warning("Telegram send failed (%s): %s", resp.status_code, resp.text[:300])
            return None
        result = resp.json()["result"]
        return result["chat"]["id"], result["message_id"]
    except requests.RequestException as exc:
        logger.warning("Telegram send error: %s", exc)
        return None
