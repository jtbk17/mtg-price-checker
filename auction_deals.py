"""TheTCGMarketplace auction deals: every live Magic auction, checked
against what GOG Cash would pay for that exact card (Card Kingdom's
buylist x GOG_CASH_MULTIPLIER) and against the cheapest matching copy
already listed for sale. Exported to docs/auctions.json for the
dashboard and local app, by auction-deals.yml every 30 minutes (auctions
run for days but bids move within hours, so nightly is too slow).

Lightweight by design: one request lists every live auction
(POST /bidding/filter — ~100 at a time), plus one detail request each
(GET /bidding/bid-details/<id>) for the buyout price and bid increment.
No login is needed for either. (The dashboard can't call these itself:
TheTCGMarketplace's API refuses requests from other websites.)

Each auction is pinned to its exact printing the same way market deals
are: set code from the product name's "[2X2]" tag, collector number from
its image filename ("2x2_356 ..."), matched to the all-cards database's
labels (see all_cards_history.backfill_names()) for the Card Kingdom
uuid. Card Kingdom's buylist is for Near Mint, so only Near Mint auctions
count as deals; others are listed but flagged.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import all_cards_history
import cardkingdom
import tcgmarketplace

logger = logging.getLogger("tcg-price-checker")

# Keep in sync with GOG_CASH_MULTIPLIER in static/app.js and docs/index.html.
GOG_CASH_MULTIPLIER = 1.15
AUCTIONS_FILE = Path(__file__).parent / "docs" / "auctions.json"
BASE_URL = tcgmarketplace.BASE_URL


def fetch_auctions():
    resp = tcgmarketplace._session.post(
        f"{BASE_URL}/bidding/filter", json={"category_id": tcgmarketplace.MTG_CATEGORY_ID}, timeout=30
    )
    resp.raise_for_status()
    return resp.json().get("data", {}).get("data") or []


def fetch_details(auction):
    """Buyout price, bid increment and live current bid for one auction,
    or {} if the detail request fails (the auction is still shown, just
    without a buyout)."""
    try:
        resp = tcgmarketplace._session.get(
            f"{BASE_URL}/bidding/bid-details/{auction['new_bidding_id']}",
            params={"source_region": auction.get("source_region")},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("data") or []
        return data[0] if data else {}
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("Auction detail fetch failed for %s: %s", auction.get("new_bidding_id"), exc)
        return {}


def _number(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _finish(auction):
    foil_type = (auction.get("crd_foil_type") or "").lower()
    if "etched" in foil_type:
        return "etched"
    return "foil" if str(auction.get("crd_foil") or "0") != "0" else "nonfoil"


def _printing_index(db_path):
    """{(set_code, collector_number, lang): [uuid, ...]} from the all-cards
    db. A list because split/double-faced cards have one uuid per face for
    the same printing, and only one of them usually carries Card Kingdom's
    prices (see mtgjson_crosswalk.py)."""
    conn = all_cards_history._get_connection(db_path)
    try:
        index = {}
        for uuid, set_code, number, lang in conn.execute(
            "SELECT mtgjson_uuid, set_code, collector_number, lang FROM cards WHERE collector_number IS NOT NULL"
        ):
            index.setdefault(((set_code or "").lower(), (number or "").lower(), lang or "en"), []).append(uuid)
        return index
    finally:
        conn.close()


def _buylist(prices, uuids, finish):
    field = "buylist_normal" if finish == "nonfoil" else "buylist_foil"
    return next((prices[u][field] for u in uuids if prices.get(u, {}).get(field) is not None), None)


def find_auction_deals(db_path):
    auctions = fetch_auctions()
    logger.info("Checking %d live auction(s)", len(auctions))
    with ThreadPoolExecutor(max_workers=10) as executor:
        details = list(executor.map(fetch_details, auctions))
    index = _printing_index(db_path)
    prices = cardkingdom.get_all_prices()
    now = datetime.now(timezone.utc)

    rows = []
    for auction, detail in zip(auctions, details):
        merged = {**auction, **{k: v for k, v in detail.items() if v is not None}}
        set_code = (tcgmarketplace._set_code(merged) or merged.get("setcode") or "").lower()
        number = tcgmarketplace._printing_number(merged)
        lang = (merged.get("crd_language") or "en").lower()
        finish = _finish(merged)
        buylist = _buylist(prices, index.get((set_code, number or "", lang), []), finish)
        gog_cash = round(buylist * GOG_CASH_MULTIPLIER, 2) if buylist is not None else None

        current_bid = _number(merged.get("current_bid"))
        min_price = _number(merged.get("min_price"))
        increment = _number(merged.get("bid_increment")) or 0
        next_bid = round(current_bid + increment, 2) if current_bid is not None else min_price
        buyout = _number(merged.get("buyout_price"))

        try:
            start = datetime.fromisoformat(merged["starting_date"].replace("Z", "+00:00"))
            ends_at = start + timedelta(days=float(merged.get("duration") or 0))
        except (KeyError, TypeError, ValueError):
            ends_at = None
        if ends_at is not None and ends_at <= now:
            continue  # already over

        product_id = merged.get("product_id")
        listings = tcgmarketplace.matching_listings(product_id, foil=finish != "nonfoil", lang=lang, condition="Near Mint")
        cheapest_listing = min((price for price, _ in listings), default=None)

        near_mint = (merged.get("crd_condition") or "").upper() == "NM"
        margin = round(gog_cash - next_bid, 2) if gog_cash is not None and next_bid is not None else None
        buyout_margin = round(gog_cash - buyout, 2) if gog_cash is not None and buyout is not None else None
        rows.append(
            {
                "id": merged.get("new_bidding_id"),
                "name": (merged.get("name") or "").strip(),
                "set_name": merged.get("setname") or merged.get("crd_setname"),
                "set_code": set_code.upper(),
                "collector_number": number,
                "finish": finish,
                "lang": lang,
                "condition": merged.get("crd_condition"),
                "graded": bool(merged.get("graded")),
                "quantity": merged.get("quantity") or 1,
                "image_url": merged.get("image"),
                "seller": merged.get("merchant_name"),
                "current_bid": current_bid,
                "min_price": min_price,
                "bid_increment": increment,
                "next_bid": next_bid,
                "buyout_price": buyout,
                "ends_at": ends_at.isoformat() if ends_at else None,
                "cardkingdom_buylist_price": buylist,
                "gog_cash": gog_cash,
                "cheapest_listing": cheapest_listing,
                "margin": margin,
                "buyout_margin": buyout_margin,
                # A deal: Near Mint (what Card Kingdom's buylist is for),
                # and either winning at the next bid or buying out pays less
                # than GOG Cash.
                "is_deal": near_mint
                and not merged.get("graded")
                and ((margin is not None and margin > 0) or (buyout_margin is not None and buyout_margin > 0)),
            }
        )

    rows.sort(key=lambda r: (not r["is_deal"], -(max(r["margin"] or -1e9, r["buyout_margin"] or -1e9))))
    logger.info("Found %d auction deal(s) among %d live auction(s)", sum(r["is_deal"] for r in rows), len(rows))
    return rows


def export_auctions(db_path):
    rows = find_auction_deals(db_path)
    AUCTIONS_FILE.parent.mkdir(exist_ok=True)
    AUCTIONS_FILE.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_auctions": len(rows),
                "total_deals": sum(r["is_deal"] for r in rows),
                "auctions": rows,
            },
            separators=(",", ":"),
        )
    )
    logger.info("Wrote %d auction(s) to %s", len(rows), AUCTIONS_FILE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    export_auctions(all_cards_history.db_path_for_year(date.today().year))
    # Our own shop's listings ride along on this 30-minute run, so the
    # dashboard's Sell plan shows new/changed listings within half an hour
    # rather than waiting for the nightly refresh.
    tcgmarketplace.export_my_listings(AUCTIONS_FILE.parent / "my_listings.json")
