# MTG Price Checker

Local web app for tracking Magic: The Gathering card prices from Card Kingdom
(market + buylist), with search and card art from Scryfall.

No API keys or accounts needed — every data source is free and keyless.

## Setup

1. Install dependencies:
   ```
   py -m pip install -r requirements.txt
   ```
2. Run the app:
   ```
   py app.py
   ```
3. Open http://127.0.0.1:5000

## How it works

- **Search** hits [Scryfall's](https://scryfall.com) free card search API — the
  standard Magic card database. Each result shows every printing/set, with a
  dropdown for Normal/Foil/Etched when a printing has more than one finish.
- **Prices** come from Card Kingdom, via [MTGJSON's](https://mtgjson.com) daily
  price feed. A card is linked to its Card Kingdom price by looking up its
  MTGJSON uuid through a small per-set crosswalk file (fetched and cached the
  first time a set is seen).
- **Track** saves a specific card + finish to a local SQLite database
  (`tcg_prices.db`) and records today's market price as the first history point.
- A background job refreshes every tracked card's price once a day (6am) and
  appends a new history point. You can also hit **Refresh prices** manually.
- **History** shows a price-over-time chart per tracked card, built from your
  own refreshes (there's no historical backfill — pricing only accumulates
  from when a card is first tracked).

## Notes

- Card Kingdom doesn't stock or buy back every printing — a blank price means
  they don't currently offer it, not a bug.
- The first search for a card whose set hasn't been seen before will fetch and
  cache that set's MTGJSON file (a few MB); later lookups for cards in the
  same set are instant.

## Project structure

- `app.py` — Flask routes + scheduler
- `scryfall.py` — card search + images (Scryfall API)
- `mtgjson_crosswalk.py` — Scryfall id → MTGJSON uuid lookup, per set
- `cardkingdom.py` — Card Kingdom market/buylist prices (MTGJSON price feed)
- `db.py` — SQLite schema and queries
- `templates/`, `static/` — frontend (vanilla HTML/CSS/JS)
