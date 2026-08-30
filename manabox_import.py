"""Bulk-import a ManaBox collection export (CSV) into the watchlist.

ManaBox's CSV includes (among others) these columns: Name, Set code,
Set name, Collector number, Foil, Rarity, Quantity, ManaBox ID,
Scryfall ID, Purchase price, Condition, Language. The Foil column is the
string "normal" for non-foil cards, or "foil"/"etched" otherwise.

Scryfall ID is used as the join key here rather than trusting the CSV's
own Name/Set columns, since it's an unambiguous reference to the exact
printing — the CSV's text fields are only used as a fallback if a card
can't be found on Scryfall for some reason.
"""

import csv
import io
import logging
import time

import requests

import cardkingdom
import db
import mtgjson_crosswalk

logger = logging.getLogger("tcg-price-checker")

COLLECTION_URL = "https://api.scryfall.com/cards/collection"
HEADERS = {"User-Agent": "tcg-price-checker/1.0 (local personal project)", "Accept": "application/json"}
CHUNK_SIZE = 75


def _finish_and_label(foil_value):
    value = (foil_value or "").strip().lower()
    if value == "normal":
        return "nonfoil", "Normal"
    if value == "etched":
        return "etched", "Etched Foil"
    return "foil", "Foil"


# ManaBox tracks a finer 7-tier condition scale than the app's 5-tier one
# (confirmed against ManaBox's own CSV format: mint, near_mint, excellent,
# good, light_played, played, poor — all lowercase with underscores).
# Mapped by name/meaning rather than even ordinal spread — "light_played"
# means the same thing as our "Lightly Played" tier, so it (and the two
# tiers just above it) collapses there instead of landing on "Moderately
# Played" just because it happens to be the 5th-best of 7.
_CONDITION_MAP = {
    "mint": "Near Mint",
    "near_mint": "Near Mint",
    "excellent": "Lightly Played",
    "good": "Lightly Played",
    "light_played": "Lightly Played",
    "played": "Moderately Played",
    "poor": "Damaged",
}


def _normalize_condition(raw_value):
    key = (raw_value or "").strip().lower().replace(" ", "_")
    if key in _CONDITION_MAP:
        return _CONDITION_MAP[key]
    # Unrecognized value (a ManaBox format change, or a hand-edited CSV) —
    # title-case whatever's there rather than silently mislabeling it as a
    # fixed default.
    return key.replace("_", " ").title() if key else "Near Mint"


def _image_url(card):
    image_uris = card.get("image_uris")
    if not image_uris and card.get("card_faces"):
        image_uris = card["card_faces"][0].get("image_uris")
    return (image_uris or {}).get("normal")


REQUEST_PACING_SECONDS = 0.1  # Scryfall asks for ~50-100ms between requests


def _fetch_scryfall_cards(scryfall_ids, on_progress=None):
    """Return {scryfall_id: card_object} via Scryfall's bulk collection
    endpoint (up to 75 ids per request). on_progress(phase, done, total),
    if given, is called after each chunk for progress reporting."""
    result = {}
    ids = list(dict.fromkeys(i for i in scryfall_ids if i))
    total = len(ids)
    for start in range(0, len(ids), CHUNK_SIZE):
        if start > 0:
            time.sleep(REQUEST_PACING_SECONDS)
        chunk = ids[start : start + CHUNK_SIZE]
        resp = requests.post(
            COLLECTION_URL,
            headers=HEADERS,
            json={"identifiers": [{"id": i} for i in chunk]},
            timeout=30,
        )
        resp.raise_for_status()
        for card in resp.json().get("data", []):
            result[card["id"]] = card
        if on_progress:
            on_progress("Fetching card data from Scryfall", min(start + CHUNK_SIZE, total), total)
    return result


def parse_csv(file_bytes):
    text = file_bytes.decode("utf-8-sig")  # ManaBox exports include a BOM
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Scryfall ID" not in reader.fieldnames:
        raise ValueError("This doesn't look like a ManaBox export (no 'Scryfall ID' column found).")
    return list(reader)


def _parse_quantity(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _parse_price(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def import_rows(rows, owner=None, on_progress=None):
    """on_progress(phase, done, total), if given, is called periodically
    across the import's three phases (Scryfall lookup, Card Kingdom price
    lookup, saving to the watchlist) — purely for UI feedback, safe to
    omit. Each phase has its own done/total (they aren't the same count),
    so a caller rendering a single progress bar should reset it on every
    phase change rather than treating this as one continuous 0-100%."""
    scryfall_cards = _fetch_scryfall_cards((row.get("Scryfall ID") for row in rows), on_progress=on_progress)

    # ManaBox splits the same card+finish across multiple rows when you own
    # copies in different conditions (e.g. 2 Near Mint + 1 Lightly Played)
    # — condition is part of variant_id (see db.condition_slug), so those
    # stay as separate grouped entries; only rows that are fully identical
    # (same printing *and* condition) get their quantities summed here,
    # rather than letting the last one silently overwrite the others.
    grouped = {}
    skipped = 0
    errors = []
    for row in rows:
        scryfall_id = row.get("Scryfall ID")
        card = scryfall_cards.get(scryfall_id)
        if not card:
            skipped += 1
            errors.append(f"{row.get('Name', '?')}: not found on Scryfall")
            continue

        quantity = _parse_quantity(row.get("Quantity"))
        if quantity <= 0:
            continue  # e.g. a sold/removed card ManaBox kept a zero-row for

        finish, printing = _finish_and_label(row.get("Foil"))
        condition = _normalize_condition(row.get("Condition"))
        variant_id = f"{scryfall_id}:{finish}:{db.condition_slug(condition)}"
        purchase_price = _parse_price(row.get("Purchase price"))

        if variant_id in grouped:
            g = grouped[variant_id]
            g["quantity"] += quantity
            # Weighted average unit cost across merged rows (e.g. 2 copies
            # bought at $5 + 1 at $8 should read as ~$6, not just the last
            # row seen). Rows missing a price are excluded from the
            # average rather than treated as $0, which would understate it.
            if purchase_price is not None:
                g["_cost_total"] += purchase_price * quantity
                g["_cost_qty"] += quantity
        else:
            grouped[variant_id] = {
                "scryfall_id": scryfall_id,
                "card": card,
                "row": row,
                "finish": finish,
                "printing": printing,
                "condition": condition,
                "quantity": quantity,
                "_cost_total": purchase_price * quantity if purchase_price is not None else 0,
                "_cost_qty": quantity if purchase_price is not None else 0,
            }

    # A diverse collection can span dozens of sets never seen before —
    # warm them all concurrently first rather than one blocking request
    # per grouped variant (measured at 84s for a single 69-set search
    # before this fix; a large collection could be far worse).
    mtgjson_crosswalk.prefetch_sets(
        (g["card"].get("set") or g["row"].get("Set code") for g in grouped.values()),
        on_progress=on_progress,
    )

    imported = 0
    total_variants = len(grouped)
    for i, (variant_id, g) in enumerate(grouped.items(), start=1):
        card, row = g["card"], g["row"]
        set_code = card.get("set") or row.get("Set code")
        uuid = mtgjson_crosswalk.get_uuid(g["scryfall_id"], set_code)
        ck_prices = cardkingdom.get_prices(uuid, foil=(g["finish"] != "nonfoil")) if uuid else None
        avg_purchase_price = g["_cost_total"] / g["_cost_qty"] if g["_cost_qty"] else None

        watchlist_card = {
            "variant_id": variant_id,
            "card_id": g["scryfall_id"],
            "game": "Magic: The Gathering",
            "name": card.get("name") or row.get("Name"),
            "set_name": card.get("set_name") or row.get("Set name"),
            "condition": g["condition"],
            "printing": g["printing"],
            "tcgplayer_id": card.get("tcgplayer_id"),
            "image_url": _image_url(card),
            "price": ck_prices["market"] if ck_prices else None,
            "mtgjson_id": uuid,
            "cardkingdom_price": ck_prices["market"] if ck_prices else None,
            "cardkingdom_buylist_price": ck_prices["buylist"] if ck_prices else None,
            "owner": owner,
            "quantity": g["quantity"],
            "purchase_price": avg_purchase_price,
        }
        db.add_to_watchlist(watchlist_card)
        imported += 1
        if on_progress:
            on_progress("Saving to your watchlist", i, total_variants)

    return {"imported": imported, "skipped": skipped, "errors": errors[:20]}
