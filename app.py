import logging
import os
import subprocess
from pathlib import Path

from flask import Flask, jsonify, render_template, request

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


def _git_sync(message):
    """Best-effort: commit and push tcg_prices.db so the nightly job and
    dashboard pick up watchlist changes without a manual git step. Failures
    (offline, no remote, merge conflict) are logged, not raised — the
    watchlist change itself already succeeded locally regardless."""
    try:
        subprocess.run(
            ["git", "add", "tcg_prices.db"], cwd=PROJECT_DIR, capture_output=True, timeout=10, check=True
        )
        commit = subprocess.run(
            ["git", "commit", "-m", message], cwd=PROJECT_DIR, capture_output=True, timeout=10
        )
        if commit.returncode != 0:
            if b"nothing to commit" in commit.stdout:
                return
            logger.warning("git commit failed: %s", commit.stdout.decode(errors="replace"))
            return
    except Exception as exc:
        logger.warning("Could not commit watchlist change to git (%s)", exc)
        return

    # `git commit` (no -a) only ever touched the staged file above, so any
    # *other* dirty files in the working tree are harmless here — but they
    # can still make `pull --rebase` refuse to run at all. Don't let that
    # block the push: a plain push succeeds in the common case where the
    # remote hasn't moved, and if it has, we just log it for manual fixup.
    subprocess.run(["git", "pull", "--rebase"], cwd=PROJECT_DIR, capture_output=True, timeout=30)
    push = subprocess.run(["git", "push"], cwd=PROJECT_DIR, capture_output=True, timeout=30)
    if push.returncode != 0:
        logger.warning(
            "Committed locally but could not push (%s) — run `git push` manually",
            push.stderr.decode(errors="replace").strip(),
        )
    else:
        logger.info("Synced to git: %s", message)


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


@app.route("/api/owners")
def api_owners():
    return jsonify(db.list_owners())


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
        "owner": owner,
    }
    item = db.add_to_watchlist(card)
    _git_sync(f"Track {item['name']} ({item['set_name']}){f' for {owner}' if owner else ''}")
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

    result = manabox_import.import_rows(rows, owner=owner)
    if result["imported"]:
        _git_sync(f"Import {result['imported']} card(s) from ManaBox CSV{f' for {owner}' if owner else ''}")
    return jsonify(result), 201


@app.route("/api/watchlist/<int:watchlist_id>", methods=["DELETE"])
def api_watchlist_remove(watchlist_id):
    item = db.get_watchlist_item(watchlist_id)
    db.remove_from_watchlist(watchlist_id)
    if item:
        _git_sync(f"Untrack {item['name']} ({item['set_name']})")
    return "", 204


@app.route("/api/watchlist/<int:watchlist_id>/history")
def api_watchlist_history(watchlist_id):
    item = db.get_watchlist_item(watchlist_id)
    if not item:
        return jsonify({"error": "Not found"}), 404
    return jsonify(db.get_history(item["variant_id"]))


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
