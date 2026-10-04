"""Basic smoke tests: catch import errors and obvious crashes in the core
modules before they reach production. Not exhaustive — no network calls
(Scryfall/Telegram/GitHub) are made, so this stays fast and doesn't depend
on external services being up. Run with: py -m unittest test_smoke -v

IMPORTANT: TCG_DB_PATH must be set before any of our modules are imported,
since db.py reads it once at import time — this is why it's set at the top
of this file, before the `import db` below.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_list_watchlist_sort_options(self):
        def make(variant_id, name, price, quantity=1, purchase_price=None):
            item = db.add_to_watchlist(
                {
                    "variant_id": variant_id,
                    "card_id": variant_id,
                    "game": "Magic: The Gathering",
                    "name": name,
                    "set_name": "Test Set",
                    "condition": "Near Mint",
                    "printing": "Normal",
                    "owner": "SortTest",
                    "quantity": quantity,
                    "purchase_price": purchase_price,
                }
            )
            db.record_price(variant_id, price, kind="market")
            return item

        # Zebra: cheap but high quantity (biggest total value); no cost basis.
        # Apple: mid price, halved by a loss (worst gain).
        # Mango: pricier and doubled (best gain).
        make("sort-zebra:nonfoil", "Zebra Card", price=2.0, quantity=100)
        make("sort-apple:nonfoil", "Apple Card", price=5.0, purchase_price=10.0)
        make("sort-mango:nonfoil", "Mango Card", price=20.0, purchase_price=10.0)

        try:
            by_name = [i["name"] for i in db.list_watchlist(owner="SortTest", sort="name")]
            self.assertEqual(by_name, ["Apple Card", "Mango Card", "Zebra Card"])

            by_price = [i["name"] for i in db.list_watchlist(owner="SortTest", sort="price")]
            self.assertEqual(by_price, ["Mango Card", "Apple Card", "Zebra Card"])

            by_value = [i["name"] for i in db.list_watchlist(owner="SortTest", sort="value")]
            self.assertEqual(by_value, ["Zebra Card", "Mango Card", "Apple Card"])  # 100*2=200 tops both

            by_gain = [i["name"] for i in db.list_watchlist(owner="SortTest", sort="gain")]
            # Mango doubled (+100%), Apple halved (-50%); Zebra has no cost
            # basis, so its gain is undefined (NULL) and sinks to the end.
            self.assertEqual(by_gain, ["Mango Card", "Apple Card", "Zebra Card"])
        finally:
            for item in db.list_watchlist(owner="SortTest"):
                db.remove_from_watchlist(item["id"])

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

    def test_record_article_if_new_dedupes_by_url(self):
        article_id = db.record_article_if_new("https://example.com/a", "Test Site", "A Title", None)
        self.assertIsNotNone(article_id)
        self.assertIsNone(db.record_article_if_new("https://example.com/a", "Test Site", "A Title", None))

    def test_news_signal_round_trip(self):
        article_id = db.record_article_if_new("https://example.com/signal-test", "Test Site", "Title", None)
        signal_id = db.record_news_signal(article_id, "Some Card", "combo_discovery", True)
        self.assertIsNotNone(signal_id)
        conn = db.get_connection()
        row = conn.execute("SELECT card_name, signal_type, is_genuine_interest FROM news_signals WHERE id = ?", (signal_id,)).fetchone()
        conn.close()
        self.assertEqual(row["card_name"], "Some Card")
        self.assertEqual(row["signal_type"], "combo_discovery")
        self.assertEqual(row["is_genuine_interest"], 1)

    def test_had_recent_news_signal_respects_window_and_genuineness(self):
        # Within window + genuine -> True
        article_id = db.record_article_if_new(
            "https://example.com/news-1", "Test Site", "Title", "2026-08-01T00:00:00+00:00"
        )
        db.record_news_signal(article_id, "Windowed Card", "combo_discovery", True)
        self.assertTrue(db.had_recent_news_signal("Windowed Card", "2026-08-30T00:00:00+00:00", window_days=60))

        # Outside the window -> False
        self.assertFalse(db.had_recent_news_signal("Windowed Card", "2026-12-01T00:00:00+00:00", window_days=60))

        # After the cutoff (not yet "prior") -> False
        self.assertFalse(db.had_recent_news_signal("Windowed Card", "2026-07-01T00:00:00+00:00", window_days=60))

        # Not genuine -> False
        article_id2 = db.record_article_if_new(
            "https://example.com/news-2", "Test Site", "Title", "2026-08-01T00:00:00+00:00"
        )
        db.record_news_signal(article_id2, "Routine Card", "routine_mention", False)
        self.assertFalse(db.had_recent_news_signal("Routine Card", "2026-08-30T00:00:00+00:00", window_days=60))

        # Unknown card -> False
        self.assertFalse(db.had_recent_news_signal("Nonexistent Card", "2026-08-30T00:00:00+00:00", window_days=60))


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

    def test_news_signal_presence_is_a_real_feature_in_training(self):
        # Build a dataset where "had a genuine news signal beforehand"
        # perfectly predicts "good pick", with price/pct_change held
        # identical across both groups — isolates the news-signal
        # feature's effect rather than conflating it with price. Uses a
        # dynamically-recent published_at (not a hardcoded date) since
        # recommendations get a real CURRENT_TIMESTAMP sent_at — a fixed
        # past date drifts out of the window as real time passes, which
        # is exactly what silently broke this the first time it ran.
        from datetime import datetime, timedelta, timezone

        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        rec_ids = []
        for i in range(recommender.MIN_LABELED_EXAMPLES):
            with_news = i % 2 == 0
            card_name = f"NewsFeatureCard {i}"
            rec_id = db.record_recommendation(card_name, "Set", 10.0, 15.0, 50.0)
            rec_ids.append(rec_id)
            db.set_recommendation_feedback(rec_id, "good" if with_news else "bad")
            if with_news:
                article_id = db.record_article_if_new(f"https://example.com/nf-{i}", "Test Site", "Title", recent)
                db.record_news_signal(article_id, card_name, "combo_discovery", True)

        def cleanup():
            conn = db.get_connection()
            conn.execute("DELETE FROM recommendations WHERE id IN ({})".format(",".join("?" * len(rec_ids))), rec_ids)
            conn.execute("DELETE FROM news_signals WHERE card_name LIKE 'NewsFeatureCard%'")
            conn.execute("DELETE FROM news_articles WHERE url LIKE 'https://example.com/nf-%'")
            conn.commit()
            conn.close()

        self.addCleanup(cleanup)

        model = recommender.train()
        self.assertIsNotNone(model)

        score_with_news = recommender.score(model, 10.0, 50.0, "NewsFeatureCard 0")  # had a signal
        score_without_news = recommender.score(model, 10.0, 50.0, "Some Other Card")  # no signal recorded

        self.assertGreater(score_with_news, score_without_news)


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

    def test_reconcile_canonical_groups_merges_split_card_rows(self):
        # A split/adventure/DFC card's two per-face rows (see module
        # docstring): each only has data on alternating days, and neither
        # row alone shows a day-over-day change — but the merged series
        # does, and reconcile_canonical_groups is what makes compute_movers
        # see it as one continuous series instead of two gappy ones.
        conn = ach._get_connection(self.db_path)
        today = ach._day_number(__import__("datetime").date.today())

        conn.execute(
            "INSERT INTO cards (mtgjson_uuid, name, set_code, scryfall_id, is_token) "
            "VALUES ('face-a', 'Split Card // Other Half', 'TST', 'shared-sid', 0)"
        )
        conn.execute(
            "INSERT INTO cards (mtgjson_uuid, set_code, scryfall_id) "
            "VALUES ('face-b', 'TST', 'shared-sid')"
        )
        face_a_id = conn.execute("SELECT id FROM cards WHERE mtgjson_uuid = 'face-a'").fetchone()[0]
        face_b_id = conn.execute("SELECT id FROM cards WHERE mtgjson_uuid = 'face-b'").fetchone()[0]
        # face-a carried yesterday's price, face-b carries today's — never
        # both on the same day, matching what the real feed does.
        conn.executemany(
            "INSERT INTO price_history (card_id, day, price_cents) VALUES (?, ?, ?)",
            [(face_a_id, today - 1, 100), (face_b_id, today, 200)],
        )
        conn.commit()
        conn.close()

        ach.reconcile_canonical_groups(self.db_path)

        conn = ach._get_connection(self.db_path)
        canonical = conn.execute("SELECT canonical_card_id FROM cards WHERE id = ?", (face_b_id,)).fetchone()[0]
        self.assertEqual(canonical, face_a_id)
        # The canonical (face-a) row had no name of its own set — wait, it
        # does; check the *other* direction: face-b's row (no name) should
        # not have been promoted to canonical over face-a (which has one).
        name = conn.execute("SELECT name FROM cards WHERE id = ?", (face_a_id,)).fetchone()[0]
        self.assertEqual(name, "Split Card // Other Half")
        conn.close()

        movers = ach.compute_movers(self.db_path)
        names = {m["name"] for m in movers["daily_gainers"]}
        self.assertIn("Split Card // Other Half", names)

    def test_reconcile_canonical_groups_physically_moves_price_history(self):
        # compute_movers()/get_by_uuid() rely on price_history actually
        # being consolidated onto the canonical card_id — not merged at
        # query time (a self-join on a computed grouping column across
        # the full table, which hung for hours at real production scale
        # and got caught before it could do any damage). This locks in
        # that the move is physical, so a regression back to query-time
        # merging would fail loudly here instead of only at scale.
        conn = ach._get_connection(self.db_path)
        conn.execute("INSERT INTO cards (mtgjson_uuid, name, set_code, scryfall_id) VALUES ('face-a', 'X', 'TST', 'sid')")
        conn.execute("INSERT INTO cards (mtgjson_uuid, set_code, scryfall_id) VALUES ('face-b', 'TST', 'sid')")
        face_a_id = conn.execute("SELECT id FROM cards WHERE mtgjson_uuid = 'face-a'").fetchone()[0]
        face_b_id = conn.execute("SELECT id FROM cards WHERE mtgjson_uuid = 'face-b'").fetchone()[0]
        conn.execute("INSERT INTO price_history (card_id, day, price_cents) VALUES (?, 100, 35)", (face_b_id,))
        conn.commit()
        conn.close()

        ach.reconcile_canonical_groups(self.db_path)

        conn = ach._get_connection(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM price_history WHERE card_id = ?", (face_b_id,)).fetchone()[0], 0
        )
        moved = conn.execute("SELECT day, price_cents FROM price_history WHERE card_id = ?", (face_a_id,)).fetchone()
        self.assertEqual(tuple(moved), (100, 35))
        conn.close()

    def test_reconcile_canonical_groups_backfills_canonical_row_labels_from_child(self):
        # If backfill_names() happens to label the *second*-seen (higher
        # id) row first, the canonical (lowest id) row must still end up
        # with real labels — compute_movers() filters on c.name IS NOT NULL,
        # so an unlabeled canonical row would silently drop the whole group.
        conn = ach._get_connection(self.db_path)
        conn.execute("INSERT INTO cards (mtgjson_uuid, scryfall_id) VALUES ('face-a', 'shared-sid')")
        conn.execute(
            "INSERT INTO cards (mtgjson_uuid, name, set_code, set_name, scryfall_id, is_token) "
            "VALUES ('face-b', 'Split Card // Other Half', 'TST', 'Test Set', 'shared-sid', 0)"
        )
        conn.commit()
        conn.close()

        ach.reconcile_canonical_groups(self.db_path)

        conn = ach._get_connection(self.db_path)
        row = conn.execute(
            "SELECT name, set_code, set_name, is_token FROM cards WHERE mtgjson_uuid = 'face-a'"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("Split Card // Other Half", "TST", "Test Set", 0))


class AllCardsLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "all_cards_lookup_test.db"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_get_by_uuid_merges_history_across_canonical_group(self):
        import all_cards_lookup

        conn = ach._get_connection(self.db_path)
        conn.execute(
            "INSERT INTO cards (mtgjson_uuid, name, set_code, scryfall_id) "
            "VALUES ('face-a', 'Split Card // Other Half', 'TST', 'shared-sid')"
        )
        conn.execute("INSERT INTO cards (mtgjson_uuid, set_code, scryfall_id) VALUES ('face-b', 'TST', 'shared-sid')")
        face_a_id = conn.execute("SELECT id FROM cards WHERE mtgjson_uuid = 'face-a'").fetchone()[0]
        face_b_id = conn.execute("SELECT id FROM cards WHERE mtgjson_uuid = 'face-b'").fetchone()[0]
        conn.executemany(
            "INSERT INTO price_history (card_id, day, price_cents) VALUES (?, ?, ?)",
            [(face_a_id, 100, 35), (face_b_id, 101, 49), (face_a_id, 102, 40)],
        )
        conn.commit()
        conn.close()

        ach.reconcile_canonical_groups(self.db_path)

        with patch.object(all_cards_lookup, "_ensure_cached", return_value=self.db_path):
            result = all_cards_lookup.get_by_uuid("face-b")  # looked up by the *child* uuid

        self.assertEqual(len(result["history"]), 3)  # merged across both rows, not just face-b's one point


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
             patch.object(manabox_import.tcgmarketplace, "prefetch_ids"), \
             patch.object(manabox_import.tcgmarketplace, "prefetch_prices"), \
             patch.object(manabox_import.tcgmarketplace, "get_price_for_card", return_value=None), \
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
             patch.object(manabox_import.tcgmarketplace, "prefetch_ids"), \
             patch.object(manabox_import.tcgmarketplace, "prefetch_prices"), \
             patch.object(manabox_import.tcgmarketplace, "get_price_for_card", return_value=None), \
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

    def test_import_syncs_removals_but_spares_ambiguous_matches(self):
        # Pre-existing state for this owner, as if from an earlier import.
        sold_item = db.add_to_watchlist(
            {
                "variant_id": "sold-card-id:nonfoil:near-mint",
                "card_id": "sold-card-id",
                "game": "Magic: The Gathering",
                "name": "Sold Card",
                "set_name": "Test Set",
                "condition": "Near Mint",
                "printing": "Normal",
                "owner": "SyncTest",
                "quantity": 1,
            }
        )
        ambiguous_item = db.add_to_watchlist(
            {
                "variant_id": "ambiguous-id:nonfoil:near-mint",
                "card_id": "ambiguous-id",
                "game": "Magic: The Gathering",
                "name": "Ambiguous Card",
                "set_name": "Test Set",
                "condition": "Near Mint",
                "printing": "Normal",
                "owner": "SyncTest",
                "quantity": 1,
            }
        )

        new_card = {"id": "new-card-id", "name": "New Card", "set": "tst", "set_name": "Test Set"}
        rows = [
            {"Scryfall ID": "new-card-id", "Name": "New Card", "Set code": "tst", "Foil": "normal", "Quantity": "1"},
            # "ambiguous-id" is present in the CSV but fails to resolve on
            # Scryfall this time (not in the mocked return dict below) — the
            # previously-tracked card for it must be spared, not treated as
            # absent, since we can't confirm it's actually gone.
            {"Scryfall ID": "ambiguous-id", "Name": "Ambiguous Card", "Set code": "tst", "Foil": "normal", "Quantity": "1"},
        ]

        with patch.object(manabox_import, "_fetch_scryfall_cards", return_value={"new-card-id": new_card}), \
             patch.object(manabox_import.mtgjson_crosswalk, "get_uuid", return_value=None), \
             patch.object(manabox_import.mtgjson_crosswalk, "prefetch_sets"), \
             patch.object(manabox_import.tcgmarketplace, "prefetch_ids"), \
             patch.object(manabox_import.tcgmarketplace, "prefetch_prices"), \
             patch.object(manabox_import.tcgmarketplace, "get_price_for_card", return_value=None), \
             patch.object(manabox_import.cardkingdom, "get_prices", return_value=None):
            result = manabox_import.import_rows(rows, owner="SyncTest")

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["removed"], 1)

        remaining = {i["variant_id"] for i in db.list_watchlist(owner="SyncTest")}
        self.assertNotIn("sold-card-id:nonfoil:near-mint", remaining)  # genuinely absent -> removed
        self.assertIn("ambiguous-id:nonfoil:near-mint", remaining)  # present but unresolved -> spared
        self.assertIn("new-card-id:nonfoil:near-mint", remaining)  # newly imported

        for item in db.list_watchlist(owner="SyncTest"):
            db.remove_from_watchlist(item["id"])


class RefreshJobTests(unittest.TestCase):
    def test_alert_requires_both_dollar_and_percent_threshold(self):
        import refresh_job

        items = [
            {  # clears both $2 and 10% -> should alert
                "variant_id": "v1", "name": "Big Mover", "set_name": "Set", "card_id": None,
                "mtgjson_id": "uuid-1", "printing": "Normal", "latest_price": 20.0,
            },
            {  # clears 10% but not $2 (cheap-card noise) -> should NOT alert
                "variant_id": "v2", "name": "Cheap Noise", "set_name": "Set", "card_id": None,
                "mtgjson_id": "uuid-2", "printing": "Normal", "latest_price": 1.00,
            },
            {  # clears $2 but not 10% -> should NOT alert
                "variant_id": "v3", "name": "Slow Creep", "set_name": "Set", "card_id": None,
                "mtgjson_id": "uuid-3", "printing": "Normal", "latest_price": 100.0,
            },
        ]
        prices_by_uuid = {
            "uuid-1": {"market": 25.0, "buylist": None},   # +5.00, +25%
            "uuid-2": {"market": 1.15, "buylist": None},   # +0.15, +15%
            "uuid-3": {"market": 102.0, "buylist": None},  # +2.00, +2%
        }
        with patch.object(refresh_job.db, "list_watchlist", return_value=items), \
             patch.object(refresh_job.tcgmarketplace, "prefetch_ids"), \
             patch.object(refresh_job.tcgmarketplace, "prefetch_prices"), \
             patch.object(refresh_job.tcgmarketplace, "get_price_for_card", return_value=None), \
             patch.object(refresh_job.cardkingdom, "get_prices", side_effect=lambda uid, foil: prices_by_uuid[uid]), \
             patch.object(refresh_job.db, "record_price"), \
             patch.object(refresh_job.db, "update_cardkingdom_price"):
            alerts = refresh_job.refresh_watchlist_prices()

        self.assertEqual([a["name"] for a in alerts], ["Big Mover"])

    def test_chunk_lines_keeps_short_batches_in_one_chunk(self):
        import refresh_job

        lines = ["short line"] * 5
        self.assertEqual(refresh_job._chunk_lines(lines, limit=1000), [lines])

    def test_chunk_lines_splits_when_over_limit(self):
        import refresh_job

        lines = ["x" * 30 for _ in range(10)]  # 10 lines, ~31 chars each with newlines
        chunks = refresh_job._chunk_lines(lines, limit=100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len("\n".join(chunk)), 100)
        # No line dropped or duplicated across the split.
        self.assertEqual([line for chunk in chunks for line in chunk], lines)

    def test_notify_alerts_splits_large_batch_into_multiple_messages(self):
        import refresh_job

        alerts = [
            {
                "name": f"Card {i}",
                "set_name": "Test Set",
                "printing": "Normal",
                "owner": None,
                "price_before": 1.0,
                "price_now": 2.0,
                "pct_change": 100.0,
            }
            for i in range(200)  # enough to blow past 4096 chars in one message
        ]
        with patch.object(refresh_job.telegram_notify, "send_message") as mock_send:
            refresh_job.notify_alerts(alerts)

        self.assertGreater(mock_send.call_count, 1)
        for call in mock_send.call_args_list:
            self.assertLessEqual(len(call.args[0]), refresh_job.TELEGRAM_MESSAGE_LIMIT)
        # Every alert line appears exactly once across all the sent messages.
        sent_text = "\n".join(call.args[0] for call in mock_send.call_args_list)
        for i in range(200):
            self.assertEqual(sent_text.count(f"Card {i} ["), 1)

    def test_refresh_self_heals_stale_mtgjson_id_for_multi_candidate_cards(self):
        import refresh_job

        # A split/adventure card whose stored mtgjson_id ("uuid-a") no
        # longer has price data — the crosswalk now prefers "uuid-b" (see
        # MtgjsonCrosswalkTests) — refresh should correct the stored id
        # and fetch prices under the corrected one, not the stale one.
        item = {
            "variant_id": "v1",
            "name": "Ardenvale Tactician // Dizzying Swoop",
            "set_name": "Throne of Eldraine",
            "card_id": "sid1",
            "mtgjson_id": "uuid-a",
            "printing": "Normal",
            "latest_price": None,
        }
        with patch.object(refresh_job.db, "list_watchlist", return_value=[item]), \
             patch.object(refresh_job.tcgmarketplace, "prefetch_ids"), \
             patch.object(refresh_job.tcgmarketplace, "prefetch_prices"), \
             patch.object(refresh_job.tcgmarketplace, "get_price_for_card", return_value=None), \
             patch.object(refresh_job.mtgjson_crosswalk, "get_uuid_candidates", return_value=["uuid-a", "uuid-b"]), \
             patch.object(refresh_job.mtgjson_crosswalk, "get_uuid", return_value="uuid-b"), \
             patch.object(refresh_job.db, "update_mtgjson_id") as mock_update_id, \
             patch.object(
                 refresh_job.cardkingdom, "get_prices", return_value={"market": 1.23, "buylist": None}
             ) as mock_get_prices, \
             patch.object(refresh_job.db, "record_price"), \
             patch.object(refresh_job.db, "update_cardkingdom_price"):
            refresh_job.refresh_watchlist_prices()

        mock_update_id.assert_called_once_with("v1", "uuid-b")
        mock_get_prices.assert_called_once_with("uuid-b", foil=False)

    def test_refresh_leaves_single_candidate_mtgjson_id_untouched(self):
        import refresh_job

        item = {
            "variant_id": "v1",
            "name": "Plain Old Card",
            "set_name": "Some Set",
            "card_id": "sid1",
            "mtgjson_id": "uuid-only",
            "printing": "Normal",
            "latest_price": None,
        }
        with patch.object(refresh_job.db, "list_watchlist", return_value=[item]), \
             patch.object(refresh_job.tcgmarketplace, "prefetch_ids"), \
             patch.object(refresh_job.tcgmarketplace, "prefetch_prices"), \
             patch.object(refresh_job.tcgmarketplace, "get_price_for_card", return_value=None), \
             patch.object(refresh_job.mtgjson_crosswalk, "get_uuid_candidates", return_value=["uuid-only"]), \
             patch.object(refresh_job.db, "update_mtgjson_id") as mock_update_id, \
             patch.object(
                 refresh_job.cardkingdom, "get_prices", return_value={"market": 1.23, "buylist": None}
             ) as mock_get_prices, \
             patch.object(refresh_job.db, "record_price"), \
             patch.object(refresh_job.db, "update_cardkingdom_price"):
            refresh_job.refresh_watchlist_prices()

        mock_update_id.assert_not_called()
        mock_get_prices.assert_called_once_with("uuid-only", foil=False)


class MtgjsonCrosswalkTests(unittest.TestCase):
    def test_get_uuid_prefers_candidate_with_actual_price_data(self):
        # Split/adventure/DFC cards: MTGJSON emits one card object per face
        # sharing the same Scryfall id (see module docstring) — only one
        # face's uuid usually has real Card Kingdom price data, and it
        # isn't reliably the first (side "a") one.
        import mtgjson_crosswalk as mc

        original_cache = mc._cache
        mc._cache = {"fetched_sets": ["ELD"], "map": {"sid1": ["uuid-a", "uuid-b"]}}
        try:
            with patch("cardkingdom.has_prices", side_effect=lambda u: u == "uuid-b"):
                self.assertEqual(mc.get_uuid("sid1", "ELD"), "uuid-b")
                self.assertEqual(mc.get_uuid_candidates("sid1", "ELD"), ["uuid-a", "uuid-b"])
        finally:
            mc._cache = original_cache

    def test_get_uuid_falls_back_to_first_candidate_if_none_priced(self):
        import mtgjson_crosswalk as mc

        original_cache = mc._cache
        mc._cache = {"fetched_sets": ["ELD"], "map": {"sid1": ["uuid-a", "uuid-b"]}}
        try:
            with patch("cardkingdom.has_prices", return_value=False):
                self.assertEqual(mc.get_uuid("sid1", "ELD"), "uuid-a")
        finally:
            mc._cache = original_cache

    def test_get_uuid_candidates_cache_only_lookup_skips_fetch_without_set_code(self):
        import mtgjson_crosswalk as mc

        original_cache = mc._cache
        mc._cache = {"fetched_sets": [], "map": {}}
        try:
            with patch.object(mc, "_fetch_set") as mock_fetch:
                self.assertEqual(mc.get_uuid_candidates("unknown-sid", set_code=None), [])
                mock_fetch.assert_not_called()
        finally:
            mc._cache = original_cache

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


class TcgMarketplaceTests(unittest.TestCase):
    def setUp(self):
        import tcgmarketplace as tcg

        self.tcg = tcg
        self._orig_id_cache = tcg._id_cache
        self._orig_price_cache = dict(tcg._price_cache)
        tcg._id_cache = {}
        tcg._price_cache = {}
        # Without this, find_id()'s real cache-save would write test data
        # into the actual tcgmarketplace_id_cache.json on disk — which
        # happened once already and poisoned it with a fake id.
        save_patcher = patch.object(tcg, "_save_id_cache")
        save_patcher.start()
        self.addCleanup(save_patcher.stop)

    def tearDown(self):
        self.tcg._id_cache = self._orig_id_cache
        self.tcg._price_cache = self._orig_price_cache

    def test_find_id_matches_by_normalized_set_name(self):
        results = [
            {"id": 1, "setname": "Some Other Set"},
            {"id": 2, "setname": "  30th  Anniversary Edition "},  # extra whitespace shouldn't break the match
            {"id": 3, "setname": "Yet Another Set"},
        ]
        with patch.object(self.tcg, "_search", return_value=results) as mock_search:
            found = self.tcg.find_id("Lightning Bolt", "30th Anniversary Edition")
            self.assertEqual(found, 2)

            # Second lookup for the same key must hit the cache, not search again.
            found_again = self.tcg.find_id("Lightning Bolt", "30th Anniversary Edition")
            self.assertEqual(found_again, 2)
            mock_search.assert_called_once()

    def test_find_id_caches_negative_result_without_immediate_recheck(self):
        with patch.object(self.tcg, "_search", return_value=[{"id": 1, "setname": "Nonmatching Set"}]) as mock_search:
            found = self.tcg.find_id("Some Card", "A Set It's Not In")
            self.assertIsNone(found)
            self.assertIsNone(self.tcg.find_id("Some Card", "A Set It's Not In"))
            mock_search.assert_called_once()  # second call served from the negative cache

    def test_get_price_prefers_price_from_then_falls_back_to_day1(self):
        def fake_response(price_from, day1):
            resp = type("Resp", (), {})()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"data": {"data": [{"price_from": price_from, "day1": day1}]}}
            return resp

        with patch.object(self.tcg._session, "get", return_value=fake_response("12.50", "9.00")):
            self.assertEqual(self.tcg.get_price(111), 12.50)

        self.tcg._price_cache = {}  # bypass the price TTL cache for the next case
        with patch.object(self.tcg._session, "get", return_value=fake_response(None, "9.00")):
            self.assertEqual(self.tcg.get_price(222), 9.00)

    def test_prefetch_ids_and_prices_report_progress(self):
        with patch.object(self.tcg, "find_id", side_effect=[10, 20]) as mock_find, \
             patch.object(self.tcg, "get_price", return_value=1.0) as mock_price:
            id_calls = []
            self.tcg.prefetch_ids(
                [("A", "Set A"), ("B", "Set B")],
                on_progress=lambda phase, done, total: id_calls.append((phase, done, total)),
            )
            self.assertEqual(mock_find.call_count, 2)
            self.assertEqual(len(id_calls), 2)
            self.assertEqual(id_calls[-1][1:], (2, 2))

            price_calls = []
            self.tcg.prefetch_prices(
                [10, 20, None],  # None must be filtered out, not passed to get_price
                on_progress=lambda phase, done, total: price_calls.append((phase, done, total)),
            )
            self.assertEqual(mock_price.call_count, 2)
            self.assertEqual(len(price_calls), 2)


def _fake_rss(items):
    """Builds a minimal Google-News-RSS-shaped XML string from
    [(title, link, pub_date_rfc822, source)] tuples, for mocking
    requests.get in MtgNewsTests without hitting the real network."""
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link><pubDate>{pub_date}</pubDate>"
        f"<source url=\"https://example.com\">{source}</source></item>"
        for title, link, pub_date, source in items
    )
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'.encode()


def _fake_sync_playwright(pages, browser=None):
    """Builds a fake object for mtg_news.sync_playwright — mtg_news.
    BrowserSession calls sync_playwright().start() (not `with sync_playwright()
    as p`), so the mock must support that same shape: a callable returning
    something with .start() -> p, and p.chromium.launch() -> browser."""
    fake_browser = browser if browser is not None else MagicMock()
    if browser is None:
        fake_browser.new_page.side_effect = pages

    fake_p = MagicMock()
    fake_p.chromium.launch.return_value = fake_browser

    fake_instance = MagicMock()
    fake_instance.start.return_value = fake_p
    return fake_instance


def _fake_playwright_context(page_behaviors):
    """Builds a fake sync_playwright()-shaped object for mocking
    mtg_news.py's headless-browser verification step, without launching a
    real browser in tests. `page_behaviors` is a list of either a body-text
    string (what inner_text("body") returns for that candidate, in call
    order) or an Exception instance (what goto() raises instead, simulating
    a blocked/failed fetch)."""
    pages = []
    for behavior in page_behaviors:
        page = MagicMock()
        page.url = "https://example.com/real-article"
        if isinstance(behavior, Exception):
            page.goto.side_effect = behavior
        else:
            page.inner_text.return_value = behavior
        pages.append(page)
    return _fake_sync_playwright(pages)


def _fake_feed_xml(items):
    """Builds a minimal WordPress-RSS-shaped XML string from
    [(title, link, pub_date_rfc822, content_html)] tuples, for mocking
    requests.get in MtgNewsFeedTests."""
    body = "".join(
        f'<item><title>{title}</title><link>{link}</link><pubDate>{pub_date}</pubDate>'
        f'<content:encoded><![CDATA[{content_html}]]></content:encoded></item>'
        for title, link, pub_date, content_html in items
    )
    return (
        '<?xml version="1.0"?>'
        '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel>{body}</channel></rss>"
    ).encode()


class MtgNewsFeedTests(unittest.TestCase):
    def _fake_response(self, xml_bytes):
        resp = type("Resp", (), {})()
        resp.content = xml_bytes
        resp.raise_for_status = lambda: None
        return resp

    def test_fetch_recent_articles_parses_full_content_and_strips_html(self):
        import mtg_news_feed as mnf

        xml = _fake_feed_xml(
            [("A Title", "https://example.com/a", "Fri, 02 Oct 2026 20:00:00 +0000", "<p>Hello <b>World</b></p>")]
        )
        with patch.object(mnf.requests, "get", return_value=self._fake_response(xml)):
            articles = mnf.fetch_recent_articles()

        mine = [a for a in articles if a["link"] == "https://example.com/a"]
        self.assertGreaterEqual(len(mine), 1)  # the same fake response is returned for every feed URL
        self.assertEqual(mine[0]["text"].strip(), "Hello World")
        self.assertEqual(mine[0]["published"].year, 2026)

    def test_fetch_recent_articles_skips_a_down_feed_without_crashing(self):
        import mtg_news_feed as mnf

        good_xml = _fake_feed_xml([("Good", "https://example.com/good", "Fri, 02 Oct 2026 20:00:00 +0000", "ok")])

        def fake_get(url, **kwargs):
            if "mtgrocks" in url:
                raise mnf.requests.RequestException("down")
            return self._fake_response(good_xml)

        with patch.object(mnf.requests, "get", side_effect=fake_get):
            articles = mnf.fetch_recent_articles()

        self.assertTrue(any(a["link"] == "https://example.com/good" for a in articles))

    def test_find_mentioned_cards_requires_multiword_by_default(self):
        import mtg_news_feed as mnf

        names = ["Exile", "Darklight Phoenix"]
        found = mnf.find_mentioned_cards("Exile this creature, then cast Darklight Phoenix.", names)
        self.assertEqual(found, ["Darklight Phoenix"])

    def test_find_mentioned_cards_respects_word_boundaries(self):
        import mtg_news_feed as mnf

        found = mnf.find_mentioned_cards("Check out Boltwing Hatchling in this deck.", ["Bolt Hatchling"])
        self.assertEqual(found, [])

    def test_find_mentioned_cards_can_include_single_word_when_requested(self):
        import mtg_news_feed as mnf

        found = mnf.find_mentioned_cards("Solitude is a strong card.", ["Solitude"], require_multiword=False)
        self.assertEqual(found, ["Solitude"])


class NewsSignalsTests(unittest.TestCase):
    def setUp(self):
        import news_signals

        self.news_signals = news_signals
        self.article = {
            "source": "Test Site",
            "title": "New Combo Discovered",
            "link": "https://example.com/combo-article",
            "published": None,
            "text": "This deck uses Darklight Phoenix to great effect.",
        }

    def test_classify_mentions_empty_when_claude_not_configured(self):
        with patch.object(self.news_signals.claude_client, "configured", return_value=False):
            result = self.news_signals._classify_mentions(self.article, ["Darklight Phoenix"])
        self.assertEqual(result, [])

    def test_classify_mentions_strips_markdown_fences_and_filters_hallucinations(self):
        fake_block = type("Block", (), {"type": "text", "text": (
            '```json\n[{"card_name": "Darklight Phoenix", "is_genuine_interest": true, '
            '"signal_type": "combo_discovery"}, {"card_name": "Made Up Card", '
            '"is_genuine_interest": true, "signal_type": "combo_discovery"}]\n```'
        )})()
        fake_response = type("Response", (), {"content": [fake_block]})()
        fake_client = type("Client", (), {})()
        fake_client.messages = type("Messages", (), {"create": lambda self, **kw: fake_response})()

        with patch.object(self.news_signals.claude_client, "configured", return_value=True), \
             patch.object(self.news_signals.claude_client, "get_client", return_value=fake_client):
            result = self.news_signals._classify_mentions(self.article, ["Darklight Phoenix"])

        self.assertEqual(len(result), 1)  # "Made Up Card" wasn't in the candidate list — dropped
        self.assertEqual(result[0]["card_name"], "Darklight Phoenix")

    def test_classify_mentions_caps_candidates_and_scales_max_tokens(self):
        # Live testing against a real day's articles found a fixed
        # max_tokens too small once an article had enough mentions —
        # Claude's JSON got cut off mid-string and the whole batch was
        # lost to a parse error. Scaling max_tokens with candidate count
        # (and capping candidates at all, for cost) fixes that.
        import news_signals as ns

        many_names = [f"Card {i}" for i in range(50)]
        captured_kwargs = {}

        def fake_create(**kwargs):
            captured_kwargs.update(kwargs)
            block = type("Block", (), {"type": "text", "text": "[]"})()
            return type("Response", (), {"content": [block]})()

        fake_client = type("Client", (), {})()
        fake_client.messages = type("Messages", (), {"create": staticmethod(fake_create)})()

        with patch.object(ns.claude_client, "configured", return_value=True), \
             patch.object(ns.claude_client, "get_client", return_value=fake_client):
            ns._classify_mentions(self.article, many_names)

        sent_names = json.loads(captured_kwargs["messages"][0]["content"].split("Candidate card names mentioned: ")[1])
        self.assertEqual(len(sent_names), ns.MAX_CANDIDATES_PER_ARTICLE)
        expected_tokens = ns.BASE_OUTPUT_TOKENS + ns.OUTPUT_TOKENS_PER_CANDIDATE * ns.MAX_CANDIDATES_PER_ARTICLE
        self.assertEqual(captured_kwargs["max_tokens"], expected_tokens)

    def test_classify_mentions_fails_soft_on_malformed_json(self):
        fake_block = type("Block", (), {"type": "text", "text": "not valid json at all"})()
        fake_response = type("Response", (), {"content": [fake_block]})()
        fake_client = type("Client", (), {})()
        fake_client.messages = type("Messages", (), {"create": lambda self, **kw: fake_response})()

        with patch.object(self.news_signals.claude_client, "configured", return_value=True), \
             patch.object(self.news_signals.claude_client, "get_client", return_value=fake_client):
            result = self.news_signals._classify_mentions(self.article, ["Darklight Phoenix"])

        self.assertEqual(result, [])

    def test_check_for_signals_skips_already_seen_articles(self):
        with patch.object(self.news_signals, "_load_known_card_names", return_value=["Darklight Phoenix"]), \
             patch.object(self.news_signals.mtg_news_feed, "fetch_recent_articles", return_value=[self.article]), \
             patch.object(self.news_signals.db, "record_article_if_new", return_value=None), \
             patch.object(self.news_signals.mtg_news_feed, "find_mentioned_cards") as mock_find:
            self.news_signals.check_for_signals()

        mock_find.assert_not_called()  # already-seen article never gets this far

    def test_check_for_signals_records_classified_signals_without_alerting(self):
        # news_signals.py no longer sends anything to Telegram — the
        # signals it finds only feed recommender.py's model
        # (db.had_recent_news_signal), so this just has to confirm the
        # row gets recorded, not that any message goes out.
        with patch.object(self.news_signals, "_load_known_card_names", return_value=["Darklight Phoenix"]), \
             patch.object(self.news_signals.mtg_news_feed, "fetch_recent_articles", return_value=[self.article]), \
             patch.object(self.news_signals.db, "record_article_if_new", return_value=42), \
             patch.object(
                 self.news_signals, "_classify_mentions",
                 return_value=[{"card_name": "Darklight Phoenix", "is_genuine_interest": True, "signal_type": "combo_discovery"}],
             ), \
             patch.object(self.news_signals.db, "record_news_signal", return_value=7) as mock_record_signal:
            self.news_signals.check_for_signals()

        mock_record_signal.assert_called_once_with(42, "Darklight Phoenix", "combo_discovery", True)

    def test_check_for_signals_records_routine_mentions_as_not_genuine(self):
        with patch.object(self.news_signals, "_load_known_card_names", return_value=["Darklight Phoenix"]), \
             patch.object(self.news_signals.mtg_news_feed, "fetch_recent_articles", return_value=[self.article]), \
             patch.object(self.news_signals.db, "record_article_if_new", return_value=42), \
             patch.object(
                 self.news_signals, "_classify_mentions",
                 return_value=[{"card_name": "Darklight Phoenix", "is_genuine_interest": False, "signal_type": "routine_mention"}],
             ), \
             patch.object(self.news_signals.db, "record_news_signal", return_value=7) as mock_record_signal:
            self.news_signals.check_for_signals()

        mock_record_signal.assert_called_once_with(42, "Darklight Phoenix", "routine_mention", False)


class MtgNewsTests(unittest.TestCase):
    def _fake_response(self, xml_bytes):
        resp = type("Resp", (), {})()
        resp.content = xml_bytes
        resp.raise_for_status = lambda: None
        return resp

    def test_find_news_keeps_recent_verified_items(self):
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
        xml = _fake_rss([("Recent Article", "https://news.google.com/a", recent, "Some Site")])
        fake_ctx = _fake_playwright_context(["...this article is about Some Card and its price..."])
        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright", return_value=fake_ctx):
            results = mtg_news.find_news("Some Card")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Recent Article")
        self.assertEqual(results[0]["link"], "https://example.com/real-article")
        self.assertEqual(results[0]["source"], "example.com")

    def test_find_news_strips_redundant_source_suffix_from_title(self):
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
        # Google appends " - <source>" to every title itself (real example
        # observed live: "... - Polygon.com").
        xml = _fake_rss([("Big Price Spike - example.com", "https://news.google.com/a", recent, "Ignored")])
        fake_ctx = _fake_playwright_context(["Some Card appears right here in the body"])
        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright", return_value=fake_ctx):
            results = mtg_news.find_news("Some Card")

        self.assertEqual(results[0]["title"], "Big Price Spike")

    def test_find_news_strips_suffix_when_title_uses_human_readable_site_name(self):
        # The title's site-name suffix ("MTG Rocks") rarely matches the
        # bare resolved domain ("mtgrocks.com") as an exact string — the
        # strip has to compare normalized forms, not do a literal suffix
        # check (confirmed live: this exact "MTG Rocks"/"mtgrocks.com"
        # pair wasn't stripped before normalizing).
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
        xml = _fake_rss([("Big Price Spike - MTG Rocks", "https://news.google.com/a", recent, "Ignored")])
        page = MagicMock()
        page.url = "https://mtgrocks.com/some-article"
        page.inner_text.return_value = "Some Card appears right here in the body"
        fake_instance = _fake_sync_playwright([page])

        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright", return_value=fake_instance):
            results = mtg_news.find_news("Some Card")

        self.assertEqual(results[0]["title"], "Big Price Spike")
        self.assertEqual(results[0]["source"], "mtgrocks.com")

    def test_find_news_drops_stale_items_without_even_verifying(self):
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        stale = format_datetime(datetime.now(timezone.utc) - timedelta(days=mtg_news.RECENCY_DAYS + 30))
        xml = _fake_rss([("Old Set Guide", "https://news.google.com/old", stale, "Old Site")])
        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright") as mock_pw:
            results = mtg_news.find_news("Some Card")

        self.assertEqual(results, [])
        mock_pw.assert_not_called()  # recency filtering happens before ever opening a browser

    def test_find_news_rejects_headline_match_card_name_not_in_body(self):
        # The real false positives this is modeled on: "Island" matched a
        # Red Dead Redemption article, "Zack Fair" matched an FF7 deals
        # roundup — same-window headline coincidence, card never actually
        # mentioned in the body.
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
        xml = _fake_rss([("Unrelated Article", "https://news.google.com/a", recent, "Site")])
        fake_ctx = _fake_playwright_context(["this article never mentions that card at all"])
        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright", return_value=fake_ctx):
            results = mtg_news.find_news("Some Card")

        self.assertEqual(results, [])

    def test_find_news_skips_blocked_candidate_and_tries_the_next_one(self):
        # Observed live: some publishers block the headless browser outright
        # (a 403, or a real response that's just an empty HTML shell) — that
        # candidate is skipped (not assumed true or false), and the next
        # headline-level candidate is still tried.
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
        xml = _fake_rss(
            [
                ("Blocked Article", "https://news.google.com/blocked", recent, "Site"),
                ("Real Article", "https://news.google.com/real", recent, "Site"),
            ]
        )
        fake_ctx = _fake_playwright_context([RuntimeError("blocked"), "Some Card shows up here"])
        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright", return_value=fake_ctx):
            results = mtg_news.find_news("Some Card")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Real Article")

    def test_find_news_respects_max_results(self):
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=1))
        xml = _fake_rss([(f"Article {i}", f"https://news.google.com/{i}", recent, "Site") for i in range(5)])
        fake_ctx = _fake_playwright_context(["Some Card"] * 5)
        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright", return_value=fake_ctx):
            results = mtg_news.find_news("Some Card", max_results=2)

        self.assertEqual(len(results), 2)

    def test_find_news_reuses_a_passed_in_browser_without_launching_its_own(self):
        # market_alerts.py checks ~20-30 movers a night — sharing one
        # BrowserSession across all of them avoids paying browser-startup
        # cost per card.
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
        xml = _fake_rss([("Recent Article", "https://news.google.com/a", recent, "Site")])
        page = MagicMock()
        page.url = "https://example.com/real-article"
        page.inner_text.return_value = "Some Card is mentioned here"
        shared_browser = MagicMock()
        shared_browser.new_page.return_value = page

        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright") as mock_sync_playwright:
            results = mtg_news.find_news("Some Card", browser=shared_browser)

        self.assertEqual(len(results), 1)
        mock_sync_playwright.assert_not_called()  # never launched its own browser
        shared_browser.new_page.assert_called_once()

    def test_find_news_fails_soft_on_network_error(self):
        import mtg_news

        with patch.object(mtg_news.requests, "get", side_effect=mtg_news.requests.RequestException("boom")):
            self.assertEqual(mtg_news.find_news("Some Card"), [])

    def test_find_news_skips_basic_lands_without_any_network_call(self):
        # Body-text matching gives no real discriminative power for a bare
        # word like "Island" — confirmed live, it matched a Red Dead
        # Redemption article and a Hawaiian powwow story. Skip entirely
        # rather than risk a confident-looking wrong link.
        import mtg_news

        with patch.object(mtg_news.requests, "get") as mock_get, \
             patch.object(mtg_news, "sync_playwright") as mock_pw:
            for name in ("Island", "mountain", " Forest "):
                self.assertEqual(mtg_news.find_news(name), [])
        mock_get.assert_not_called()
        mock_pw.assert_not_called()

    def test_find_news_gives_up_if_redirect_never_resolves(self):
        # Observed live: Google's own interstitial page can render a
        # preview of the target headline before its JS redirect fires —
        # if the page is still on news.google.com after waiting, treat it
        # as unresolved rather than risk matching against Google's own
        # page instead of the real article.
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
        xml = _fake_rss([("Some Headline", "https://news.google.com/a", recent, "Site")])
        page = MagicMock()
        page.url = "https://news.google.com/rss/articles/still-here"  # never leaves Google's domain
        fake_instance = _fake_sync_playwright([page])

        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright", return_value=fake_instance), \
             patch.object(mtg_news.time, "monotonic", side_effect=[0, 100]):  # deadline elapses on first check
            results = mtg_news.find_news("Some Card")

        self.assertEqual(results, [])
        page.inner_text.assert_not_called()  # never trusted Google's own page as the article body

    def test_find_news_fails_soft_if_playwright_itself_is_unavailable(self):
        import mtg_news

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=5))
        xml = _fake_rss([("Recent Article", "https://news.google.com/a", recent, "Site")])
        with patch.object(mtg_news.requests, "get", return_value=self._fake_response(xml)), \
             patch.object(mtg_news, "sync_playwright", side_effect=RuntimeError("browser not installed")):
            self.assertEqual(mtg_news.find_news("Some Card"), [])

    def test_news_lines_formats_and_escapes_html(self):
        import market_alerts

        items = [{"title": "Price Spike & Reprint News", "link": "https://x.com/a?b=1&c=2", "source": "Site <A>"}]
        rendered = market_alerts._news_lines(items)
        self.assertIn("Price Spike &amp; Reprint News", rendered)
        self.assertIn("https://x.com/a?b=1&amp;c=2", rendered)
        self.assertIn("Site &lt;A&gt;", rendered)
        self.assertNotIn("<A>", rendered)

    def test_news_lines_empty_for_no_items(self):
        import market_alerts

        self.assertEqual(market_alerts._news_lines([]), "")


class MarketAlertsTests(unittest.TestCase):
    def setUp(self):
        import market_alerts

        self.market_alerts = market_alerts
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.movers_file = Path(self.tmp_dir.name) / "movers.json"
        self.movers_file.write_text(json.dumps({"daily_gainers": [
            {"name": "Mover One", "set": "TST", "set_full_name": "Test Set",
             "price_before": 1.0, "price_now": 2.0, "pct_change": 100.0, "image_url": None},
        ], "daily_losers": [], "weekly_gainers": [], "weekly_losers": []}))
        self._base_patches = [
            patch.object(market_alerts, "MOVERS_FILE", self.movers_file),
            patch.object(market_alerts.db, "already_ran_today", return_value=False),
            patch.object(market_alerts.db, "mark_ran_today"),
            patch.object(market_alerts.db, "record_recommendation", return_value=1),
            patch.object(market_alerts.db, "set_recommendation_telegram_info"),
            patch.object(market_alerts.recommender, "train", return_value=None),
            patch.object(market_alerts.recommender, "score", return_value=None),
            patch.object(market_alerts.telegram_notify, "send_photo_with_buttons", return_value=None),
        ]
        for p in self._base_patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_send_market_alerts_passes_shared_browser_to_news_lookups(self):
        fake_session = MagicMock()
        fake_browser = MagicMock()
        fake_session.__enter__.return_value = fake_browser
        fake_session.__exit__.return_value = False

        with patch.object(self.market_alerts.mtg_news, "BrowserSession", return_value=fake_session), \
             patch.object(self.market_alerts.mtg_news, "find_news", return_value=[]) as mock_find_news:
            self.market_alerts.send_market_alerts()

        mock_find_news.assert_called_once_with("Mover One", browser=fake_browser)
        fake_session.__exit__.assert_called_once()

    def test_send_market_alerts_closes_browser_session_even_if_sending_fails(self):
        fake_session = MagicMock()
        fake_browser = MagicMock()
        fake_session.__enter__.return_value = fake_browser
        fake_session.__exit__.return_value = False

        with patch.object(self.market_alerts.mtg_news, "BrowserSession", return_value=fake_session), \
             patch.object(self.market_alerts.mtg_news, "find_news", return_value=[]), \
             patch.object(
                 self.market_alerts.telegram_notify, "send_photo_with_buttons", side_effect=RuntimeError("boom")
             ):
            with self.assertRaises(RuntimeError):
                self.market_alerts.send_market_alerts()

        fake_session.__exit__.assert_called_once()

    def test_send_market_alerts_falls_back_to_no_browser_if_session_fails_to_start(self):
        with patch.object(self.market_alerts.mtg_news, "BrowserSession", side_effect=RuntimeError("no chromium")), \
             patch.object(self.market_alerts.mtg_news, "find_news", return_value=[]) as mock_find_news:
            self.market_alerts.send_market_alerts()  # must not raise

        mock_find_news.assert_called_once_with("Mover One", browser=None)


class ImportsTests(unittest.TestCase):
    """Every module should at least import cleanly — catches syntax errors
    and top-level exceptions before they reach the nightly job."""

    def test_all_modules_import(self):
        import all_cards_lookup  # noqa: F401
        import cardkingdom  # noqa: F401
        import claude_client  # noqa: F401
        import market_alerts  # noqa: F401
        import mtg_news  # noqa: F401
        import mtg_news_feed  # noqa: F401
        import mtgjson_crosswalk  # noqa: F401
        import news_signals  # noqa: F401
        import record_feedback  # noqa: F401
        import refresh_job  # noqa: F401
        import scryfall  # noqa: F401
        import tcgmarketplace  # noqa: F401
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

    def test_history_merges_all_cards_backfill_without_writing_to_db(self):
        import app as flask_app_module

        item = db.add_to_watchlist(
            {
                "variant_id": "history-merge-test:nonfoil",
                "card_id": "history-merge-test",
                "game": "Magic: The Gathering",
                "name": "History Merge Card",
                "set_name": "Test Set",
                "condition": "Near Mint",
                "printing": "Normal",
                "owner": "HistoryMergeTest",
                "mtgjson_id": "fake-mtgjson-uuid",
                "price": 5.00,  # seeds one local point "today"
            }
        )
        local_date = db.get_history("history-merge-test:nonfoil", kind="market")[0]["recorded_at"][:10]

        backfill_history = [
            {"date": "2026-01-01", "price": 1.00},
            {"date": "2026-01-02", "price": 1.50},
            {"date": local_date, "price": 999.00},  # same day as the local point — local must win
        ]

        try:
            with patch.object(
                flask_app_module.all_cards_lookup,
                "get_by_uuid",
                return_value={"name": "History Merge Card", "set": "tst", "history": backfill_history},
            ):
                resp = self.client.get(f"/api/watchlist/{item['id']}/history")
            self.assertEqual(resp.status_code, 200)
            history = resp.get_json()

            self.assertEqual([h["price"] for h in history], [1.00, 1.50, 5.00])  # sorted, local wins the overlap
            self.assertEqual(len(history), 3)

            # The merge must be response-only — nothing gets written back
            # into tcg_prices.db (that's the whole point of doing this at
            # read time instead of backfilling the file).
            raw_local_history = db.get_history("history-merge-test:nonfoil", kind="market")
            self.assertEqual(len(raw_local_history), 1)
        finally:
            db.remove_from_watchlist(item["id"])


if __name__ == "__main__":
    unittest.main()
