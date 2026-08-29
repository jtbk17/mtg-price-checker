# MTG Price Checker

Tracks Magic: The Gathering card prices from Card Kingdom (market + buylist),
with search and card art from Scryfall. No API keys or paid accounts needed —
every data source is free and keyless, and nightly tracking runs entirely on
GitHub's infrastructure, not your machine.

## Parts

1. **Local app** (`app.py`) — search for cards and add them to your watchlist.
   Only needed when you want to add a *new* card to track.
2. **GitHub Actions** (`.github/workflows/nightly-refresh.yml`) — runs nightly
   at 2am SGT, with no laptop required:
   - refreshes prices for every watchlist card
   - snapshots today's market price for **every** Card Kingdom-priced card
     (~85,000+), building indefinite price history over time
3. **GitHub Pages dashboard** (`docs/index.html`) — a read-only page showing
   your watchlist's current prices and history charts, updated nightly.

## Running the local app

```
py -m pip install -r requirements.txt
py app.py
```
Open http://127.0.0.1:5000, search, and hit **Track**. This writes to your
local `tcg_prices.db`. To sync a newly tracked card so it starts showing up
in the nightly job and dashboard:
```
git pull
git add tcg_prices.db
git commit -m "track new card"
git push
```

## How pricing works

- **Search** hits [Scryfall's](https://scryfall.com) free card search API —
  the standard Magic card database. Each result shows every printing/set,
  with a dropdown for Normal/Foil/Etched when a printing has more than one
  finish.
- **Prices** come from Card Kingdom, via [MTGJSON's](https://mtgjson.com)
  daily price feed. A card is linked to its Card Kingdom price by looking up
  its MTGJSON uuid through a small per-set crosswalk file (fetched and
  cached the first time a set is seen).
- Card Kingdom doesn't stock or buy back every printing — a blank price means
  they don't currently offer it, not a bug.

## Where the data lives

- **Watchlist** (`tcg_prices.db`): small, committed straight into the git
  repo. Every nightly run and every local track/refresh updates it.
- **All-cards history** (`all_cards_<year>.db`, e.g. `all_cards_2026.db`):
  one row per card per day, for every card Card Kingdom prices. This file is
  **not** committed to git (it would bloat the repo) — instead it's stored as
  an asset on a GitHub Release named `data`, downloaded and re-uploaded by
  the nightly workflow each run. A fresh file starts each calendar year (to
  stay under GitHub's 2GB-per-file limit) and is automatically seeded with
  MTGJSON's own ~88-day rolling history the first time it's created, so
  there's no cold-start gap.
- **Pages snapshot** (`docs/watchlist.json`): a plain JSON export of the
  watchlist + its history, regenerated every run for the dashboard to read.
  Also committed to git since it's small.

## Notes

- The first search for a card whose set hasn't been seen before will fetch
  and cache that set's MTGJSON file (a few MB); later lookups for cards in
  the same set are instant.
- If you edit `tcg_prices.db` locally around the same time the nightly job
  runs, `git pull` before pushing — the timestamp in `docs/watchlist.json`
  changes every run, so pushes without pulling first will be rejected.

## Project structure

- `app.py` — Flask routes for the local app
- `scryfall.py` — card search + images (Scryfall API)
- `mtgjson_crosswalk.py` — Scryfall id → MTGJSON uuid lookup, per set
- `cardkingdom.py` — Card Kingdom market/buylist prices (MTGJSON price feed)
- `db.py` — watchlist SQLite schema and queries
- `refresh_job.py` — nightly watchlist refresh + dashboard snapshot export
- `all_cards_history.py` — nightly full-catalog price snapshot + yearly backfill
- `templates/`, `static/` — local app frontend
- `docs/` — GitHub Pages dashboard
- `.github/workflows/` — the nightly GitHub Actions job
