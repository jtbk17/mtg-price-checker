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

Everything that can reject the sheet runs before the first write, so a
bad sheet changes nothing. Removed cards keep their price history, so a
card that drops off the sheet by mistake picks its chart back up when
it returns.
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


def non_blank_rows(sheet_rows):
    return [r for r in sheet_rows if (r.get("Name") or "").strip() or (r.get("Scryfall ID") or "").strip()]


def validate_rows(sheet_rows):
    """Returns the non-blank rows, or raises if any can't be trusted. A
    blank "In box" would otherwise import as 1 copy (_parse_quantity's
    default), and a missing Scryfall ID would look like a removed card."""
    rows = non_blank_rows(sheet_rows)
    if not rows:
        raise ValueError("Sheet has no rows — refusing to sync an empty inventory")
    bad = [r for r in rows if not (r.get("In box") or "").strip().isdigit() or not (r.get("Scryfall ID") or "").strip()]
    if bad:
        names = ", ".join((r.get("Name") or "?") for r in bad[:5])
        raise ValueError(f"{len(bad)} sheet row(s) have an invalid In box or Scryfall ID (e.g. {names})")
    return rows


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
    ]


def find_removals(sheet_rows):
    """Shared rows the sheet no longer holds any copies of, or raises if
    that's too many to be believable. Checks finish and condition too,
    unlike import_rows' own pass, which keeps any card whose Scryfall ID
    appears anywhere in the CSV (so In box = 0 would linger)."""
    held = set()
    for r in sheet_rows:
        if int(r["In box"]) <= 0:
            continue
        finish, _ = manabox_import._finish_and_label(r["Foil"])
        condition = manabox_import._normalize_condition(r["Condition"])
        held.add(f"{r['Scryfall ID']}:{finish}:{db.condition_slug(condition)}")

    current = db.list_watchlist(owner=OWNER)
    stale = [item for item in current if item["variant_id"] not in held]
    if current and len(stale) / len(current) > manabox_import.REMOVAL_WARNING_THRESHOLD:
        raise RuntimeError(f"Refusing to remove {len(stale)} of {len(current)} {OWNER} cards — sheet looks wrong")
    return stale


def main():
    db.init_db()
    sheet_rows = fetch_rows()
    # Empty is only believable for an owner starting from nothing; an empty
    # sheet with cards already tracked falls through to validate_rows.
    if not non_blank_rows(sheet_rows) and not db.list_watchlist(owner=OWNER):
        logger.info("Sheet sync: %s's sheet has no cards yet — nothing to do", OWNER)
        return
    sheet_rows = validate_rows(sheet_rows)
    stale = find_removals(sheet_rows)

    result = manabox_import.import_rows(to_manabox_rows(sheet_rows), owner=OWNER, remove_absent=False)
    if not result["imported"]:
        raise RuntimeError(f"Sheet sync imported nothing ({result['skipped']} skipped) — is Scryfall reachable?")
    for item in stale:
        db.remove_from_watchlist(item["id"], keep_history=True)
    logger.info("Sheet sync: %d imported, %d skipped, %d removed", result["imported"], result["skipped"], len(stale))
    for err in result["errors"]:
        logger.warning("Sheet sync: %s", err)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except Exception:
        logger.exception("Sheet sync failed")
        sys.exit(1)
