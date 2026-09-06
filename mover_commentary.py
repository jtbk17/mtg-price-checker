"""Short AI commentary on nightly market movers, appended to each
Telegram alert in market_alerts.py. Uses Claude's web search tool so the
commentary can point to an actual likely cause (a banned/restricted
announcement, a new-set spoiler, a tournament result, a reprint) instead
of just guessing from the price delta alone. Fails soft (returns None)
so a Claude outage or a slow/failed search never blocks an alert from
going out.
"""

import logging

import claude_client

logger = logging.getLogger("tcg-price-checker")

SYSTEM_PROMPT = """You are a terse Magic: The Gathering market analyst. Given one card's price move, write ONE short sentence (under 25 words) speculating on the likely cause. Use web search to check for a specific real-world trigger — a banned/restricted announcement, a new-set spoiler or reprint, a tournament result, a supply shock. If nothing specific turns up, say the move looks speculative/unclear rather than inventing a reason. Do not restate the price numbers — the caller already shows those. Output ONLY the sentence: no preamble, no citations, no markdown."""


def comment(mover, label):
    """Return a short commentary sentence about a market mover dict (as
    produced by all_cards_history.compute_movers), or None if Claude
    isn't configured or the call fails."""
    if not claude_client.configured():
        return None

    prompt = (
        f"Card: {mover['name']} ({mover.get('set_full_name', mover['set'])})\n"
        f"Move: ${mover['price_before']:.2f} -> ${mover['price_now']:.2f} "
        f"({mover['pct_change']:+.1f}%) over {label}."
    )
    try:
        client = claude_client.get_client()
        response = client.messages.create(
            model=claude_client.MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        return text or None
    except Exception as exc:
        logger.warning("Mover commentary failed for %s: %s", mover.get("name"), exc)
        return None
