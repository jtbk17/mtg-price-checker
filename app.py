import logging
import os

from flask import Flask, jsonify, render_template, request

import cardkingdom
import db
import mtgjson_crosswalk
import scryfall
from refresh_job import export_snapshot, refresh_watchlist_prices

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tcg-price-checker")

app = Flask(__name__)
db.init_db()


def _is_foil(finish):
    return finish != "Normal"


def _serialize_card(card):
    scryfall_id = card["id"]
    set_code = card.get("set")
    uuid = mtgjson_crosswalk.get_uuid(scryfall_id, set_code)

    finishes = [f for f in card.get("finishes", []) if f in ("nonfoil", "foil", "etched")] or ["nonfoil"]
    labels = {"nonfoil": "Normal", "foil": "Foil", "etched": "Etched Foil"}

    variants = []
    for finish in finishes:
        label = labels[finish]
        ck_prices = cardkingdom.get_prices(uuid, foil=_is_foil(label)) if uuid else None
        variants.append(
            {
                "variantId": f"{scryfall_id}:{finish}",
                "printing": label,
                "cardKingdomPrice": ck_prices["market"] if ck_prices else None,
                "cardKingdomBuylist": ck_prices["buylist"] if ck_prices else None,
            }
        )

    return {
        "scryfallId": scryfall_id,
        "mtgjsonId": uuid,
        "name": card.get("name"),
        "set": set_code,
        "setName": card.get("set_name"),
        "tcgplayerId": card.get("tcgplayer_id"),
        "imageUrl": scryfall.extract_image(card),
        "variants": variants,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    q = request.args.get("q")
    if not q:
        return jsonify({"error": "Query parameter 'q' is required."}), 400
    try:
        cards = scryfall.search_cards(q)
        return jsonify([_serialize_card(c) for c in cards])
    except scryfall.ScryfallError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_list():
    return jsonify(db.list_watchlist())


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    payload = request.get_json(force=True)
    required = ["variantId", "name"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    card = {
        "variant_id": payload["variantId"],
        "card_id": payload.get("cardId"),
        "game": "Magic: The Gathering",
        "name": payload.get("name"),
        "set_name": payload.get("setName"),
        "condition": "Near Mint",
        "printing": payload.get("printing"),
        "tcgplayer_id": payload.get("tcgplayerId"),
        "image_url": payload.get("imageUrl"),
        "price": payload.get("cardKingdomPrice"),
        "mtgjson_id": payload.get("mtgjsonId"),
        "cardkingdom_price": payload.get("cardKingdomPrice"),
        "cardkingdom_buylist_price": payload.get("cardKingdomBuylist"),
    }
    item = db.add_to_watchlist(card)
    return jsonify(item), 201


@app.route("/api/watchlist/<int:watchlist_id>", methods=["DELETE"])
def api_watchlist_remove(watchlist_id):
    db.remove_from_watchlist(watchlist_id)
    return "", 204


@app.route("/api/watchlist/<int:watchlist_id>/history")
def api_watchlist_history(watchlist_id):
    item = db.get_watchlist_item(watchlist_id)
    if not item:
        return jsonify({"error": "Not found"}), 404
    return jsonify(db.get_history(item["variant_id"]))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    refresh_watchlist_prices()
    export_snapshot()
    return jsonify(db.list_watchlist())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True, use_reloader=False)
