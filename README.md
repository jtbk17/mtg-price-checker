# MTG Price Checker

Tracks Magic: The Gathering card prices from Card Kingdom (market + buylist),
with search and card art from Scryfall. No API keys or paid accounts needed —
every data source is free and keyless, and nightly tracking runs entirely on
GitHub's infrastructure, not your machine.

## Parts

1. **Local app** (`app.py`) — search for cards and add them to your watchlist.
   Only needed when you want to add a *new* card to track.
2. **GitHub Actions** (`.github/workflows/nightly-refresh.yml`) — runs daily
   at 3pm SGT (07:00 UTC — shortly after MTGJSON's price feed refreshes for
   the day, ~06:00-06:10 UTC), with no laptop required:
   - refreshes prices for every watchlist card
   - snapshots today's market price for **every** Card Kingdom-priced card
     (~85,000+), building indefinite price history over time
3. **GitHub Pages dashboard** (`docs/index.html`) — a read-only page showing
   your watchlist's current prices/history and a **market movers** section
   (biggest daily/weekly gainers and losers across every card), updated
   nightly.

## Running the local app

```
py -m pip install -r requirements.txt
py app.py
```
Open http://127.0.0.1:5000, search, and hit **Track** (or use **Import
ManaBox CSV** to bulk-add a whole collection export at once — matched by
its `Scryfall ID` column). Tracking, untracking, or importing automatically
commits and pushes `tcg_prices.db` for you, so changes show up in the
nightly job and dashboard without a manual git step. If a push fails (e.g.
you're offline, or there's a conflict), it's logged to the console and the
change stays committed locally — just run `git push` yourself once you're
able to.

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
- **Pages snapshot** (`docs/watchlist.json`, `docs/movers.json`): plain JSON
  exports of the watchlist + its history, and the day/week's biggest movers,
  regenerated every run for the dashboard to read. Committed to git since
  they're small.

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
- `all_cards_history.py` — nightly full-catalog snapshot, card-name backfill, and movers computation (needs `ijson`, only used in CI)
- `manabox_import.py` — bulk-import a ManaBox CSV export into the watchlist
- `templates/`, `static/` — local app frontend
- `docs/` — GitHub Pages dashboard
- `.github/workflows/` — the nightly GitHub Actions job
