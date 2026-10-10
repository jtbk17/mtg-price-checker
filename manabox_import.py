"""Bulk-import a ManaBox collection export (CSV) into the watchlist.

ManaBox's CSV includes (among others) these columns: Name, Set code,
Set name, Collector number, Foil, Rarity, Quantity, ManaBox ID,
Scryfall ID, Purchase price, Condition, Language. The Foil column is the
string "normal" for non-foil cards, or "foil"/"etched" otherwise.

Scryfall ID is used as the join key here rather than trusting the CSV's
own Name/Set columns, since it's an unambiguous reference to the exact
printing — the CSV's text fields are only used as a fallback if a card
can't be found on Scryfall for some reason.

Each import fully syncs the given owner's watchlist to this CSV: cards
present get added/updated, and cards previously tracked for that owner
but genuinely absent from this CSV (e.g. sold and no longer in the real
collection) get automatically untracked — see the `removed` handling
near the end of import_rows() for exactly what counts as "absent".
"""

import csv
import io
import logging

import cardkingdom
import db
import mtgjson_crosswalk
import scryfall
import tcgmarketplace

logger = logging.getLogger("tcg-price-checker")

# A partial/stale CSV imported under the same owner as a much bigger
# collection silently deletes everything not in it (confirmed live: a
# ~100-card CSV wiped 9,378 of 9,478 tracked cards with no warning,
# since nothing here previously capped how much of an owner's
# collection a single import could remove). Above this fraction,
# import_rows() stops short of deleting and asks for confirmation
# instead — see confirm_removals.
REMOVAL_WARNING_THRESHOLD = 0.2


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


def _fetch_scryfall_cards(scryfall_ids, on_progress=None):
    return scryfall.get_cards_by_ids(scryfall_ids, on_progress=on_progress)


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


def import_rows(rows, owner=None, on_progress=None, confirm_removals=False):
    """on_progress(phase, done, total), if given, is called periodically
    across the import's three phases (Scryfall lookup, Card Kingdom price
    lookup, saving to the watchlist) — purely for UI feedback, safe to
    omit. Each phase has its own done/total (they aren't the same count),
    so a caller rendering a single progress bar should reset it on every
    phase change rather than treating this as one continuous 0-100%.

    confirm_removals=False (the default) means a removal that would wipe
    more than REMOVAL_WARNING_THRESHOLD of the owner's current collection
    is held back — nothing gets deleted, and the result instead reports
    needs_confirmation=True with how many cards and what fraction would
    go. The caller should show the user that number and re-call with
    confirm_removals=True only if they explicitly confirm. Smaller
    removals (e.g. a few genuinely sold cards) still happen automatically
    either way, same as before — this only gates the unusual, high-blast-
    radius case."""
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

    # Same two-pass concurrency reasoning as Card Kingdom just above:
    # TheTCGMarketplace has no bulk file to cache from, so a diverse
    # import needs this to avoid one blocking call per card.
    def _name_and_set(g):
        card, row = g["card"], g["row"]
        return card.get("name") or row.get("Name"), card.get("set_name") or row.get("Set name")

    def _tcg_lookup(g):
        name, set_name = _name_and_set(g)
        return tcgmarketplace.lookup_args(
            g["card"], g["finish"], name=name, set_name=set_name, condition=g["condition"]
        )

    tcgmarketplace.prefetch_ids((_tcg_lookup(g) for g in grouped.values()), on_progress=on_progress)
    tcgmarketplace.prefetch_prices((_tcg_lookup(g) for g in grouped.values()), on_progress=on_progress)

    imported = 0
    total_variants = len(grouped)
    for i, (variant_id, g) in enumerate(grouped.items(), start=1):
        card, row = g["card"], g["row"]
        set_code = card.get("set") or row.get("Set code")
        uuid = mtgjson_crosswalk.get_uuid(g["scryfall_id"], set_code)
        ck_prices = cardkingdom.get_prices(uuid, foil=(g["finish"] != "nonfoil")) if uuid else None
        avg_purchase_price = g["_cost_total"] / g["_cost_qty"] if g["_cost_qty"] else None
        name, set_name = _name_and_set(g)
        tcg_market_price = tcgmarketplace.get_price_for_card(*_tcg_lookup(g))

        watchlist_card = {
            "variant_id": variant_id,
            "card_id": g["scryfall_id"],
            "game": "Magic: The Gathering",
            "name": name,
            "set_name": set_name,
            "condition": g["condition"],
            "printing": g["printing"],
            "tcgplayer_id": card.get("tcgplayer_id"),
            "image_url": _image_url(card),
            "price": ck_prices["market"] if ck_prices else None,
            "mtgjson_id": uuid,
            "cardkingdom_price": ck_prices["market"] if ck_prices else None,
            "cardkingdom_buylist_price": ck_prices["buylist"] if ck_prices else None,
            "tcgmarketplace_price": tcg_market_price,
            "owner": owner,
            "quantity": g["quantity"],
            "purchase_price": avg_purchase_price,
        }
        db.add_to_watchlist(watchlist_card)
        imported += 1
        if on_progress:
            on_progress("Saving to your watchlist", i, total_variants)

    removed = 0
    needs_confirmation = False
    pending_removal_count = 0
    if owner:
        # Treat this CSV as the source of truth for the owner's collection:
        # anything previously tracked for them that's genuinely absent here
        # (e.g. a sold card) gets untracked automatically. Scoped by owner
        # so this never touches anyone else's cards, and — since this is a
        # destructive action — deliberately conservative about what counts
        # as "absent": a card whose Scryfall ID appears *anywhere* in this
        # CSV (even under a finish/condition combo that didn't end up in
        # `grouped`, e.g. a transient per-row lookup failure) is left alone
        # rather than risk deleting a real card over an ambiguous partial
        # match.
        csv_scryfall_ids = {row.get("Scryfall ID") for row in rows if row.get("Scryfall ID")}
        present_variant_ids = set(grouped.keys())
        current_items = db.list_watchlist(owner=owner)
        to_remove = [
            item
            for item in current_items
            if item["variant_id"] not in present_variant_ids and item["card_id"] not in csv_scryfall_ids
        ]

        if to_remove and current_items and len(to_remove) / len(current_items) > REMOVAL_WARNING_THRESHOLD and not confirm_removals:
            # Big, unusual removal — hold off and let the caller confirm
            # with the user first, instead of silently deleting most of
            # their collection over one wrong/stale/partial CSV.
            needs_confirmation = True
            pending_removal_count = len(to_remove)
        else:
            for item in to_remove:
                db.remove_from_watchlist(item["id"])
                removed += 1

    return {
        "imported": imported,
        "skipped": skipped,
        "removed": removed,
        "errors": errors[:20],
        "needs_confirmation": needs_confirmation,
        "pending_removal_count": pending_removal_count,
    }
