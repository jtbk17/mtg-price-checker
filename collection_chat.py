"""'Ask about your collection' chat: answers plain-English questions like
"what's my most valuable card" or "how has my Sol Ring done" via a
Claude tool-use loop over db.py.

Tools return compact, aggregated results — never the raw watchlist
table — since a collection can run into the thousands of rows and
dumping all of it into the prompt would blow both the token budget and
the cost of a single question.
"""

import json
import logging

import claude_client
import db

logger = logging.getLogger("tcg-price-checker")

MAX_TOOL_ROUNDS = 5
QUERY_RESULT_LIMIT = 25
HISTORY_POINT_LIMIT = 30

SYSTEM_PROMPT = f"""You answer questions about the user's Magic: The Gathering card collection using the tools provided.

The collection may be shared across multiple owners. If the question says "my cards" or names \
an owner and it's ambiguous which owner is meant, call list_owners and ask for clarification \
rather than guessing.

"Value" of a card means its quantity times its latest known market price (Card Kingdom). \
Gain/loss is only meaningful for cards that have a recorded purchase price — say so rather \
than inventing a cost basis for cards that don't have one.

Be concise and concrete: cite actual card names, set names, and numbers from the tool results \
rather than speaking in generalities. If a tool returns no results, say so plainly rather than \
guessing. Do not call a tool more than necessary to answer the question."""

TOOLS = [
    {
        "name": "list_owners",
        "description": "List the distinct owner names tracked in the collection.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "query_collection",
        "description": (
            f"Search/filter/sort the watchlist. Returns up to {QUERY_RESULT_LIMIT} matching "
            "cards, each with name, set, condition, quantity, owner, market price, buylist "
            "price, purchase price, and gain/loss. Use this for questions like 'what cards "
            "does X own', 'most valuable cards', 'cards worth more than $Y'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Exact owner name to filter to. Omit for all owners."},
                "name_contains": {"type": "string", "description": "Case-insensitive substring match on card name."},
                "set_name_contains": {"type": "string", "description": "Case-insensitive substring match on set name."},
                "sort": {
                    "type": "string",
                    "enum": ["name", "price", "value", "gain"],
                    "description": "name=A-Z, price=highest unit price first, value=highest quantity*price first, gain=best gain/loss first.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max rows to return, default {QUERY_RESULT_LIMIT}, capped at {QUERY_RESULT_LIMIT}.",
                },
            },
        },
    },
    {
        "name": "get_portfolio_summary",
        "description": (
            "Get aggregated totals for the collection: number of copies, distinct cards, "
            "total market value, total cost basis, and gain/loss. Broken down per-owner when "
            "owner is omitted and more than one owner is tracked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Exact owner name to restrict to. Omit for everyone."},
            },
        },
    },
    {
        "name": "get_price_history",
        "description": (
            "Get recent market price history for one specific card already in the collection, "
            "to answer questions like 'how has X done' or 'is X trending up'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Card name (substring match)."},
                "set_name": {"type": "string", "description": "Set name, to disambiguate if needed."},
                "owner": {"type": "string", "description": "Owner, to disambiguate if needed."},
            },
            "required": ["name"],
        },
    },
]


def _row_summary(row):
    latest = row.get("latest_price")
    purchase = row.get("purchase_price")
    qty = row.get("quantity") or 1
    gain = None
    if latest is not None and purchase is not None:
        gain = round((latest - purchase) * qty, 2)
    return {
        "name": row.get("name"),
        "set_name": row.get("set_name"),
        "condition": row.get("condition"),
        "owner": row.get("owner") or None,
        "quantity": qty,
        "cardkingdom_market_price": latest,
        "cardkingdom_buylist_price": row.get("cardkingdom_buylist_price"),
        "tcgmarketplace_price": row.get("tcgmarketplace_price"),
        "purchase_price_per_card": purchase,
        "total_gain_loss": gain,
    }


def _filter_rows(rows, name_contains=None, set_name_contains=None):
    if name_contains:
        needle = name_contains.lower()
        rows = [r for r in rows if needle in (r.get("name") or "").lower()]
    if set_name_contains:
        needle = set_name_contains.lower()
        rows = [r for r in rows if needle in (r.get("set_name") or "").lower()]
    return rows


def _tool_list_owners(_input):
    return {"owners": db.list_owners()}


def _tool_query_collection(input_):
    rows = db.list_watchlist(owner=input_.get("owner") or None, sort=input_.get("sort"))
    rows = _filter_rows(rows, input_.get("name_contains"), input_.get("set_name_contains"))
    limit = min(input_.get("limit") or QUERY_RESULT_LIMIT, QUERY_RESULT_LIMIT)
    total_matches = len(rows)
    return {
        "total_matches": total_matches,
        "returned": min(total_matches, limit),
        "cards": [_row_summary(r) for r in rows[:limit]],
    }


def _summarize_portfolio(rows):
    total_copies = sum(r.get("quantity") or 1 for r in rows)
    distinct_cards = len(rows)
    total_value = sum(
        (r["latest_price"] * (r.get("quantity") or 1)) for r in rows if r.get("latest_price") is not None
    )
    costed = [r for r in rows if r.get("purchase_price") is not None and r.get("latest_price") is not None]
    total_cost_basis = sum(r["purchase_price"] * (r.get("quantity") or 1) for r in costed)
    total_value_of_costed = sum(r["latest_price"] * (r.get("quantity") or 1) for r in costed)
    return {
        "distinct_cards": distinct_cards,
        "total_copies": total_copies,
        "total_market_value": round(total_value, 2),
        "cards_with_known_cost_basis": len(costed),
        "total_cost_basis": round(total_cost_basis, 2) if costed else None,
        "total_gain_loss": round(total_value_of_costed - total_cost_basis, 2) if costed else None,
    }


def _tool_get_portfolio_summary(input_):
    owner = input_.get("owner") or None
    if owner:
        return _summarize_portfolio(db.list_watchlist(owner=owner))

    owners = db.list_owners()
    if len(owners) <= 1:
        return _summarize_portfolio(db.list_watchlist())

    return {
        "overall": _summarize_portfolio(db.list_watchlist()),
        "by_owner": {o: _summarize_portfolio(db.list_watchlist(owner=o)) for o in owners},
    }


def _tool_get_price_history(input_):
    rows = db.list_watchlist(owner=input_.get("owner") or None)
    rows = _filter_rows(rows, input_.get("name"), input_.get("set_name"))
    if not rows:
        return {"error": "No card in the collection matches that name/set/owner."}
    if len(rows) > 1:
        return {
            "error": "Multiple matching cards — narrow it down with set_name and/or owner.",
            "matches": [_row_summary(r) for r in rows[:QUERY_RESULT_LIMIT]],
        }

    row = rows[0]
    history = db.get_history(row["variant_id"], kind="market")[-HISTORY_POINT_LIMIT:]
    return {
        "card": _row_summary(row),
        "history": [{"date": h["recorded_at"], "price": h["price"]} for h in history],
    }


_TOOL_IMPLS = {
    "list_owners": _tool_list_owners,
    "query_collection": _tool_query_collection,
    "get_portfolio_summary": _tool_get_portfolio_summary,
    "get_price_history": _tool_get_price_history,
}


def ask(question, history=None):
    """Answer a question about the collection. `history` is a prior list
    of {role, content} message dicts (as returned by this function) to
    continue a conversation; pass None to start fresh. Returns
    (answer_text, updated_history), or (None, history) if Claude isn't
    configured or the call fails."""
    if not claude_client.configured():
        return None, history

    messages = list(history or [])
    messages.append({"role": "user", "content": question})

    try:
        client = claude_client.get_client()
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.messages.create(
                model=claude_client.MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
            # Converted to plain dicts (rather than appending the SDK's
            # response.content objects as-is) so `messages` stays JSON
            # round-trippable — the caller stores it client-side between
            # turns and hands it straight back on the next request.
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append(
                        {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                    )
            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason != "tool_use":
                text = next((b["text"] for b in assistant_content if b["type"] == "text"), "").strip()
                return text or None, messages

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                impl = _TOOL_IMPLS.get(block.name)
                try:
                    result = impl(block.input) if impl else {"error": f"Unknown tool {block.name}"}
                except Exception as exc:  # noqa: BLE001 - surfaced to Claude, not raised
                    result = {"error": str(exc)}
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return "Sorry, that question needed more digging than I could fit — try breaking it into a simpler question.", messages
    except Exception as exc:
        logger.warning("Collection chat failed: %s", exc)
        return None, history
