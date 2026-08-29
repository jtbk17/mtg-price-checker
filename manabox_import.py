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


def _image_url(card):
    image_uris = card.get("image_uris")
    if not image_uris and card.get("card_faces"):
        image_uris = card["card_faces"][0].get("image_uris")
    return (image_uris or {}).get("normal")


def _fetch_scryfall_cards(scryfall_ids):
    """Return {scryfall_id: card_object} via Scryfall's bulk collection
    endpoint (up to 75 ids per request)."""
    result = {}
    ids = list(dict.fromkeys(i for i in scryfall_ids if i))
    for start in range(0, len(ids), CHUNK_SIZE):
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
    return result


def parse_csv(file_bytes):
    text = file_bytes.decode("utf-8-sig")  # ManaBox exports include a BOM
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Scryfall ID" not in reader.fieldnames:
        raise ValueError("This doesn't look like a ManaBox export (no 'Scryfall ID' column found).")
    return list(reader)


def import_rows(rows, owner=None):
    scryfall_cards = _fetch_scryfall_cards(row.get("Scryfall ID") for row in rows)

    imported = 0
    skipped = 0
    errors = []

    for row in rows:
        scryfall_id = row.get("Scryfall ID")
        card = scryfall_cards.get(scryfall_id)
        if not card:
            skipped += 1
            errors.append(f"{row.get('Name', '?')}: not found on Scryfall")
            continue

        set_code = card.get("set") or row.get("Set code")
        finish, printing = _finish_and_label(row.get("Foil"))
        uuid = mtgjson_crosswalk.get_uuid(scryfall_id, set_code)
        ck_prices = cardkingdom.get_prices(uuid, foil=(finish != "nonfoil")) if uuid else None

        watchlist_card = {
            "variant_id": f"{scryfall_id}:{finish}",
            "card_id": scryfall_id,
            "game": "Magic: The Gathering",
            "name": card.get("name") or row.get("Name"),
            "set_name": card.get("set_name") or row.get("Set name"),
            "condition": "Near Mint",
            "printing": printing,
            "tcgplayer_id": card.get("tcgplayer_id"),
            "image_url": _image_url(card),
            "price": ck_prices["market"] if ck_prices else None,
            "mtgjson_id": uuid,
            "cardkingdom_price": ck_prices["market"] if ck_prices else None,
            "cardkingdom_buylist_price": ck_prices["buylist"] if ck_prices else None,
            "owner": owner,
        }
        db.add_to_watchlist(watchlist_card)
        imported += 1

    return {"imported": imported, "skipped": skipped, "errors": errors[:20]}
