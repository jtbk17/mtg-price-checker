"""Market-wide Good Deals: every card Card Kingdom buys (not just the
watchlist) where TheTCGMarketplace has copies listed for less than GOG
Cash would pay for them — i.e. you could buy there and cash out at GOG
for a profit. Exported nightly to docs/deals.json for the dashboard.

Only cards whose GOG Cash payout is at least MIN_GOG_CASH are checked:
that's ~17,000 card/finish combinations, against ~81,000 with any Card
Kingdom buylist price at all. Below it the margins are cents, and
checking everything (each card costs a TheTCGMarketplace search plus a
listings fetch — there's no usable bulk endpoint, every bulk query is
capped at 300 products) would take hours a night.

Each card is compared against listings in its own language (mostly
English, but e.g. Japanese Mystical Archive cards against Japanese
copies) and in Near Mint — Card Kingdom's buylist price is for NM. A
deal also reports supply: how many such copies are listed below GOG
Cash, and the total profit from buying all of them.

Card labels (name, set, collector number, finishes, language) come from
the all-cards database's `cards` table, filled in by all_cards_history.
backfill_names() — run after all_cards_history.py.
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import all_cards_history
import cardkingdom
import tcgmarketplace

logger = logging.getLogger("tcg-price-checker")

# Keep in sync with GOG_CASH_MULTIPLIER in static/app.js and docs/index.html.
GOG_CASH_MULTIPLIER = 1.15
MIN_GOG_CASH = 5.0
MAX_DEALS = 500
DEALS_FILE = Path(__file__).parent / "docs" / "deals.json"


def _candidates(prices):
    """[(uuid, foil, buylist)] for every card/finish whose GOG Cash payout
    clears MIN_GOG_CASH."""
    out = []
    for uuid, entry in prices.items():
        for foil, field, retail_field in ((False, "buylist_normal", "retail_normal"), (True, "buylist_foil", "retail_foil")):
            buylist = entry.get(field)
            if buylist is None or buylist * GOG_CASH_MULTIPLIER < MIN_GOG_CASH:
                continue
            # Card Kingdom never buys above its own sell price, so a feed
            # entry that does is bad data — e.g. a $6.99 foil Nissa, Who
            # Shakes the World (Bloomburrow Commander) with a $72 foil
            # buylist. No retail price (often just out of stock) is fine.
            retail = entry.get(retail_field)
            if retail is not None and buylist > retail:
                continue
            out.append((uuid, foil, buylist))
    return out


def _labels(conn, uuids):
    """{uuid: card row dict} from the all-cards db, for the given uuids."""
    labels = {}
    uuids = list(uuids)
    for start in range(0, len(uuids), 900):  # stay under SQLite's bound-parameter limit
        chunk = uuids[start : start + 900]
        rows = conn.execute(
            "SELECT mtgjson_uuid, name, set_code, set_name, scryfall_id, collector_number, finishes, lang, is_token "
            f"FROM cards WHERE mtgjson_uuid IN ({','.join('?' * len(chunk))})",
            chunk,
        )
        for uuid, name, set_code, set_name, scryfall_id, number, finishes, lang, is_token in rows:
            labels[uuid] = {
                "name": name,
                "set_code": set_code,
                "set_name": set_name,
                "scryfall_id": scryfall_id,
                "collector_number": number,
                "finishes": json.loads(finishes) if finishes else [],
                "lang": lang or "en",
                "is_token": is_token,
            }
    return labels


def _finish(foil, finishes):
    if not foil:
        return "nonfoil"
    # Card Kingdom's feed has no separate etched price: an etched-only
    # card's "foil" price is its etched one.
    return "etched" if "etched" in finishes and "foil" not in finishes else "foil"


def _image_url(scryfall_id):
    if not scryfall_id:
        return None
    return f"https://cards.scryfall.io/normal/front/{scryfall_id[0]}/{scryfall_id[1]}/{scryfall_id}.jpg"


def find_deals(db_path, on_progress=None):
    prices = cardkingdom.get_all_prices()
    candidates = _candidates(prices)
    logger.info("Checking %d card/finish combination(s) for market deals", len(candidates))

    # Some Card Kingdom-priced uuids never get a retail snapshot, so they
    # may not be in the cards table yet — add them so backfill_names()
    # labels them too.
    conn = all_cards_history._get_connection(db_path)
    uuid_cache = {row[1]: row[0] for row in conn.execute("SELECT id, mtgjson_uuid FROM cards")}
    for uuid, _, _ in candidates:
        all_cards_history._card_id(conn, uuid_cache, uuid)
    conn.commit()
    conn.close()
    all_cards_history.backfill_names(db_path)

    conn = all_cards_history._get_connection(db_path)
    labels = _labels(conn, {uuid for uuid, _, _ in candidates})
    conn.close()

    # Split/double-faced cards can have one uuid per face for the same
    # printing — check each printing+finish once.
    checks = {}
    for uuid, foil, buylist in candidates:
        label = labels.get(uuid)
        if not label or not label["name"] or not label["collector_number"] or label["is_token"]:
            continue
        finish = _finish(foil, label["finishes"])
        key = (label["set_code"], label["collector_number"], finish, label["lang"])
        if key in checks:
            continue
        lookup = (
            # TheTCGMarketplace's search matches on the front face's name.
            label["name"].split(" // ")[0],
            label["set_name"],
            (label["set_code"] or "").lower(),
            label["collector_number"],
            finish,
            label["lang"],
            "Near Mint",
        )
        checks[key] = (lookup, label, buylist)

    lookups = [lookup for lookup, _, _ in checks.values()]
    tcgmarketplace.prefetch_ids(lookups, on_progress=on_progress)
    tcgmarketplace.prefetch_prices(lookups, on_progress=on_progress)

    deals = []
    for lookup, label, buylist in checks.values():
        gog_cash = round(buylist * GOG_CASH_MULTIPLIER, 2)
        product_id = tcgmarketplace.find_id(*lookup[:5])
        _, _, _, _, finish, lang, condition = lookup
        listings = tcgmarketplace.matching_listings(product_id, foil=finish != "nonfoil", lang=lang, condition=condition)
        under = [(price, qty) for price, qty in listings if price < gog_cash]
        if not under:
            continue
        cheapest = min(price for price, _ in under)
        deals.append(
            {
                "name": label["name"],
                "set_name": label["set_name"],
                "set_code": label["set_code"],
                "collector_number": label["collector_number"],
                "finish": finish,
                "lang": lang,
                "image_url": _image_url(label["scryfall_id"]),
                "cardkingdom_buylist_price": buylist,
                "gog_cash": gog_cash,
                "cheapest_price": cheapest,
                "margin": round(gog_cash - cheapest, 2),
                # Supply: every qualifying copy listed below GOG Cash.
                "copies_under_gog_cash": sum(qty for _, qty in under),
                "total_profit": round(sum((gog_cash - price) * qty for price, qty in under), 2),
            }
        )

    deals.sort(key=lambda d: d["margin"], reverse=True)
    logger.info("Found %d market deal(s)", len(deals))
    return {"checked": len(checks), "deals": deals}


def export_deals(db_path):
    result = find_deals(db_path)
    DEALS_FILE.parent.mkdir(exist_ok=True)
    DEALS_FILE.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "min_gog_cash": MIN_GOG_CASH,
                "checked": result["checked"],
                "total_deals": len(result["deals"]),
                "deals": result["deals"][:MAX_DEALS],
            },
            separators=(",", ":"),
        )
    )
    logger.info("Wrote %d market deal(s) to %s", min(len(result["deals"]), MAX_DEALS), DEALS_FILE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    export_deals(all_cards_history.db_path_for_year(date.today().year))
