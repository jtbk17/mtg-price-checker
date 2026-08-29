import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "tcg_prices.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id TEXT UNIQUE NOT NULL,
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
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id TEXT NOT NULL,
    price REAL,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_variant_time
    ON price_history (variant_id, recorded_at);
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
        ):
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE watchlist ADD COLUMN {column} {coltype}")
        conn.commit()
    finally:
        conn.close()


def add_to_watchlist(card):
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO watchlist (variant_id, card_id, game, name, set_name, condition, printing, tcgplayer_id, image_url, mtgjson_id, cardkingdom_price, cardkingdom_buylist_price)
            VALUES (:variant_id, :card_id, :game, :name, :set_name, :condition, :printing, :tcgplayer_id, :image_url, :mtgjson_id, :cardkingdom_price, :cardkingdom_buylist_price)
            ON CONFLICT(variant_id) DO NOTHING
            """,
            card,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM watchlist WHERE variant_id = ?", (card["variant_id"],)).fetchone()
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


def list_watchlist():
    conn = get_connection()
    try:
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
