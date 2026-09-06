"""Shared Anthropic client setup for this app's three Claude-powered
features: natural language search (nl_search.py), the "ask about your
collection" chat (collection_chat.py), and market mover commentary
(mover_commentary.py). All three fail soft if ANTHROPIC_API_KEY isn't
set, matching telegram_notify.py's convention of not being a hard
dependency for the rest of the app to run.
"""

import os

MODEL = "claude-opus-5"

_client = None


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client
