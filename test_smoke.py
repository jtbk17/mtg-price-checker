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
from unittest.mock import patch

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

    def test_retrack_does_not_wipe_purchase_price(self):
        card = {
            "variant_id": "cost-test:nonfoil",
            "card_id": "cost-test",
            "game": "Magic: The Gathering",
            "name": "Cost Card",
            "set_name": "Test Set",
            "condition": "Near Mint",
            "printing": "Normal",
            "price": 1.0,
            "cardkingdom_price": 1.0,
            "cardkingdom_buylist_price": 0.4,
            "owner": None,
            "purchase_price": 2.50,
        }
        item = db.add_to_watchlist(card)
        self.assertEqual(item["purchase_price"], 2.50)

        # Re-tracking without a purchase price (e.g. just bumping quantity
        # from search) must not silently erase the existing cost basis.
        item = db.add_to_watchlist({**card, "quantity": 3, "purchase_price": None})
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["purchase_price"], 2.50)

        # Providing a new purchase price does update it.
        item = db.add_to_watchlist({**card, "purchase_price": 4.00})
        self.assertEqual(item["purchase_price"], 4.00)

        db.remove_from_watchlist(item["id"])

    def test_add_copies_blends_weighted_average_cost(self):
        card = {
            "variant_id": "add-copies-test:nonfoil",
            "card_id": "add-copies-test",
            "game": "Magic: The Gathering",
            "name": "Add Copies Card",
            "set_name": "Test Set",
            "condition": "Near Mint",
            "printing": "Normal",
            "owner": None,
            "quantity": 2,
            "purchase_price": 3.00,
        }
        item = db.add_to_watchlist(card)

        # 2 @ $3 + 1 @ $6 -> 3 copies averaging $4.
        updated = db.add_copies(item["id"], added_quantity=1, added_purchase_price=6.00)
        self.assertEqual(updated["quantity"], 3)
        self.assertAlmostEqual(updated["purchase_price"], 4.00)

        # Adding more with no price given contributes nothing to the
        # average (not treated as $0) — 3 @ $4 + 2 @ unknown still
        # averages to $4 over the priced copies, quantity still grows.
        updated = db.add_copies(item["id"], added_quantity=2, added_purchase_price=None)
        self.assertEqual(updated["quantity"], 5)
        self.assertAlmostEqual(updated["purchase_price"], 4.00)

        self.assertIsNone(db.add_copies(999999, 1, 1.0))  # unknown id

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
        for key in ("daily_gainers", "daily_losers", "weekly_gainers", "weekly_losers"):
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
    def setUp(self):
        db.init_db()

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

    def test_normalize_condition_maps_all_manabox_tiers(self):
        self.assertEqual(manabox_import._normalize_condition("mint"), "Near Mint")
        self.assertEqual(manabox_import._normalize_condition("near_mint"), "Near Mint")
        self.assertEqual(manabox_import._normalize_condition("excellent"), "Lightly Played")
        self.assertEqual(manabox_import._normalize_condition("good"), "Lightly Played")
        self.assertEqual(manabox_import._normalize_condition("light_played"), "Lightly Played")
        self.assertEqual(manabox_import._normalize_condition("played"), "Moderately Played")
        self.assertEqual(manabox_import._normalize_condition("poor"), "Damaged")
        # Unrecognized value: title-cased rather than silently defaulted.
        self.assertEqual(manabox_import._normalize_condition("weird_value"), "Weird Value")
        self.assertEqual(manabox_import._normalize_condition(""), "Near Mint")

    def test_parse_quantity_does_not_clamp_zero(self):
        self.assertEqual(manabox_import._parse_quantity("0"), 0)
        self.assertEqual(manabox_import._parse_quantity("-2"), -2)
        self.assertEqual(manabox_import._parse_quantity("3"), 3)
        self.assertEqual(manabox_import._parse_quantity("not a number"), 1)

    def test_import_rows_end_to_end(self):
        fake_card = {
            "id": "abc-123",
            "name": "Fake Card",
            "set": "tst",
            "set_name": "Test Set",
            "tcgplayer_id": 999,
            "image_uris": {"normal": "https://example.com/fake.jpg"},
        }
        rows = [
            # Same card+finish, different condition — must NOT merge.
            {
                "Scryfall ID": "abc-123",
                "Name": "Fake Card",
                "Set code": "tst",
                "Foil": "foil",
                "Condition": "near_mint",
                "Quantity": "2",
                "Purchase price": "2.00",
            },
            # A second near_mint row for the *same* variant — must merge
            # with the row above, weight-averaging the cost: (2*2 + 1*5)/3 = 3.
            {
                "Scryfall ID": "abc-123",
                "Name": "Fake Card",
                "Set code": "tst",
                "Foil": "foil",
                "Condition": "near_mint",
                "Quantity": "1",
                "Purchase price": "5.00",
            },
            {
                "Scryfall ID": "abc-123",
                "Name": "Fake Card",
                "Set code": "tst",
                "Foil": "foil",
                "Condition": "light_played",
                "Quantity": "1",
                "Purchase price": "1.50",
            },
            # Zero quantity — must be skipped entirely, not imported as 1.
            {
                "Scryfall ID": "abc-123",
                "Name": "Fake Card",
                "Set code": "tst",
                "Foil": "foil",
                "Condition": "near_mint",
                "Quantity": "0",
            },
            # Not resolvable on Scryfall — must be skipped with an error.
            {
                "Scryfall ID": "does-not-exist",
                "Name": "Ghost Card",
                "Set code": "tst",
                "Foil": "normal",
                "Condition": "near_mint",
                "Quantity": "1",
            },
        ]

        with patch.object(manabox_import, "_fetch_scryfall_cards", return_value={"abc-123": fake_card}), \
             patch.object(manabox_import.mtgjson_crosswalk, "get_uuid", return_value="fake-uuid"), \
             patch.object(manabox_import.mtgjson_crosswalk, "prefetch_sets"), \
             patch.object(
                 manabox_import.cardkingdom,
                 "get_prices",
                 return_value={"market": 3.50, "buylist": 1.25},
             ):
            result = manabox_import.import_rows(rows, owner="TestImporter")

        self.assertEqual(result["imported"], 2)  # near_mint and light_played, kept separate
        self.assertEqual(result["skipped"], 1)
        self.assertIn("Ghost Card", result["errors"][0])

        items = {i["condition"]: i for i in db.list_watchlist(owner="TestImporter")}
        self.assertEqual(set(items), {"Near Mint", "Lightly Played"})
        self.assertEqual(items["Near Mint"]["quantity"], 3)  # 2 + 1 merged; the 0-qty row added nothing
        self.assertEqual(items["Lightly Played"]["quantity"], 1)
        self.assertTrue(items["Near Mint"]["variant_id"].endswith(":near-mint"))
        self.assertTrue(items["Lightly Played"]["variant_id"].endswith(":lightly-played"))
        self.assertAlmostEqual(items["Near Mint"]["purchase_price"], 3.00)  # (2*2 + 1*5) / 3
        self.assertAlmostEqual(items["Lightly Played"]["purchase_price"], 1.50)

        for item in items.values():
            db.remove_from_watchlist(item["id"])

    def test_import_rows_reports_progress_through_all_three_phases(self):
        fake_card = {"id": "abc-123", "name": "Fake Card", "set": "tst", "set_name": "Test Set"}
        rows = [{"Scryfall ID": "abc-123", "Name": "Fake Card", "Set code": "tst", "Foil": "normal", "Quantity": "1"}]
        calls = []

        with patch.object(manabox_import, "_fetch_scryfall_cards", return_value={"abc-123": fake_card}), \
             patch.object(manabox_import.mtgjson_crosswalk, "get_uuid", return_value="fake-uuid"), \
             patch.object(manabox_import.mtgjson_crosswalk, "prefetch_sets") as mock_prefetch, \
             patch.object(manabox_import.cardkingdom, "get_prices", return_value={"market": 1.0, "buylist": 0.5}):
            # The two lower-level pieces (_fetch_scryfall_cards, prefetch_sets)
            # are mocked above for the rest of this test suite's purposes, but
            # here we want to confirm import_rows actually *passes* on_progress
            # through to them rather than dropping it, plus exercises its own
            # (real, unmocked) third-phase reporting.
            mock_prefetch.side_effect = lambda codes, on_progress=None: on_progress and on_progress(
                "Looking up Card Kingdom prices", 1, 1
            )
            manabox_import.import_rows(rows, owner="ProgressTest", on_progress=lambda *a: calls.append(a))

        phases = [c[0] for c in calls]
        self.assertIn("Looking up Card Kingdom prices", phases)
        self.assertIn("Saving to your watchlist", phases)
        save_calls = [c for c in calls if c[0] == "Saving to your watchlist"]
        self.assertEqual(save_calls[-1][1:], (1, 1))  # done == total on the last call

        item = db.list_watchlist(owner="ProgressTest")[0]
        db.remove_from_watchlist(item["id"])


class MtgjsonCrosswalkTests(unittest.TestCase):
    def test_prefetch_sets_dedupes_and_skips_cached(self):
        import mtgjson_crosswalk as mc

        original_cache = mc._cache
        mc._cache = {"fetched_sets": ["ALREADY"], "map": {}}
        try:
            calls = []
            with patch.object(mc, "_fetch_set", side_effect=lambda code: calls.append(code)):
                mc.prefetch_sets(["m10", "M10", "already", "m11", None, ""])
            # Case-insensitive dedupe, already-cached set skipped, blanks ignored.
            self.assertEqual(sorted(calls), ["M10", "M11"])
        finally:
            mc._cache = original_cache

    def test_prefetch_sets_reports_progress_and_survives_a_bad_set(self):
        import mtgjson_crosswalk as mc

        original_cache = mc._cache
        mc._cache = {"fetched_sets": [], "map": {}}
        try:
            progress_calls = []

            def fake_fetch(code):
                if code == "BAD":
                    raise RuntimeError("boom")  # simulates an unexpected bug, not a normal RequestException

            with patch.object(mc, "_fetch_set", side_effect=fake_fetch):
                mc.prefetch_sets(
                    ["m10", "bad"], on_progress=lambda phase, done, total: progress_calls.append((phase, done, total))
                )
            # Both sets get counted as "done" even though one raised —
            # a bad set shouldn't stall progress reporting for the rest.
            self.assertEqual(len(progress_calls), 2)
            self.assertEqual(progress_calls[-1][1:], (2, 2))
            self.assertTrue(all(c[0] == "Looking up Card Kingdom prices" for c in progress_calls))
        finally:
            mc._cache = original_cache


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

    def test_unknown_import_job_status_404s(self):
        resp = self.client.get("/api/watchlist/import/not-a-real-job-id/status")
        self.assertEqual(resp.status_code, 404)

    def test_watchlist_add_requires_fields(self):
        resp = self.client.post("/api/watchlist", json={})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
