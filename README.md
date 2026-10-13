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
syncs `tcg_prices.db` to the GitHub Release asset the nightly job and
dashboard also read from (see `release_sync.py`), so changes show up there
without a manual step. If the sync fails (e.g. you're offline), it's logged
to the console and your local change is still saved — it'll sync next time
you make an edit while online.

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

- **Watchlist + price history** (`tcg_prices.db`): **not** committed to git —
  it grew past GitHub's 100MB-per-file push limit once price history built
  up, so like the all-cards database below, it's stored as an asset on a
  GitHub Release named `data` instead. The nightly job, the Telegram-feedback
  webhook, and the local app (`release_sync.py`) all download/edit/upload it
  against that same asset, using a compare-and-swap check (the asset's
  content digest) so two of them writing around the same time can't silently
  clobber each other — whichever one detects the asset changed underneath it
  just re-downloads and redoes its edit.
- **All-cards history** (`all_cards_<year>.db`, e.g. `all_cards_2026.db`):
  one row per card per day, for every card Card Kingdom prices. Also a
  GitHub Release asset (same `data` release), downloaded and re-uploaded by
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

## Project structure

- `app.py` — Flask routes for the local app
- `scryfall.py` — card search + images (Scryfall API)
- `mtgjson_crosswalk.py` — Scryfall id → MTGJSON uuid lookup, per set
- `cardkingdom.py` — Card Kingdom market/buylist prices (MTGJSON price feed)
- `db.py` — watchlist SQLite schema and queries
- `release_sync.py` — compare-and-swap sync of `tcg_prices.db` against its GitHub Release asset
- `refresh_job.py` — nightly watchlist refresh + dashboard snapshot export
- `all_cards_history.py` — nightly full-catalog snapshot, card-name backfill, and movers computation (needs `ijson`, only used in CI)
- `manabox_import.py` — bulk-import a ManaBox CSV export into the watchlist
- `templates/`, `static/` — local app frontend
- `docs/` — GitHub Pages dashboard
- `.github/workflows/` — the nightly GitHub Actions job
