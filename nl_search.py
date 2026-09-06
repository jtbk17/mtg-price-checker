"""Translates a plain-English card search into Scryfall's query syntax
via Claude, so "cheap red removal under $5" works as a search instead
of requiring Scryfall's own operators (c:r, o:destroy, usd<5, ...).
"""

import logging

import claude_client

logger = logging.getLogger("tcg-price-checker")

SYSTEM_PROMPT = """You translate a plain-English Magic: The Gathering card search into a single Scryfall search query string.

Scryfall syntax reference (use only what's needed for the given request):
- c:wubrg / color:red - color(s). c>=wu means at least white and blue. c=r means mono-red.
- t:creature / type:instant - card type/subtype (t:goblin, t:legendary, t:artifact)
- o:"destroy target creature" - oracle text search (substring)
- usd<5 / usd>=10 / usd>1 - price in USD (Scryfall market price)
- cmc<=3 / cmc=2 - mana value
- pow>=4 / tou<=2 - power/toughness
- r:rare / r:mythic / r:common / r:uncommon - rarity
- is:commander / is:foil / is:reprint
- f:standard / f:modern / f:commander - format legality
- year<2010 / year>=2020 - set release year
- Bare words (no operator) search the card name.

Respond with ONLY the Scryfall query string, nothing else - no explanation, no markdown, no quotes around it."""


def translate(natural_language_query):
    """Return a Scryfall query string, or None if Claude isn't
    configured or the call fails (caller should show an error rather
    than silently falling back to a literal/broken search)."""
    if not claude_client.configured():
        return None
    try:
        client = claude_client.get_client()
        response = client.messages.create(
            model=claude_client.MODEL,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": natural_language_query}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        return text or None
    except Exception as exc:
        logger.warning("Claude search translation failed: %s", exc)
        return None
