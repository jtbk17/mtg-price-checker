"""Basic smoke tests: catch import errors and obvious crashes in the core
modules before they reach production. Not exhaustive — no network calls
(Scryfall/Telegram/GitHub) are made, so this stays fast and doesn't depend
on external services being up. Run with: py -m unittest test_smoke -v

IMPORTANT: TCG_DB_PATH must be set before any of our modules are imported,
since db.py reads it once at import time — this is why it's set at the top
of this file, before the `import db` below.
"""

import os
import tempfile
import unittest
from pathlib import Path

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["TCG_DB_PATH"] = _tmp_db.name

import all_cards_history as ach
import db
import manabox_import
import recommender


class DbTests(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def test_watchlist_round_trip(self):
        card = {
            "variant_id": "test-scryfall-id:nonfoil",
            "card_id": "test-scryfall-id",
            "game": "Magic: The Gathering",
            "name": "Test Card",
            "set_name": "Test Set",
            "condition": "Near Mint",
            "printing": "Normal",
            "tcgplayer_id": None,
            "image_url": None,
            "price": 1.23,
            "mtgjson_id": "test-uuid",
            "cardkingdom_price": 1.23,
            "cardkingdom_buylist_price": 0.50,
            "owner": "TestOwner",
        }
        item = db.add_to_watchlist(card)
        self.assertEqual(item["name"], "Test Card")
        self.assertEqual(item["owner"], "TestOwner")

        items = db.list_watchlist(owner="TestOwner")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["latest_price"], 1.23)

        self.assertIn("TestOwner", db.list_owners())

        db.remove_from_watchlist(item["id"])
        self.assertEqual(db.list_watchlist(owner="TestOwner"), [])

    def test_quantity_defaults_and_updates_on_retrack(self):
        card = {
            "variant_id": "qty-test:nonfoil",
            "card_id": "qty-test",
            "game": "Magic: The Gathering",
            "name": "Qty Card",
            "set_name": "Test Set",
            "condition": "Near Mint",
            "printing": "Normal",
            "price": 1.0,
            "cardkingdom_price": 1.0,
            "cardkingdom_buylist_price": 0.4,
            "owner": None,
        }
        item = db.add_to_watchlist(card)
        self.assertEqual(item["quantity"], 1)

        item = db.add_to_watchlist({**card, "quantity": 4})
        self.assertEqual(item["quantity"], 4)
        self.assertEqual(len(db.list_watchlist()), 1)  # still one row, not a duplicate

        db.remove_from_watchlist(item["id"])

    def test_market_and_buylist_history_are_independent(self):
        # record_price()'s recorded_at defaults to CURRENT_TIMESTAMP, which
        # only has second-level granularity, so inserting explicit
        # timestamps here (rather than calling record_price() twice in a
        # row for the same kind) avoids a same-second collision making
        # this test timing-dependent.
        variant_id = "history-test:nonfoil"
        conn = db.get_connection()
        conn.executemany(
            "INSERT INTO price_history (variant_id, price, kind, recorded_at) VALUES (?, ?, ?, ?)",
            [
                (variant_id, 5.00, "market", "2026-01-01 00:00:00"),
                (variant_id, 2.00, "buylist", "2026-01-01 00:00:00"),
                (variant_id, 6.00, "market", "2026-01-02 00:00:00"),
            ],
        )
        conn.commit()
        conn.close()

        market = db.get_history(variant_id, kind="market")
        buylist = db.get_history(variant_id, kind="buylist")
        self.assertEqual([h["price"] for h in market], [5.00, 6.00])
        self.assertEqual([h["price"] for h in buylist], [2.00])

    def test_second_owner_can_track_same_card(self):
        base_card = {
            "variant_id": "shared-variant:nonfoil",
            "card_id": "shared",
            "game": "Magic: The Gathering",
            "name": "Shared Card",
            "set_name": "Test Set",
            "condition": "Near Mint",
            "printing": "Normal",
            "tcgplayer_id": None,
            "image_url": None,
            "price": 5.00,
            "mtgjson_id": "shared-uuid",
            "cardkingdom_price": 5.00,
            "cardkingdom_buylist_price": 2.00,
        }
        item_a = db.add_to_watchlist({**base_card, "owner": "Alice"})
        item_b = db.add_to_watchlist({**base_card, "owner": "Bob"})
        self.assertNotEqual(item_a["id"], item_b["id"])
        db.remove_from_watchlist(item_a["id"])
        db.remove_from_watchlist(item_b["id"])

    def test_recommendation_feedback_round_trip(self):
        rec_id = db.record_recommendation("Rec Card", "Rec Set", 1.00, 2.00, 100.0)
        self.assertIsNone(db.get_recommendation(rec_id)["feedback"])

        db.set_recommendation_feedback(rec_id, "good")
        self.assertEqual(db.get_recommendation(rec_id)["feedback"], "good")

        labeled = db.get_labeled_recommendations()
        self.assertTrue(any(r["feedback"] == "good" for r in labeled))

    def test_app_state_round_trip(self):
        self.assertFalse(db.already_ran_today("smoke_test_key"))
        db.mark_ran_today("smoke_test_key")
        self.assertTrue(db.already_ran_today("smoke_test_key"))


class RecommenderTests(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def test_no_model_with_insufficient_data(self):
        model = recommender.train()
        self.assertIsNone(model)
        self.assertIsNone(recommender.score(model, 1.0, 10.0))

    def test_trains_once_enough_labeled_examples_exist(self):
        for i in range(recommender.MIN_LABELED_EXAMPLES):
            rec_id = db.record_recommendation(f"Card {i}", "Set", 1.0 + i, 2.0 + i, 50.0)
            db.set_recommendation_feedback(rec_id, "good" if i % 2 == 0 else "bad")

        model = recommender.train()
        self.assertIsNotNone(model)
        score = recommender.score(model, 1.5, 60.0)
        self.assertIsNotNone(score)
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)


class AllCardsHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "all_cards_test.db"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_compute_movers_on_empty_db(self):
        conn = ach._get_connection(self.db_path)
        conn.close()
        movers = ach.compute_movers(self.db_path)
        for key in ("daily_gainers", "daily_losers", "weekly_gainers", "weekly_losers", "trend_gainers", "trend_losers"):
            self.assertEqual(movers[key], [])

    def test_compute_movers_excludes_tokens_and_zero_change(self):
        conn = ach._get_connection(self.db_path)
        today = ach._day_number(__import__("datetime").date.today())

        def add_card(uuid, name, is_token):
            conn.execute(
                "INSERT INTO cards (mtgjson_uuid, name, set_code, is_token) VALUES (?, ?, 'TST', ?)",
                (uuid, name, is_token),
            )
            return conn.execute("SELECT id FROM cards WHERE mtgjson_uuid = ?", (uuid,)).fetchone()[0]

        real_card_id = add_card("real-1", "Real Card", 0)
        token_id = add_card("token-1", "Generic Token", 1)
        unchanged_id = add_card("unchanged-1", "Unchanged Card", 0)

        rows = [
            (real_card_id, today - 1, 100),
            (real_card_id, today, 200),
            (token_id, today - 1, 100),
            (token_id, today, 200),
            (unchanged_id, today - 1, 100),
            (unchanged_id, today, 100),
        ]
        conn.executemany(
            "INSERT INTO price_history (card_id, day, price_cents) VALUES (?, ?, ?)", rows
        )
        conn.commit()
        conn.close()

        movers = ach.compute_movers(self.db_path)
        names = {m["name"] for m in movers["daily_gainers"]}
        self.assertIn("Real Card", names)
        self.assertNotIn("Generic Token", names)
        self.assertNotIn("Unchanged Card", names)


class ManaboxImportTests(unittest.TestCase):
    def test_parse_csv_rejects_non_manabox_file(self):
        with self.assertRaises(ValueError):
            manabox_import.parse_csv(b"Name,Foo\nLightning Bolt,bar\n")

    def test_parse_csv_accepts_valid_header(self):
        csv_bytes = b"Name,Set code,Foil,Scryfall ID\nLightning Bolt,m10,normal,7673784e-db4b-43a1-8d55-1bb9fc1e284f\n"
        rows = manabox_import.parse_csv(csv_bytes)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Name"], "Lightning Bolt")

    def test_finish_and_label_mapping(self):
        self.assertEqual(manabox_import._finish_and_label("normal"), ("nonfoil", "Normal"))
        self.assertEqual(manabox_import._finish_and_label("foil"), ("foil", "Foil"))
        self.assertEqual(manabox_import._finish_and_label("etched"), ("etched", "Etched Foil"))


class ImportsTests(unittest.TestCase):
    """Every module should at least import cleanly — catches syntax errors
    and top-level exceptions before they reach the nightly job."""

    def test_all_modules_import(self):
        import all_cards_lookup  # noqa: F401
        import cardkingdom  # noqa: F401
        import market_alerts  # noqa: F401
        import mtgjson_crosswalk  # noqa: F401
        import record_feedback  # noqa: F401
        import refresh_job  # noqa: F401
        import scryfall  # noqa: F401
        import telegram_notify  # noqa: F401


class AppRouteTests(unittest.TestCase):
    """Exercise the Flask routes that don't need network access
    (Scryfall/Card Kingdom search is out of scope here — no network calls
    in this test suite)."""

    @classmethod
    def setUpClass(cls):
        import app as flask_app_module

        cls.app = flask_app_module.app
        cls.app.testing = True

    def setUp(self):
        db.init_db()
        self.client = self.app.test_client()

    def test_watchlist_and_owners_endpoints(self):
        resp = self.client.get("/api/watchlist")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), list)

        resp = self.client.get("/api/owners")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), list)

    def test_index_page_renders(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_watchlist_add_requires_fields(self):
        resp = self.client.post("/api/watchlist", json={})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
