"""Shared Anthropic client setup, used only by news_signals.py to
classify card mentions found in MTG news articles (mtg_news_feed.py) —
narrowly scoped on purpose: a small daily batch of already-filtered
candidates, not a per-search or per-chat dependency. Fails soft if
ANTHROPIC_API_KEY isn't set, matching telegram_notify.py's convention of
not being a hard dependency for the rest of the app to run.
"""

import os

MODEL = "claude-haiku-4-5-20251001"

_client = None


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client
