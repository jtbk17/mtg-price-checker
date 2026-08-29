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
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (variant_id, owner)
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id TEXT NOT NULL,
    price REAL,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_variant_time
    ON price_history (variant_id, recorded_at);

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
        ):
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE watchlist ADD COLUMN {column} {coltype}")

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
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (variant_id, owner)
                );
                INSERT INTO watchlist_new
                    (id, variant_id, card_id, game, name, set_name, condition, printing,
                     tcgplayer_id, image_url, mtgjson_id, cardkingdom_price,
                     cardkingdom_buylist_price, owner, created_at)
                SELECT id, variant_id, card_id, game, name, set_name, condition, printing,
                       tcgplayer_id, image_url, mtgjson_id, cardkingdom_price,
                       cardkingdom_buylist_price, owner, created_at
                FROM watchlist;
                DROP TABLE watchlist;
                ALTER TABLE watchlist_new RENAME TO watchlist;
                """
            )
        conn.commit()
    finally:
        conn.close()


def add_to_watchlist(card):
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO watchlist (variant_id, card_id, game, name, set_name, condition, printing, tcgplayer_id, image_url, mtgjson_id, cardkingdom_price, cardkingdom_buylist_price, owner)
            VALUES (:variant_id, :card_id, :game, :name, :set_name, :condition, :printing, :tcgplayer_id, :image_url, :mtgjson_id, :cardkingdom_price, :cardkingdom_buylist_price, :owner)
            ON CONFLICT(variant_id, owner) DO NOTHING
            """,
            card,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM watchlist WHERE variant_id = ? AND owner IS ?",
            (card["variant_id"], card.get("owner")),
        ).fetchone()
        if card.get("price") is not None:
            record_price(row["variant_id"], card["price"])
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


def record_price(variant_id, price):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO price_history (variant_id, price) VALUES (?, ?)",
            (variant_id, price),
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
                "SELECT price, recorded_at FROM price_history WHERE variant_id = ? ORDER BY recorded_at DESC LIMIT 2",
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


def get_history(variant_id):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT price, recorded_at FROM price_history WHERE variant_id = ? ORDER BY recorded_at ASC",
            (variant_id,),
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
