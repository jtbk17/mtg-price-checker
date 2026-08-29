"""Sends price alerts via Telegram's free Bot API, including alerts with
"Good pick" / "False positive" feedback buttons for the recommender to
learn from. Configured with the TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
environment variables — if either is unset, every function here is a
silent no-op, so this module is always safe to call.
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


def get_updates(offset=None):
    if not _token():
        return []
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(API_URL.format(token=_token(), method="getUpdates"), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except requests.RequestException as exc:
        logger.warning("Telegram getUpdates error: %s", exc)
        return []


def answer_callback_query(callback_query_id, text=""):
    if not _token():
        return
    try:
        requests.post(
            API_URL.format(token=_token(), method="answerCallbackQuery"),
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning("Telegram answerCallbackQuery error: %s", exc)


def edit_message_text(chat_id, message_id, text):
    """Replaces a message's text and clears its buttons (so a tap can't
    be registered twice)."""
    if not _token():
        return
    try:
        requests.post(
            API_URL.format(token=_token(), method="editMessageText"),
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": []},
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.warning("Telegram editMessageText error: %s", exc)
