import os
import sqlite3
from pathlib import Path

# Overridable so tests can point at a throwaway file instead of the real
# production database (must be set before this module is first imported).
DB_PATH = Path(os.environ.get("TCG_DB_PATH", Path(__file__).parent / "tcg_prices.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id TEXT NOT NULL,
    card_id TEXT,
    game TEXT,
    name TEXT,
    set_name TEXT,
    condition TEXT,
    printing TEXT,
    tcgplayer_id TEXT,
    image_url TEXT,
    mtgjson_id TEXT,
    cardkingdom_price REAL,
    cardkingdom_buylist_price REAL,
    owner TEXT,
    quantity INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (variant_id, owner)
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id TEXT NOT NULL,
    price REAL,
    kind TEXT NOT NULL DEFAULT 'market',
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name TEXT,
    set_name TEXT,
    price_before REAL,
    price_now REAL,
    pct_change REAL,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
    telegram_chat_id TEXT,
    telegram_message_id TEXT,
    feedback TEXT,
    feedback_at TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(watchlist)")}
        for column, coltype in (
            ("mtgjson_id", "TEXT"),
            ("cardkingdom_price", "REAL"),
            ("cardkingdom_buylist_price", "REAL"),
            ("owner", "TEXT"),
            ("quantity", "INTEGER DEFAULT 1"),
        ):
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE watchlist ADD COLUMN {column} {coltype}")

        price_history_columns = {row["name"] for row in conn.execute("PRAGMA table_info(price_history)")}
        if "kind" not in price_history_columns:
            conn.execute("ALTER TABLE price_history ADD COLUMN kind TEXT NOT NULL DEFAULT 'market'")
            # The old unique index was (variant_id, recorded_at); a buylist
            # point recorded in the same instant as a market point would
            # collide with it, so it has to be replaced rather than kept
            # alongside the new one.
            conn.execute("DROP INDEX IF EXISTS idx_price_history_variant_time")

        # Created here (not in SCHEMA) so it always runs after the `kind`
        # column above is guaranteed to exist, whether that's from the
        # ALTER TABLE just above or because CREATE TABLE already included
        # it for a brand new database.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_variant_kind_time "
            "ON price_history (variant_id, kind, recorded_at)"
        )

        # Normalize any pre-existing NULL owners to '' — see the comment in
        # add_to_watchlist() for why NULL doesn't work as the "no owner"
        # sentinel with a UNIQUE(variant_id, owner) constraint.
        conn.execute("UPDATE watchlist SET owner = '' WHERE owner IS NULL")

        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='watchlist'"
        ).fetchone()[0]
        if "UNIQUE (variant_id, owner)" not in table_sql:
            # Older databases had a bare UNIQUE(variant_id), which blocks a
            # second owner from tracking the same card+finish. SQLite can't
            # alter a constraint in place, so recreate the table.
            conn.executescript(
                """
                CREATE TABLE watchlist_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    variant_id TEXT NOT NULL,
                    card_id TEXT,
                    game TEXT,
                    name TEXT,
                    set_name TEXT,
                    condition TEXT,
                    printing TEXT,
                    tcgplayer_id TEXT,
                    image_url TEXT,
                    mtgjson_id TEXT,
                    cardkingdom_price REAL,
                    cardkingdom_buylist_price REAL,
                    owner TEXT,
                    quantity INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (variant_id, owner)
                );
                INSERT INTO watchlist_new
                    (id, variant_id, card_id, game, name, set_name, condition, printing,
                     tcgplayer_id, image_url, mtgjson_id, cardkingdom_price,
                     cardkingdom_buylist_price, owner, quantity, created_at)
                SELECT id, variant_id, card_id, game, name, set_name, condition, printing,
                       tcgplayer_id, image_url, mtgjson_id, cardkingdom_price,
                       cardkingdom_buylist_price, owner, quantity, created_at
                FROM watchlist;
                DROP TABLE watchlist;
                ALTER TABLE watchlist_new RENAME TO watchlist;
                """
            )
        conn.commit()
    finally:
        conn.close()


_WATCHLIST_FIELDS = (
    "variant_id",
    "card_id",
    "game",
    "name",
    "set_name",
    "condition",
    "printing",
    "tcgplayer_id",
    "image_url",
    "mtgjson_id",
    "cardkingdom_price",
    "cardkingdom_buylist_price",
    "owner",
)


def add_to_watchlist(card):
    conn = get_connection()
    try:
        # Callers only need to pass the fields they actually have — treat
        # anything else as null rather than crashing on a missing binding.
        # `price` (the initial market price to seed history with) isn't a
        # watchlist column, so it's carried over separately.
        price = card.get("price")
        quantity = card.get("quantity") or 1
        card = {field: card.get(field) for field in _WATCHLIST_FIELDS}
        card["quantity"] = quantity
        card["price"] = price
        # SQL's UNIQUE(variant_id, owner) treats every NULL as distinct
        # from every other NULL, so an untagged card would never actually
        # be deduplicated against itself — a second "track" of the exact
        # same card+finish would silently insert a duplicate row instead
        # of hitting ON CONFLICT. Normalize "no owner" to '' so the
        # constraint (and re-tracking with an updated quantity) works.
        card["owner"] = card.get("owner") or ""
        conn.execute(
            """
            INSERT INTO watchlist (variant_id, card_id, game, name, set_name, condition, printing, tcgplayer_id, image_url, mtgjson_id, cardkingdom_price, cardkingdom_buylist_price, owner, quantity)
            VALUES (:variant_id, :card_id, :game, :name, :set_name, :condition, :printing, :tcgplayer_id, :image_url, :mtgjson_id, :cardkingdom_price, :cardkingdom_buylist_price, :owner, :quantity)
            ON CONFLICT(variant_id, owner) DO UPDATE SET quantity = excluded.quantity
            """,
            card,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM watchlist WHERE variant_id = ? AND owner IS ?",
            (card["variant_id"], card.get("owner")),
        ).fetchone()
        if card.get("price") is not None:
            record_price(row["variant_id"], card["price"], kind="market")
        if card.get("cardkingdom_buylist_price") is not None:
            record_price(row["variant_id"], card["cardkingdom_buylist_price"], kind="buylist")
        return dict(row)
    finally:
        conn.close()


def remove_from_watchlist(watchlist_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT variant_id FROM watchlist WHERE id = ?", (watchlist_id,)).fetchone()
        conn.execute("DELETE FROM watchlist WHERE id = ?", (watchlist_id,))
        if row:
            conn.execute("DELETE FROM price_history WHERE variant_id = ?", (row["variant_id"],))
        conn.commit()
    finally:
        conn.close()


def record_price(variant_id, price, kind="market"):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO price_history (variant_id, price, kind) VALUES (?, ?, ?)",
            (variant_id, price, kind),
        )
        conn.commit()
    finally:
        conn.close()


def list_watchlist(owner=None):
    conn = get_connection()
    try:
        if owner:
            rows = conn.execute(
                "SELECT * FROM watchlist WHERE owner = ? ORDER BY created_at DESC", (owner,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM watchlist ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            history = conn.execute(
                "SELECT price, recorded_at FROM price_history WHERE variant_id = ? AND kind = 'market' "
                "ORDER BY recorded_at DESC LIMIT 2",
                (item["variant_id"],),
            ).fetchall()
            item["latest_price"] = history[0]["price"] if len(history) > 0 else None
            item["previous_price"] = history[1]["price"] if len(history) > 1 else None
            result.append(item)
        return result
    finally:
        conn.close()


def list_owners():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT owner FROM watchlist WHERE owner IS NOT NULL AND owner != '' ORDER BY owner"
        ).fetchall()
        return [r["owner"] for r in rows]
    finally:
        conn.close()


def get_history(variant_id, kind="market"):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT price, recorded_at FROM price_history WHERE variant_id = ? AND kind = ? "
            "ORDER BY recorded_at ASC",
            (variant_id, kind),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_watchlist_item(watchlist_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM watchlist WHERE id = ?", (watchlist_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_cardkingdom_price(variant_id, market_price, buylist_price):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE watchlist SET cardkingdom_price = ?, cardkingdom_buylist_price = ? WHERE variant_id = ?",
            (market_price, buylist_price, variant_id),
        )
        conn.commit()
    finally:
        conn.close()


def record_recommendation(card_name, set_name, price_before, price_now, pct_change):
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO recommendations (card_name, set_name, price_before, price_now, pct_change)
            VALUES (?, ?, ?, ?, ?)
            """,
            (card_name, set_name, price_before, price_now, pct_change),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def set_recommendation_telegram_info(rec_id, chat_id, message_id):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE recommendations SET telegram_chat_id = ?, telegram_message_id = ? WHERE id = ?",
            (str(chat_id), str(message_id), rec_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_recommendation_feedback(rec_id, feedback):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE recommendations SET feedback = ?, feedback_at = CURRENT_TIMESTAMP WHERE id = ?",
            (feedback, rec_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_recommendation(rec_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM recommendations WHERE id = ?", (rec_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_labeled_recommendations():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT price_before, price_now, pct_change, feedback FROM recommendations WHERE feedback IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_state(key, default=None):
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_state(key, value):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def already_ran_today(key):
    """True if mark_ran_today(key) was already called today — lets a
    once-daily action (like sending Telegram alerts) stay safe to re-run
    if a CI retry replays the whole pipeline after a git conflict, without
    sending duplicate notifications."""
    from datetime import date

    return get_state(key) == date.today().isoformat()


def mark_ran_today(key):
    from datetime import date

    set_state(key, date.today().isoformat())
