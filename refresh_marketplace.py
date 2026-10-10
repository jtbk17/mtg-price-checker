"""On-demand TheTCGMarketplace refresh, run by refresh-marketplace.yml
(the dashboard's "Refresh marketplace data" button): re-prices the
watchlist's TheTCGMarketplace prices and re-finds Market deals, then
re-exports docs/watchlist.json and docs/deals.json.

Deliberately leaves out everything else the nightly job does — Card
Kingdom price history, market movers, Telegram alerts — so it's safe to
run any time of day, as often as wanted. (The nightly job's movers and
alerts depend on Card Kingdom's daily price update, which lands mid-
afternoon SGT; running those earlier wipes the movers and uses up the
day's alerts.)
"""

import logging
from datetime import date

import all_cards_history
import db
import market_deals
import refresh_job

logger = logging.getLogger("tcg-price-checker")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    items = db.list_watchlist()
    logger.info("Refreshing TheTCGMarketplace prices for %d watched card(s)", len(items))
    refresh_job.refresh_tcgmarketplace_prices(items)
    refresh_job.export_snapshot()
    market_deals.export_deals(all_cards_history.db_path_for_year(date.today().year))
