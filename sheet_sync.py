"""Syncs the shared Google Sheet inventory into the watchlist.

The sheet (link-viewable) is the source of truth for the "Shared" owner:
its "In box" column is the quantity still held, so sold/pulled cards
(In box = 0) drop out of the watchlist. Runs as a step of
nightly-refresh.yml, against the already-downloaded local tcg_prices.db,
right before prices are refreshed — so it's not routed through
release_sync (the workflow's own download/upload and concurrency group
already cover that).

Reuses manabox_import.import_rows by reshaping the sheet's rows into
ManaBox's column names. Purchase price is deliberately left blank: the
sheet's "Price (USD)" is the market price when logged, not cost.
"""

import csv
import io
import logging
import os
import sys

import db
import manabox_import
import requests

logger = logging.getLogger("tcg-price-checker")

SHEET_ID = os.environ.get("INVENTORY_SHEET_ID", "1y8QHFkVoz99qECeM7MB9u9Rt9bZfd7VjD1YIX6OapAU")
SHEET_GID = os.environ.get("INVENTORY_SHEET_GID", "0")
OWNER = os.environ.get("INVENTORY_OWNER", "Shared")

REQUIRED_COLUMNS = {"Name", "Set code", "Foil", "Condition", "Scryfall ID", "In box"}


def fetch_rows():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")))
    # A private sheet answers with a login page rather than an error, so
    # check the header before trusting any of it.
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"Sheet is missing expected columns {sorted(missing)} (is it still link-viewable?)")
    return list(reader)


def to_manabox_rows(sheet_rows):
    return [
        {
            "Name": r["Name"],
            "Set code": r["Set code"],
            "Set name": r.get("Set name", ""),
            "Foil": r["Foil"],
            "Condition": r["Condition"],
            "Scryfall ID": r["Scryfall ID"],
            "Quantity": r["In box"],
            "Purchase price": "",
        }
        for r in sheet_rows
        if r.get("Scryfall ID")
    ]


def remove_zero_quantity(sheet_rows):
    """import_rows keeps any card whose Scryfall ID appears anywhere in the
    CSV, so a card now at In box = 0 would linger with its old quantity.
    Drop Shared rows that aren't held in any variant the sheet lists."""
    held = set()
    for r in sheet_rows:
        if manabox_import._parse_quantity(r.get("In box")) <= 0:
            continue
        finish, _ = manabox_import._finish_and_label(r["Foil"])
        condition = manabox_import._normalize_condition(r["Condition"])
        held.add(f"{r['Scryfall ID']}:{finish}:{db.condition_slug(condition)}")

    current = db.list_watchlist(owner=OWNER)
    stale = [item for item in current if item["variant_id"] not in held]
    if current and len(stale) / len(current) > manabox_import.REMOVAL_WARNING_THRESHOLD:
        raise RuntimeError(f"Refusing to remove {len(stale)} of {len(current)} {OWNER} cards — sheet looks wrong")
    for item in stale:
        db.remove_from_watchlist(item["id"])
    return len(stale)


def main():
    db.init_db()
    sheet_rows = fetch_rows()
    if not sheet_rows:
        raise ValueError("Sheet has no rows — refusing to sync an empty inventory")

    result = manabox_import.import_rows(to_manabox_rows(sheet_rows), owner=OWNER)
    if result["needs_confirmation"]:
        raise RuntimeError(f"Sync would remove {result['pending_removal_count']} cards — sheet looks wrong")
    removed = remove_zero_quantity(sheet_rows)
    logger.info(
        "Sheet sync: %d imported, %d skipped, %d removed (+%d zero-quantity)",
        result["imported"], result["skipped"], result["removed"], removed,
    )
    for err in result["errors"]:
        logger.warning("Sheet sync: %s", err)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except Exception:
        logger.exception("Sheet sync failed")
        sys.exit(1)
