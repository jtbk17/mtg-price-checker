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

Only cards new to the watchlist go through import_rows (the slow part:
Scryfall, Card Kingdom and TCGMP lookups); a changed count on a card
already tracked is just written, which keeps sheet_sync_job.py's
every-30-minutes run cheap.
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


def fetch_csv(sheet_id, gid="0"):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig")


def parse_rows(text):
    reader = csv.DictReader(io.StringIO(text))
    # A private sheet answers with a login page rather than an error, so
    # check the header before trusting any of it.
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"Sheet is missing expected columns {sorted(missing)} (is it still link-viewable?)")
    return list(reader)


def fetch_rows(sheet_id=None, gid=None):
    return parse_rows(fetch_csv(sheet_id or SHEET_ID, gid or SHEET_GID))


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


def variant_of(row):
    """The watchlist variant_id a sheet row maps to (as import_rows builds it)."""
    finish, _ = manabox_import._finish_and_label(row["Foil"])
    condition = manabox_import._normalize_condition(row["Condition"])
    return f"{row['Scryfall ID']}:{finish}:{db.condition_slug(condition)}"


def held_counts(sheet_rows):
    """{variant_id: copies still in the box}, summed over every row of that
    printing (one per batch location)."""
    held = {}
    for r in sheet_rows:
        if int(r["In box"]) > 0:
            held[variant_of(r)] = held.get(variant_of(r), 0) + int(r["In box"])
    return held


def find_removals(held, current, owner):
    """Tracked cards the sheet no longer holds any copies of, or raises if
    that's too many to be believable. Checks finish and condition too,
    unlike import_rows' own pass, which keeps any card whose Scryfall ID
    appears anywhere in the CSV (so In box = 0 would linger)."""
    stale = [item for item in current if item["variant_id"] not in held]
    if current and len(stale) / len(current) > manabox_import.REMOVAL_WARNING_THRESHOLD:
        raise RuntimeError(f"Refusing to remove {len(stale)} of {len(current)} {owner} cards — sheet looks wrong")
    return stale


def sync(owner, sheet_rows):
    """Brings `owner`'s watchlist in line with their sheet's rows. Returns
    whether anything changed."""
    db.init_db()
    current = db.list_watchlist(owner=owner)
    # Empty is only believable for an owner starting from nothing; an empty
    # sheet with cards already tracked falls through to validate_rows.
    if not non_blank_rows(sheet_rows) and not current:
        logger.info("Sheet sync: %s's sheet has no cards yet — nothing to do", owner)
        return False
    sheet_rows = validate_rows(sheet_rows)
    held = held_counts(sheet_rows)
    stale = find_removals(held, current, owner)
    tracked = {item["variant_id"]: item for item in current}

    new_rows = [r for r in sheet_rows if int(r["In box"]) > 0 and variant_of(r) not in tracked]
    imported, skipped, errors = 0, 0, []
    if new_rows:
        result = manabox_import.import_rows(to_manabox_rows(new_rows), owner=owner, remove_absent=False)
        if not result["imported"]:
            raise RuntimeError(f"Sheet sync imported nothing ({result['skipped']} skipped) — is Scryfall reachable?")
        imported, skipped, errors = result["imported"], result["skipped"], result["errors"]

    updated = 0
    for variant_id, copies in held.items():
        item = tracked.get(variant_id)
        if item and item["quantity"] != copies:
            db.set_quantity(item["id"], copies)
            updated += 1
    for item in stale:
        db.remove_from_watchlist(item["id"], keep_history=True)
    logger.info("Sheet sync (%s): %d new, %d count changes, %d removed, %d skipped", owner, imported, updated, len(stale), skipped)
    for err in errors:
        logger.warning("Sheet sync: %s", err)
    return bool(imported or updated or stale)


def main():
    sync(OWNER, fetch_rows())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except Exception:
        logger.exception("Sheet sync failed")
        sys.exit(1)
