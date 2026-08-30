import logging
import os
import subprocess
import threading
import uuid
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
        # Warm the crosswalk cache for every set this result touches in
        # parallel before serializing — a broad, heavily-reprinted search
        # can span dozens of sets never seen before, and fetching those
        # sequentially inside _serialize_card (the old behavior) measured
        # at 84s for one such search.
        mtgjson_crosswalk.prefetch_sets(c.get("set") for c in cards)
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
    return jsonify(
        db.list_watchlist(owner=request.args.get("owner") or None, sort=request.args.get("sort"))
    )


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


_import_jobs = {}
_import_jobs_lock = threading.Lock()


def _run_import_job(job_id, rows, owner):
    def on_progress(phase, done, total):
        with _import_jobs_lock:
            job = _import_jobs.get(job_id)
            if job is not None:
                job.update(phase=phase, done=done, total=total)

    try:
        result = _git_sync(
            lambda: manabox_import.import_rows(rows, owner=owner, on_progress=on_progress),
            lambda result: f"Import {result['imported']} card(s) from ManaBox CSV{f' for {owner}' if owner else ''}",
        )
        with _import_jobs_lock:
            _import_jobs[job_id].update(finished=True, result=result, error=None)
    except requests.RequestException as exc:
        logger.warning("ManaBox import failed due to a network error: %s", exc)
        with _import_jobs_lock:
            _import_jobs[job_id].update(
                finished=True,
                result=None,
                error="Scryfall was unreachable during import — try again in a moment.",
            )
    except Exception as exc:  # a failure here must still reach the poller, not vanish in a background thread
        logger.exception("ManaBox import job failed")
        with _import_jobs_lock:
            _import_jobs[job_id].update(finished=True, result=None, error=str(exc))


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

    job_id = str(uuid.uuid4())
    with _import_jobs_lock:
        # Large imports are rare on this app, but prune finished jobs on
        # every new start anyway so a long-running server doesn't slowly
        # accumulate stale entries.
        for stale_id in [j for j, v in _import_jobs.items() if v.get("finished")]:
            del _import_jobs[stale_id]
        _import_jobs[job_id] = {
            "phase": "Starting…",
            "done": 0,
            "total": len(rows),
            "finished": False,
            "result": None,
            "error": None,
        }
    threading.Thread(target=_run_import_job, args=(job_id, rows, owner), daemon=True).start()
    return jsonify({"jobId": job_id}), 202


@app.route("/api/watchlist/import/<job_id>/status")
def api_watchlist_import_status(job_id):
    with _import_jobs_lock:
        job = _import_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown import job"}), 404
    return jsonify(job)


@app.route("/api/watchlist/<int:watchlist_id>/add-copies", methods=["POST"])
def api_watchlist_add_copies(watchlist_id):
    item = db.get_watchlist_item(watchlist_id)
    if not item:
        return jsonify({"error": "Not found"}), 404

    payload = request.get_json(force=True)
    try:
        quantity = int(payload.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        return jsonify({"error": "Quantity must be a positive number."}), 400
    try:
        purchase_price = float(payload["purchasePrice"]) if payload.get("purchasePrice") not in (None, "") else None
    except (TypeError, ValueError):
        purchase_price = None

    owner_tag = f" for {item['owner']}" if item.get("owner") else ""
    updated = _git_sync(
        lambda: db.add_copies(watchlist_id, quantity, purchase_price),
        lambda _: f"Add {quantity} more {item['name']} ({item['set_name']}){owner_tag}",
    )
    return jsonify(updated), 200


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
    history = db.get_history(item["variant_id"], kind=kind)

    # Merged in at read time rather than backfilled into tcg_prices.db:
    # that file is committed straight to git on every track/untrack/import,
    # and duplicating the all-cards database's ~92-day history into it for
    # every tracked card was measured at 15MB -> 144MB for a 9,478-card
    # collection — comfortably over GitHub's 100MB per-file push limit,
    # which would have broken syncing entirely. No such backfill exists
    # for buylist (the all-cards database only tracks retail/market).
    if kind == "market" and item.get("mtgjson_id"):
        backfill = all_cards_lookup.get_by_uuid(item["mtgjson_id"])
        if backfill:
            seen_dates = {h["recorded_at"][:10] for h in history}
            older_points = [
                {"price": p["price"], "recorded_at": p["date"]}
                for p in backfill["history"]
                if p["date"] not in seen_dates
            ]
            history = sorted(older_points + history, key=lambda h: h["recorded_at"])

    return jsonify(history)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    alerts = refresh_watchlist_prices()
    export_snapshot()
    export_alerts(alerts)
    notify_alerts(alerts)
    return jsonify(db.list_watchlist())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # threaded=True is required, not just nice-to-have: ManaBox import now
    # runs in a background thread while the browser polls a status
    # endpoint, and Werkzeug's dev server is single-threaded by default —
    # without this, the status poll would just queue behind the import
    # instead of getting a live answer.
    app.run(host="127.0.0.1", port=port, debug=True, use_reloader=False, threaded=True)
