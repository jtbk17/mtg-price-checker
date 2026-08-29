"""Nightly market-price snapshots for every Magic card Card Kingdom prices
(not just the watchlist), stored in a SQLite file that lives as a GitHub
Release asset rather than being committed into the repo — this keeps the
git history small while the data file itself can grow up to GitHub's 2GB
per-asset limit.

The file is rotated once per calendar year (all_cards_2026.db,
all_cards_2027.db, ...) so no single file ever approaches that cap.

This module only touches the database file on disk; downloading it from
and uploading it back to the GitHub Release is done by the calling
workflow step via `gh release download` / `gh release upload`, so this
code has no GitHub-specific logic and can be tested locally.
"""

import gzip
import json
import logging
import sqlite3
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger("tcg-price-checker")

ALLPRICES_URL = "https://mtgjson.com/api/v5/AllPrices.json.gz"
ALLIDENTIFIERS_URL = "https://mtgjson.com/api/v5/AllIdentifiers.json.gz"
DB_DIR = Path(__file__).parent
DOCS_DIR = DB_DIR / "docs"
SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    mtgjson_uuid TEXT UNIQUE NOT NULL,
    name TEXT,
    set_code TEXT,
    is_token INTEGER
);

CREATE TABLE IF NOT EXISTS price_history (
    card_id INTEGER NOT NULL REFERENCES cards(id),
    day INTEGER NOT NULL,
    price_cents INTEGER,
    PRIMARY KEY (card_id, day)
) WITHOUT ROWID;
"""

EPOCH = date(2020, 1, 1)


def db_path_for_year(year):
    return DB_DIR / f"all_cards_{year}.db"


def _day_number(d):
    return (d - EPOCH).days


def _get_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
    for column, coltype in (("name", "TEXT"), ("set_code", "TEXT"), ("is_token", "INTEGER")):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {column} {coltype}")
    return conn


def _card_id(conn, uuid_cache, uuid):
    if uuid in uuid_cache:
        return uuid_cache[uuid]
    cur = conn.execute("INSERT OR IGNORE INTO cards (mtgjson_uuid) VALUES (?)", (uuid,))
    row = conn.execute("SELECT id FROM cards WHERE mtgjson_uuid = ?", (uuid,)).fetchone()
    uuid_cache[uuid] = row[0]
    return row[0]


def snapshot_today(db_path=None):
    """Fetch today's Card Kingdom market price for every card and append
    one row per card to today's price_history."""
    import cardkingdom  # local import to avoid a hard dependency for callers that only backfill

    today = date.today()
    db_path = db_path or db_path_for_year(today.year)
    conn = _get_connection(db_path)
    uuid_cache = {row[1]: row[0] for row in conn.execute("SELECT id, mtgjson_uuid FROM cards")}

    prices = cardkingdom.get_all_prices()  # {uuid: {"retail_normal": ..., "retail_foil": ..., ...}}
    day = _day_number(today)
    rows = []
    for uuid, entry in prices.items():
        price = entry.get("retail_normal")
        if price is None:
            continue
        card_id = _card_id(conn, uuid_cache, uuid)
        rows.append((card_id, day, round(price * 100)))

    conn.executemany(
        "INSERT OR REPLACE INTO price_history (card_id, day, price_cents) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    logger.info("Snapshotted %d cards into %s", len(rows), db_path)


def backfill_88_days(db_path=None):
    """One-time (or periodic) seed of MTGJSON's own ~88-day rolling Card
    Kingdom retail history into the current year's database."""
    today = date.today()
    db_path = db_path or db_path_for_year(today.year)
    conn = _get_connection(db_path)
    uuid_cache = {row[1]: row[0] for row in conn.execute("SELECT id, mtgjson_uuid FROM cards")}

    logger.info("Downloading MTGJSON AllPrices.json for 88-day backfill (~150MB)...")
    req = urllib.request.Request(ALLPRICES_URL, headers={"User-Agent": "tcg-price-checker/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read()
    data = json.loads(gzip.decompress(raw))

    rows = []
    for uuid, entry in data.get("data", {}).items():
        ck = entry.get("paper", {}).get("cardkingdom")
        if not ck:
            continue
        for day_str, price in ck.get("retail", {}).get("normal", {}).items():
            try:
                d = datetime.strptime(day_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d.year != today.year:
                continue  # older days belong in a prior year's file; skip for this pass
            card_id = _card_id(conn, uuid_cache, uuid)
            rows.append((card_id, _day_number(d), round(price * 100)))

    conn.executemany(
        "INSERT OR REPLACE INTO price_history (card_id, day, price_cents) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    logger.info("Backfilled %d price points into %s", len(rows), db_path)


def backfill_names(db_path):
    """Fill in name/set_code/is_token for any card uuids missing a label
    (either brand new, or — the first time this runs after is_token was
    added — every existing card needing that one field caught up), by
    streaming MTGJSON's AllIdentifiers.json (full card database, ~2-3GB
    uncompressed) and picking out just the uuids we need. Only runs when
    there's actually something missing, since this is an expensive fetch."""
    import ijson

    conn = _get_connection(db_path)
    needed = {
        row[0] for row in conn.execute("SELECT mtgjson_uuid FROM cards WHERE name IS NULL OR is_token IS NULL")
    }
    if not needed:
        conn.close()
        return

    logger.info("Fetching labels for %d card(s) from MTGJSON AllIdentifiers.json...", len(needed))
    req = urllib.request.Request(ALLIDENTIFIERS_URL, headers={"User-Agent": "tcg-price-checker/1.0"})
    updates = []
    with urllib.request.urlopen(req, timeout=600) as resp:
        with gzip.GzipFile(fileobj=resp) as stream:
            for uuid, entry in ijson.kvitems(stream, "data"):
                if uuid in needed:
                    is_token = 1 if entry.get("layout") == "token" else 0
                    updates.append((entry.get("name"), entry.get("setCode"), is_token, uuid))
                    needed.discard(uuid)
                    if not needed:
                        break

    conn.executemany(
        "UPDATE cards SET name = ?, set_code = ?, is_token = ? WHERE mtgjson_uuid = ?", updates
    )
    conn.commit()
    conn.close()
    logger.info("Updated labels for %d card(s)", len(updates))


def compute_movers(db_path, top_n=15):
    """Biggest day-over-day and week-over-week movers across every card
    Card Kingdom prices, using the history this module has been
    collecting. No price floor — a cheap card doubling in price is exactly
    the kind of move worth surfacing. Excludes tokens specifically
    (layout == "token" in MTGJSON), since those are the actual source of
    meaningless noise (e.g. a generic Soldier token blipping between
    $0.35 and $0.99), not low price alone. Also excludes cards with no
    actual change (padding a short real-movers list with 0% entries isn't
    useful)."""
    conn = _get_connection(db_path)
    today_day = _day_number(date.today())

    def top_movers(days_back):
        rows = conn.execute(
            """
            SELECT c.name, c.set_code, t.price_cents, p.price_cents
            FROM price_history t
            JOIN price_history p ON p.card_id = t.card_id AND p.day = ?
            JOIN cards c ON c.id = t.card_id
            WHERE t.day = ? AND c.name IS NOT NULL AND c.is_token = 0
                AND t.price_cents != p.price_cents
            """,
            (today_day - days_back, today_day),
        ).fetchall()
        movers = [
            {
                "name": name,
                "set": set_code,
                "price_now": now_cents / 100,
                "price_before": before_cents / 100,
                "pct_change": round((now_cents - before_cents) / before_cents * 100, 1),
            }
            for name, set_code, now_cents, before_cents in rows
        ]
        gainers = sorted(
            (m for m in movers if m["pct_change"] > 0), key=lambda m: m["pct_change"], reverse=True
        )[:top_n]
        losers = sorted((m for m in movers if m["pct_change"] < 0), key=lambda m: m["pct_change"])[:top_n]
        return gainers, losers

    daily_gainers, daily_losers = top_movers(1)
    weekly_gainers, weekly_losers = top_movers(7)
    conn.close()
    return {
        "daily_gainers": daily_gainers,
        "daily_losers": daily_losers,
        "weekly_gainers": weekly_gainers,
        "weekly_losers": weekly_losers,
    }


def export_movers(db_path):
    movers = compute_movers(db_path)
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "movers.json").write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), **movers}, indent=2)
    )
    logger.info(
        "Wrote movers.json (%d daily gainers, %d daily losers)",
        len(movers["daily_gainers"]),
        len(movers["daily_losers"]),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    year_db = db_path_for_year(date.today().year)
    if not year_db.exists():
        logger.info("No database for %s yet — seeding with an 88-day backfill first", date.today().year)
        backfill_88_days(year_db)
    snapshot_today(year_db)
    backfill_names(year_db)
    export_movers(year_db)
