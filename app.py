import logging
import os
import subprocess
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request

import all_cards_lookup
import cardkingdom
import db
import manabox_import
import mtgjson_crosswalk
import scryfall
from refresh_job import export_alerts, export_snapshot, notify_alerts, refresh_watchlist_prices

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tcg-price-checker")

app = Flask(__name__)
db.init_db()

PROJECT_DIR = Path(__file__).parent


def _git_sync(operation, message_fn, max_retries=3):
    """Runs `operation()` (a callable that mutates tcg_prices.db — e.g. a
    watchlist insert or a feedback update) and syncs the result to git.
    `message_fn(result)` builds the commit message from whatever
    `operation()` returns.

    tcg_prices.db is a SQLite binary, so git can't meaningfully merge two
    divergent writes to it the way it can with text — a conflict there
    isn't something `git pull --rebase` can resolve safely. Instead, on a
    rejected push, this resets the working tree to the remote's latest
    state and re-runs `operation()` on top of it. That's safe specifically
    because our writes are idempotent (ON CONFLICT DO NOTHING inserts,
    or plain field updates) — replaying after a fresh pull converges on
    the correct combined state instead of risking a botched binary merge.
    """
    result = operation()

    for attempt in range(1, max_retries + 1):
        try:
            subprocess.run(
                ["git", "add", "tcg_prices.db"], cwd=PROJECT_DIR, capture_output=True, timeout=10, check=True
            )
            commit = subprocess.run(
                ["git", "commit", "-m", message_fn(result)], cwd=PROJECT_DIR, capture_output=True, timeout=10
            )
            if commit.returncode != 0 and b"nothing to commit" not in commit.stdout:
                logger.warning("git commit failed: %s", commit.stdout.decode(errors="replace"))
                return result

            push = subprocess.run(["git", "push"], cwd=PROJECT_DIR, capture_output=True, timeout=30)
            if push.returncode == 0:
                logger.info("Synced to git: %s", message_fn(result))
                return result

            logger.info(
                "Push rejected (attempt %d/%d) — resetting to remote and retrying: %s",
                attempt,
                max_retries,
                push.stderr.decode(errors="replace").strip(),
            )
            subprocess.run(
                ["git", "fetch", "origin", "main"], cwd=PROJECT_DIR, capture_output=True, timeout=30, check=True
            )
            subprocess.run(
                ["git", "reset", "--hard", "origin/main"],
                cwd=PROJECT_DIR,
                capture_output=True,
                timeout=10,
                check=True,
            )
            result = operation()
        except Exception as exc:
            logger.warning("Could not sync to git (%s)", exc)
            return result

    logger.warning("Could not sync to git after %d attempt(s) — you may need to push manually", max_retries)
    return result


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


@app.route("/api/lookup/<mtgjson_id>")
def api_lookup(mtgjson_id):
    card = all_cards_lookup.get_by_uuid(mtgjson_id)
    if not card:
        return jsonify({"error": "No price history found for this card yet."}), 404
    return jsonify(card)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/autocomplete")
def api_autocomplete():
    q = request.args.get("q")
    if not q:
        return jsonify([])
    return jsonify(scryfall.autocomplete(q))


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


@app.route("/api/owners")
def api_owners():
    return jsonify(db.list_owners())


@app.route("/api/conditions")
def api_conditions():
    return jsonify(db.CONDITIONS)


@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_list():
    return jsonify(db.list_watchlist(owner=request.args.get("owner") or None))


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    payload = request.get_json(force=True)
    required = ["variantId", "name"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    owner = (payload.get("owner") or "").strip() or None
    try:
        quantity = max(1, int(payload.get("quantity") or 1))
    except (TypeError, ValueError):
        quantity = 1
    condition = payload.get("condition") or "Near Mint"
    if condition not in db.CONDITIONS:
        condition = "Near Mint"
    try:
        purchase_price = float(payload["purchasePrice"]) if payload.get("purchasePrice") not in (None, "") else None
    except (TypeError, ValueError):
        purchase_price = None
    card = {
        "variant_id": f"{payload['variantId']}:{db.condition_slug(condition)}",
        "card_id": payload.get("cardId"),
        "game": "Magic: The Gathering",
        "name": payload.get("name"),
        "set_name": payload.get("setName"),
        "condition": condition,
        "printing": payload.get("printing"),
        "tcgplayer_id": payload.get("tcgplayerId"),
        "image_url": payload.get("imageUrl"),
        "price": payload.get("cardKingdomPrice"),
        "mtgjson_id": payload.get("mtgjsonId"),
        "cardkingdom_price": payload.get("cardKingdomPrice"),
        "cardkingdom_buylist_price": payload.get("cardKingdomBuylist"),
        "purchase_price": purchase_price,
        "owner": owner,
        "quantity": quantity,
    }
    item = _git_sync(
        lambda: db.add_to_watchlist(card),
        lambda item: f"Track {item['name']} ({item['set_name']}){f' for {owner}' if owner else ''}",
    )
    return jsonify(item), 201


@app.route("/api/watchlist/import", methods=["POST"])
def api_watchlist_import():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    owner = (request.form.get("owner") or "").strip() or None
    try:
        rows = manabox_import.parse_csv(file.read())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except UnicodeDecodeError:
        return jsonify({"error": "Could not read file as text — is this a CSV file?"}), 400

    try:
        result = _git_sync(
            lambda: manabox_import.import_rows(rows, owner=owner),
            lambda result: f"Import {result['imported']} card(s) from ManaBox CSV{f' for {owner}' if owner else ''}",
        )
    except requests.RequestException as exc:
        logger.warning("ManaBox import failed due to a network error: %s", exc)
        return jsonify({"error": "Scryfall was unreachable during import — try again in a moment."}), 502
    return jsonify(result), 201


@app.route("/api/watchlist/<int:watchlist_id>", methods=["DELETE"])
def api_watchlist_remove(watchlist_id):
    item = db.get_watchlist_item(watchlist_id)
    if item:
        _git_sync(
            lambda: db.remove_from_watchlist(watchlist_id),
            lambda _: f"Untrack {item['name']} ({item['set_name']})",
        )
    else:
        db.remove_from_watchlist(watchlist_id)
    return "", 204


@app.route("/api/watchlist/<int:watchlist_id>/history")
def api_watchlist_history(watchlist_id):
    item = db.get_watchlist_item(watchlist_id)
    if not item:
        return jsonify({"error": "Not found"}), 404
    kind = "buylist" if request.args.get("kind") == "buylist" else "market"
    return jsonify(db.get_history(item["variant_id"], kind=kind))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    alerts = refresh_watchlist_prices()
    export_snapshot()
    export_alerts(alerts)
    notify_alerts(alerts)
    return jsonify(db.list_watchlist())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True, use_reloader=False)
