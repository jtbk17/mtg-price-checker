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

Split/adventure/double-faced cards get one MTGJSON card object — and
uuid — per face, all sharing the same Scryfall id (mirrors the same
quirk mtgjson_crosswalk.py works around for search/watchlist pricing).
Here that means the same physical card can land in *two* `cards` rows,
each only getting a price snapshot on the days MTGJSON's feed happens
to link Card Kingdom's price to that particular face — confirmed (by
inspecting the real data) to alternate day to day with no overlap,
never both on the same day. reconcile_canonical_groups() groups such
rows under one canonical id (the lowest) so compute_movers() and
all_cards_lookup.py see one continuous price series per physical card
instead of two series each full of gaps that rarely line up with
themselves day-over-day.
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
SETLIST_URL = "https://mtgjson.com/api/v5/SetList.json"
DB_DIR = Path(__file__).parent
DOCS_DIR = DB_DIR / "docs"
SET_NAMES_CACHE = DB_DIR / "set_names_cache.json"
SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    mtgjson_uuid TEXT UNIQUE NOT NULL,
    name TEXT,
    set_code TEXT,
    set_name TEXT,
    scryfall_id TEXT,
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
    for column, coltype in (
        ("name", "TEXT"),
        ("set_code", "TEXT"),
        ("set_name", "TEXT"),
        ("scryfall_id", "TEXT"),
        ("is_token", "INTEGER"),
        ("canonical_card_id", "INTEGER"),
    ):
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


def _load_set_names():
    """{set_code: full set name}, e.g. "SLD" -> "Secret Lair Drop". Cached
    indefinitely (set names essentially never change) rather than on the
    usual TTL pattern — refetch by deleting set_names_cache.json if MTGJSON
    ever renames a set."""
    if SET_NAMES_CACHE.exists():
        return json.loads(SET_NAMES_CACHE.read_text())

    logger.info("Fetching set names from MTGJSON SetList.json...")
    req = urllib.request.Request(SETLIST_URL, headers={"User-Agent": "tcg-price-checker/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())

    names = {s["code"]: s["name"] for s in data.get("data", [])}
    SET_NAMES_CACHE.write_text(json.dumps(names))
    return names


def backfill_names(db_path):
    """Fill in name/set_code/set_name/scryfall_id/is_token for any card
    uuids missing a label (either brand new, or — the first time this runs
    after a new field was added — every existing card needing just that
    one field caught up), by streaming MTGJSON's AllIdentifiers.json (full
    card database, ~2-3GB uncompressed) and picking out just the uuids we
    need. Only runs when there's actually something missing, since this is
    an expensive fetch."""
    import ijson

    conn = _get_connection(db_path)
    needed = {
        row[0]
        for row in conn.execute(
            "SELECT mtgjson_uuid FROM cards WHERE name IS NULL OR is_token IS NULL OR set_name IS NULL"
        )
    }
    if not needed:
        conn.close()
        return

    set_names = _load_set_names()

    logger.info("Fetching labels for %d card(s) from MTGJSON AllIdentifiers.json...", len(needed))
    req = urllib.request.Request(ALLIDENTIFIERS_URL, headers={"User-Agent": "tcg-price-checker/1.0"})
    updates = []
    with urllib.request.urlopen(req, timeout=600) as resp:
        with gzip.GzipFile(fileobj=resp) as stream:
            for uuid, entry in ijson.kvitems(stream, "data"):
                if uuid in needed:
                    is_token = 1 if entry.get("layout") == "token" else 0
                    set_code = entry.get("setCode")
                    scryfall_id = entry.get("identifiers", {}).get("scryfallId")
                    updates.append(
                        (entry.get("name"), set_code, set_names.get(set_code, set_code), scryfall_id, is_token, uuid)
                    )
                    needed.discard(uuid)
                    if not needed:
                        break

    conn.executemany(
        "UPDATE cards SET name = ?, set_code = ?, set_name = ?, scryfall_id = ?, is_token = ? "
        "WHERE mtgjson_uuid = ?",
        updates,
    )
    conn.commit()
    conn.close()
    logger.info("Updated labels for %d card(s)", len(updates))


def reconcile_canonical_groups(db_path):
    """Group `cards` rows that share a Scryfall id (see module docstring)
    under one canonical id — the lowest — so movers/history queries treat
    them as one card. Idempotent and cheap: run after every backfill_names()
    call, since a newly-labeled card can reveal a new group that wasn't
    visible while its scryfall_id was still NULL."""
    conn = _get_connection(db_path)
    groups = conn.execute(
        """
        SELECT GROUP_CONCAT(id) FROM cards
        WHERE scryfall_id IS NOT NULL
        GROUP BY scryfall_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    updates = []
    for (ids_csv,) in groups:
        ids = sorted(int(i) for i in ids_csv.split(","))
        canonical_id = ids[0]
        updates.extend((canonical_id, child_id) for child_id in ids[1:])

    if updates:
        conn.executemany("UPDATE cards SET canonical_card_id = ? WHERE id = ?", updates)

    # A canonical row can itself still be missing labels a child row
    # already has (e.g. backfill_names labeled the child first) — copy
    # them over so the canonical row is always the fully-labeled source
    # of truth compute_movers()/all_cards_lookup.py read from.
    conn.execute(
        """
        UPDATE cards SET
            name = COALESCE(name, (SELECT c2.name FROM cards c2 WHERE c2.canonical_card_id = cards.id AND c2.name IS NOT NULL LIMIT 1)),
            set_code = COALESCE(set_code, (SELECT c2.set_code FROM cards c2 WHERE c2.canonical_card_id = cards.id AND c2.set_code IS NOT NULL LIMIT 1)),
            set_name = COALESCE(set_name, (SELECT c2.set_name FROM cards c2 WHERE c2.canonical_card_id = cards.id AND c2.set_name IS NOT NULL LIMIT 1)),
            is_token = COALESCE(is_token, (SELECT c2.is_token FROM cards c2 WHERE c2.canonical_card_id = cards.id AND c2.is_token IS NOT NULL LIMIT 1))
        WHERE canonical_card_id IS NULL
        """
    )
    conn.commit()
    conn.close()
    if updates:
        logger.info("Reconciled %d card(s) into canonical groups", len(updates))


def compute_movers(db_path, top_n=15):
    """Biggest day-over-day and week-over-week movers across every card
    Card Kingdom prices, using the history this module has been
    collecting (the 7-day window relies on the initial backfill_88_days()
    seed plus however many nightly snapshots have accumulated since). No
    price floor — a cheap card doubling in price is exactly the kind of
    move worth surfacing. Excludes tokens specifically (layout == "token"
    in MTGJSON), since those are the actual source of meaningless noise
    (e.g. a generic Soldier token blipping between $0.35 and $0.99), not
    low price alone. Also excludes cards with no actual change (padding a
    short real-movers list with 0% entries isn't useful)."""
    conn = _get_connection(db_path)
    today_day = _day_number(date.today())

    def top_movers(days_back):
        # Grouping by COALESCE(canonical_card_id, id) (see reconcile_canonical_groups)
        # merges a split/adventure/DFC card's two per-face rows into one
        # series before comparing days — confirmed no day ever has a price
        # on both rows, so this merge can't produce duplicate/conflicting
        # matches. `p.price_cents != 0` guards a divide-by-zero below; it
        # has never actually occurred in the feed, but a crash here would
        # silently skip that night's alerts entirely, so it's cheap
        # insurance either way.
        rows = conn.execute(
            """
            WITH merged AS (
                SELECT COALESCE(c.canonical_card_id, c.id) AS gid, ph.day, ph.price_cents
                FROM price_history ph
                JOIN cards c ON c.id = ph.card_id
            )
            SELECT g.name, g.set_code, g.set_name, g.scryfall_id, t.price_cents, p.price_cents
            FROM merged t
            JOIN merged p ON p.gid = t.gid AND p.day = ?
            JOIN cards g ON g.id = t.gid
            WHERE t.day = ? AND g.name IS NOT NULL AND g.is_token = 0
                AND t.price_cents != p.price_cents AND p.price_cents != 0
            """,
            (today_day - days_back, today_day),
        ).fetchall()
        movers = [
            {
                "name": name,
                "set": set_code,
                "set_full_name": set_name or set_code,
                "scryfall_id": scryfall_id,
                "image_url": f"https://api.scryfall.com/cards/{scryfall_id}?format=image&version=normal"
                if scryfall_id
                else None,
                "price_now": now_cents / 100,
                "price_before": before_cents / 100,
                "pct_change": round((now_cents - before_cents) / before_cents * 100, 1),
            }
            for name, set_code, set_name, scryfall_id, now_cents, before_cents in rows
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
    reconcile_canonical_groups(year_db)
    export_movers(year_db)
