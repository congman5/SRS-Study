#!/usr/bin/env python3
"""
Tests for the concept-level memory system.

Run:  python -m pytest test_concepts.py -v
  or: python test_concepts.py

Covers:
  - Migration / bootstrap invariants
  - Concept curve helpers (update_concept_curve_rated, compute_concept_retention)
  - Session saving with concept updates
  - Undo / restore from snapshots
  - Export / import with concept data and ID remapping
  - Card CRUD with concept lifecycle
  - Concept-driven scheduling
"""

import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# Ensure the app module can be imported from the same directory
sys.path.insert(0, str(Path(__file__).parent))

# Override DB_PATH before importing app so tests use a temp database
_test_db_dir = tempfile.mkdtemp()
_test_db_counter = 0

import app as study_app

study_app.app.config["TESTING"] = True


def fresh_db():
    """Re-create a clean test database using a fresh temp file each time.

    This avoids Windows file-locking issues where SQLite connections that
    exited a ``with`` block (commit but no close) still hold the file open.
    """
    global _test_db_counter
    _test_db_counter += 1
    db_path = os.path.join(_test_db_dir, f"test_{_test_db_counter}.db")
    study_app.DB_PATH = Path(db_path)
    study_app.init_db()


class TestCurveHelpers(unittest.TestCase):
    """Test pure functions that don't need a database."""

    def test_update_concept_curve_complete(self):
        a, k = study_app.update_concept_curve_rated(
            study_app.A_INIT, study_app.K_INIT, "complete"
        )
        self.assertGreater(a, study_app.A_INIT)
        self.assertLess(k, study_app.K_INIT)

    def test_update_concept_curve_failed(self):
        a, k = study_app.update_concept_curve_rated(
            study_app.A_INIT, study_app.K_INIT, "failed"
        )
        self.assertEqual(a, study_app.A_INIT)  # a unchanged on failure
        self.assertGreaterEqual(k, study_app.K_INIT)  # k stays or increases

    def test_update_concept_curve_partial(self):
        a, k = study_app.update_concept_curve_rated(
            study_app.A_INIT, study_app.K_INIT, "partial"
        )
        a_c, k_c = study_app.update_concept_curve_rated(
            study_app.A_INIT, study_app.K_INIT, "complete"
        )
        # Partial should improve less than complete
        self.assertGreater(a, study_app.A_INIT)
        self.assertLess(a, a_c)

    def test_weight_scales_effect(self):
        a_full, k_full = study_app.update_concept_curve_rated(
            study_app.A_INIT, study_app.K_INIT, "complete", weight=1.0
        )
        a_half, k_half = study_app.update_concept_curve_rated(
            study_app.A_INIT, study_app.K_INIT, "complete", weight=0.5
        )
        # Half weight should produce smaller improvement
        self.assertLess(a_half, a_full)

    def test_global_factor_modulates(self):
        a_high, _ = study_app.update_concept_curve_rated(
            study_app.A_INIT, study_app.K_INIT, "complete", global_factor=1.15
        )
        a_low, _ = study_app.update_concept_curve_rated(
            study_app.A_INIT, study_app.K_INIT, "complete", global_factor=0.85
        )
        self.assertGreater(a_high, a_low)

    def test_compute_concept_retention_none_date(self):
        r = study_app.compute_concept_retention(
            study_app.A_INIT, study_app.K_INIT, None
        )
        # With no review date, retention falls to asymptote a (unreviewed)
        self.assertAlmostEqual(r, study_app.A_INIT, places=2)

    def test_compute_concept_retention_past_date(self):
        past = (date.today() - timedelta(days=5)).isoformat()
        r = study_app.compute_concept_retention(
            study_app.A_INIT, study_app.K_INIT, past
        )
        self.assertLess(r, 1.0)
        self.assertGreater(r, 0.0)


class TestMigrationBootstrap(unittest.TestCase):
    """Test that migration creates fallback concepts for existing cards."""

    def setUp(self):
        fresh_db()

    def test_new_db_has_concept_tables(self):
        with study_app.get_db() as db:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        for t in ("concepts", "card_concepts", "concept_states",
                   "concept_session_snapshots"):
            self.assertIn(t, tables)

    def test_bootstrap_creates_fallback_for_orphan_cards(self):
        with study_app.get_db() as db:
            # Create a topic and card manually (bypassing add_card)
            nr = study_app.next_review_date(study_app.A_INIT, study_app.K_INIT)
            db.execute(
                "INSERT INTO topics (name, learned_date, next_review) VALUES (?,?,?)",
                ("Test Topic", date.today().isoformat(), nr),
            )
            tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                "INSERT INTO cards (topic_id, question, answer) VALUES (?,?,?)",
                (tid, "Q1", "A1"),
            )
            cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]

            # No concept mapping yet
            self.assertEqual(len(study_app.get_card_concepts(db, cid)), 0)

            # Run bootstrap
            study_app._bootstrap_fallback_concepts(db)

            # Now should have a mapping
            concepts = study_app.get_card_concepts(db, cid)
            self.assertEqual(len(concepts), 1)
            self.assertEqual(concepts[0]["weight"], 1.0)

            # Concept state should exist
            state = db.execute(
                "SELECT * FROM concept_states WHERE concept_id=?",
                (concepts[0]["concept_id"],),
            ).fetchone()
            self.assertIsNotNone(state)

    def test_bootstrap_is_idempotent(self):
        with study_app.get_db() as db:
            nr = study_app.next_review_date(study_app.A_INIT, study_app.K_INIT)
            db.execute(
                "INSERT INTO topics (name, learned_date, next_review) VALUES (?,?,?)",
                ("T2", date.today().isoformat(), nr),
            )
            tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                "INSERT INTO cards (topic_id, question, answer) VALUES (?,?,?)",
                (tid, "Q", "A"),
            )
            study_app._bootstrap_fallback_concepts(db)
            count1 = db.execute("SELECT COUNT(*) as c FROM concepts").fetchone()["c"]
            study_app._bootstrap_fallback_concepts(db)
            count2 = db.execute("SELECT COUNT(*) as c FROM concepts").fetchone()["c"]
            self.assertEqual(count1, count2)

    def test_bootstrap_deterministic_order(self):
        """Cards should be processed in id order."""
        with study_app.get_db() as db:
            nr = study_app.next_review_date(study_app.A_INIT, study_app.K_INIT)
            db.execute(
                "INSERT INTO topics (name, learned_date, next_review) VALUES (?,?,?)",
                ("T3", date.today().isoformat(), nr),
            )
            tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for i in range(5):
                db.execute(
                    "INSERT INTO cards (topic_id, question, answer) VALUES (?,?,?)",
                    (tid, f"Q{i}", f"A{i}"),
                )
            study_app._bootstrap_fallback_concepts(db)
            concepts = db.execute(
                "SELECT name FROM concepts WHERE topic_id=? ORDER BY id", (tid,)
            ).fetchall()
            names = [c["name"] for c in concepts]
            # Should be card_<id> in ascending order
            for i in range(len(names) - 1):
                self.assertLess(names[i], names[i + 1])


class TestSessionSaveAndUndo(unittest.TestCase):
    """Test save_session with concept updates and undo with snapshots."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        # Create a topic with cards
        self.client.post("/api/topics", json={"name": "Math"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics WHERE name='Math'").fetchone()["id"]
        # Add cards
        for q in ["Q1", "Q2", "Q3"]:
            self.client.post(f"/api/topics/{self.tid}/cards",
                             json={"question": q, "answer": f"A-{q}"})
        with study_app.get_db() as db:
            self.card_ids = [r["id"] for r in
                             db.execute("SELECT id FROM cards WHERE topic_id=? ORDER BY id",
                                        (self.tid,)).fetchall()]

    def test_save_session_creates_concept_snapshots(self):
        ratings = [
            {"card_id": self.card_ids[0], "rating": "complete"},
            {"card_id": self.card_ids[1], "rating": "partial"},
            {"card_id": self.card_ids[2], "rating": "failed"},
        ]
        resp = self.client.post(f"/api/topics/{self.tid}/session",
                                json={"card_ratings": ratings})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("concept_summary", data)

        # Snapshots should exist
        with study_app.get_db() as db:
            snaps = db.execute(
                "SELECT * FROM concept_session_snapshots WHERE topic_id=?",
                (self.tid,),
            ).fetchall()
            self.assertEqual(len(snaps), 3)  # one per card×concept

    def test_save_session_updates_concept_states(self):
        ratings = [
            {"card_id": self.card_ids[0], "rating": "complete"},
            {"card_id": self.card_ids[1], "rating": "complete"},
        ]
        self.client.post(f"/api/topics/{self.tid}/session",
                         json={"card_ratings": ratings})
        with study_app.get_db() as db:
            for cid in self.card_ids[:2]:
                concepts = study_app.get_card_concepts(db, cid)
                state = db.execute(
                    "SELECT * FROM concept_states WHERE concept_id=?",
                    (concepts[0]["concept_id"],),
                ).fetchone()
                self.assertGreater(state["a"], study_app.A_INIT)
                self.assertEqual(state["review_count"], 1)

    def test_undo_restores_concept_state_exactly(self):
        with study_app.get_db() as db:
            # Capture pre-session concept states
            pre_states = {}
            for cid in self.card_ids:
                concepts = study_app.get_card_concepts(db, cid)
                for c in concepts:
                    state = db.execute(
                        "SELECT a, k, review_count, success_count, failure_count "
                        "FROM concept_states WHERE concept_id=?",
                        (c["concept_id"],),
                    ).fetchone()
                    pre_states[c["concept_id"]] = dict(state)

        # Do a session
        ratings = [
            {"card_id": self.card_ids[0], "rating": "complete"},
            {"card_id": self.card_ids[1], "rating": "failed"},
            {"card_id": self.card_ids[2], "rating": "partial"},
        ]
        self.client.post(f"/api/topics/{self.tid}/session",
                         json={"card_ratings": ratings})

        # Undo
        resp = self.client.post(f"/api/topics/{self.tid}/undo-review")
        self.assertEqual(resp.status_code, 200)

        # Verify concept states are exactly restored
        with study_app.get_db() as db:
            for concept_id, pre in pre_states.items():
                state = db.execute(
                    "SELECT a, k, review_count, success_count, failure_count "
                    "FROM concept_states WHERE concept_id=?",
                    (concept_id,),
                ).fetchone()
                self.assertAlmostEqual(state["a"], pre["a"], places=6,
                                       msg=f"concept {concept_id} a mismatch")
                self.assertAlmostEqual(state["k"], pre["k"], places=6,
                                       msg=f"concept {concept_id} k mismatch")
                self.assertEqual(state["review_count"], pre["review_count"])
                self.assertEqual(state["success_count"], pre["success_count"])
                self.assertEqual(state["failure_count"], pre["failure_count"])

    def test_undo_cleans_up_snapshots(self):
        ratings = [{"card_id": self.card_ids[0], "rating": "complete"}]
        self.client.post(f"/api/topics/{self.tid}/session",
                         json={"card_ratings": ratings})
        self.client.post(f"/api/topics/{self.tid}/undo-review")
        with study_app.get_db() as db:
            snaps = db.execute(
                "SELECT * FROM concept_session_snapshots WHERE topic_id=?",
                (self.tid,),
            ).fetchall()
            self.assertEqual(len(snaps), 0)

    def test_topic_next_review_derived_from_concepts(self):
        ratings = [
            {"card_id": self.card_ids[0], "rating": "complete"},
            {"card_id": self.card_ids[1], "rating": "complete"},
            {"card_id": self.card_ids[2], "rating": "complete"},
        ]
        resp = self.client.post(f"/api/topics/{self.tid}/session",
                                json={"card_ratings": ratings})
        data = resp.get_json()
        # next_review should come from concept schedule
        with study_app.get_db() as db:
            cdata = study_app.compute_topic_schedule_from_concepts(db, self.tid)
        self.assertEqual(data["next_review"], cdata["next_review"])


class TestExportImport(unittest.TestCase):
    """Test export includes concept data and import remaps IDs."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()

    def test_export_v3_includes_concepts(self):
        self.client.post("/api/topics", json={"name": "Physics"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics WHERE name='Physics'").fetchone()["id"]
        self.client.post(f"/api/topics/{tid}/cards",
                         json={"question": "F=ma?", "answer": "Yes"})
        resp = self.client.get("/api/export")
        data = resp.get_json()
        self.assertEqual(data["version"], 3)
        self.assertIn("concepts", data)
        self.assertIn("card_concepts", data)
        self.assertIn("concept_states", data)
        self.assertGreater(len(data["concepts"]), 0)

    def test_import_v3_with_concept_remapping(self):
        # Create source data
        self.client.post("/api/topics", json={"name": "Bio"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics WHERE name='Bio'").fetchone()["id"]
        self.client.post(f"/api/topics/{tid}/cards",
                         json={"question": "DNA?", "answer": "Yes"})
        export_resp = self.client.get("/api/export")
        export_data = export_resp.get_json()

        # Fresh DB
        fresh_db()
        self.client = study_app.app.test_client()

        # Import
        resp = self.client.post("/api/import", json=export_data,
                                content_type="application/json")
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["imported_topics"], 1)
        self.assertEqual(data["imported_cards"], 1)
        self.assertGreater(data["imported_concepts"], 0)

        # Verify concepts exist in new DB
        with study_app.get_db() as db:
            concepts = db.execute("SELECT * FROM concepts").fetchall()
            self.assertGreater(len(concepts), 0)
            states = db.execute("SELECT * FROM concept_states").fetchall()
            self.assertGreater(len(states), 0)

    def test_import_v2_bootstraps_concepts(self):
        """Legacy v2 imports should get fallback concepts via bootstrap."""
        v2_data = {
            "version": 2,
            "exported": date.today().isoformat(),
            "topics": [{
                "id": 99, "name": "Chem", "learned_date": date.today().isoformat(),
                "a": 0.2, "k": 0.3, "last_review": None,
                "next_review": date.today().isoformat(), "review_count": 0,
                "history": [], "tags": "",
            }],
            "cards": [{
                "id": 1, "topic_id": 99, "card_type": "qa",
                "question": "H2O?", "answer": "Water",
                "wrong_options": [], "box": 1,
                "fail_count": 0, "success_count": 0, "last_rating": "",
            }],
            "session_logs": [],
            "problems": [],
            "problem_attempts": [],
        }
        resp = self.client.post("/api/import", json=v2_data,
                                content_type="application/json")
        data = resp.get_json()
        self.assertTrue(data["ok"])
        # Concepts should have been bootstrapped
        with study_app.get_db() as db:
            concepts = db.execute("SELECT * FROM concepts").fetchall()
            self.assertGreater(len(concepts), 0)


class TestCardCRUDConceptLifecycle(unittest.TestCase):
    """Test that add_card / delete_card manage concept lifecycle."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        self.client.post("/api/topics", json={"name": "Eng"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics").fetchone()["id"]

    def test_add_card_creates_fallback_concept(self):
        resp = self.client.post(f"/api/topics/{self.tid}/cards",
                                json={"question": "Hello?", "answer": "World"})
        cid = resp.get_json()["id"]
        with study_app.get_db() as db:
            concepts = study_app.get_card_concepts(db, cid)
            self.assertEqual(len(concepts), 1)

    def test_delete_card_removes_orphan_concept(self):
        resp = self.client.post(f"/api/topics/{self.tid}/cards",
                                json={"question": "Bye?", "answer": "Bye"})
        cid = resp.get_json()["id"]
        with study_app.get_db() as db:
            concepts = study_app.get_card_concepts(db, cid)
            concept_id = concepts[0]["concept_id"]

        self.client.delete(f"/api/cards/{cid}")

        with study_app.get_db() as db:
            concept = db.execute(
                "SELECT * FROM concepts WHERE id=?", (concept_id,)
            ).fetchone()
            self.assertIsNone(concept)  # orphan cleaned up


class TestConceptScheduling(unittest.TestCase):
    """Test concept-driven topic scheduling."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()

    def test_compute_topic_schedule_empty(self):
        self.client.post("/api/topics", json={"name": "Empty"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics").fetchone()["id"]
            result = study_app.compute_topic_schedule_from_concepts(db, tid)
        self.assertEqual(result["total_concepts"], 0)
        self.assertIsNone(result["next_review"])

    def test_concept_priority_ordering(self):
        self.client.post("/api/topics", json={"name": "Prio"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        # Add cards
        for q in ["Q1", "Q2"]:
            self.client.post(f"/api/topics/{tid}/cards",
                             json={"question": q, "answer": f"A-{q}"})
        with study_app.get_db() as db:
            priorities = study_app.get_concept_priority(db, tid)
        self.assertEqual(len(priorities), 2)
        # Should be sorted by priority_score ascending
        self.assertLessEqual(priorities[0]["priority_score"],
                             priorities[1]["priority_score"])

    def test_select_cards_returns_valid_ids(self):
        self.client.post("/api/topics", json={"name": "Sel"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        for i in range(10):
            self.client.post(f"/api/topics/{tid}/cards",
                             json={"question": f"Q{i}", "answer": f"A{i}"})
        with study_app.get_db() as db:
            selected = study_app.select_cards_for_session(db, tid, limit=5)
        self.assertEqual(len(selected), 5)
        # All should be valid card IDs
        with study_app.get_db() as db:
            for cid in selected:
                card = db.execute("SELECT id FROM cards WHERE id=?", (cid,)).fetchone()
                self.assertIsNotNone(card)


class TestConceptEndpoints(unittest.TestCase):
    """Test the concept CRUD API endpoints."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        self.client.post("/api/topics", json={"name": "API Test"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        resp = self.client.post(f"/api/topics/{self.tid}/cards",
                                json={"question": "Test?", "answer": "Yes"})
        self.card_id = resp.get_json()["id"]

    def test_get_topic_concepts(self):
        resp = self.client.get(f"/api/topics/{self.tid}/concepts")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreater(len(data), 0)
        self.assertIn("concept_id", data[0])
        self.assertIn("retention", data[0])

    def test_link_and_unlink_concept(self):
        # Link a new concept
        resp = self.client.post(f"/api/cards/{self.card_id}/concepts",
                                json={"concept_name": "Newton's Laws",
                                      "weight": 0.8})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        concept_id = data["concept_id"]

        # Verify linked
        resp = self.client.get(f"/api/cards/{self.card_id}/concepts")
        names = [c["name"] for c in resp.get_json()]
        self.assertIn("Newton's Laws", names)

        # Unlink
        resp = self.client.delete(
            f"/api/cards/{self.card_id}/concepts/{concept_id}")
        self.assertEqual(resp.status_code, 200)

    def test_session_cards_endpoint(self):
        # Add more cards
        for q in ["Q2", "Q3", "Q4"]:
            self.client.post(f"/api/topics/{self.tid}/cards",
                             json={"question": q, "answer": "A"})
        resp = self.client.get(f"/api/topics/{self.tid}/session-cards?limit=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data), 2)

    def test_update_concept(self):
        resp = self.client.get(f"/api/topics/{self.tid}/concepts")
        concept_id = resp.get_json()[0]["concept_id"]
        resp = self.client.put(f"/api/concepts/{concept_id}",
                               json={"name": "Renamed Concept",
                                     "description": "A test"})
        self.assertEqual(resp.status_code, 200)


class TestListTopicsConceptData(unittest.TestCase):
    """Test that list_topics returns concept-derived metrics."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()

    def test_list_topics_includes_concept_fields(self):
        self.client.post("/api/topics", json={"name": "ConceptTopic"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        self.client.post(f"/api/topics/{tid}/cards",
                         json={"question": "Q", "answer": "A"})
        resp = self.client.get("/api/topics")
        topics = resp.get_json()
        self.assertGreater(len(topics), 0)
        t = topics[0]
        self.assertIn("concept_avg_retention", t)
        self.assertIn("concept_min_retention", t)
        self.assertIn("concept_due_count", t)
        self.assertIn("concept_total", t)
        self.assertIn("weakest_concept_name", t)
        self.assertIn("topic_retention", t)  # legacy field


class TestForeignKeyEnforcement(unittest.TestCase):
    """Verify FK enforcement is active."""

    def setUp(self):
        fresh_db()

    def test_fk_pragma_on(self):
        with study_app.get_db() as db:
            result = db.execute("PRAGMA foreign_keys").fetchone()
            self.assertEqual(result[0], 1)


# ── Audit-round tests ────────────────────────────────────────────────────


class TestBulkImportCreatesConcepts(unittest.TestCase):
    """Priority 1: bulk_import_cards() must create fallback concepts."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        self.client.post("/api/topics", json={"name": "BulkTopic"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics").fetchone()["id"]

    def test_bulk_import_creates_concept_per_card(self):
        cards = [
            {"question": "BQ1", "answer": "BA1"},
            {"question": "BQ2", "answer": "BA2"},
            {"question": "BQ3", "answer": "BA3"},
        ]
        resp = self.client.post(
            f"/api/topics/{self.tid}/cards/bulk",
            json={"cards": cards},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["added"], 3)

        # Every card should have a concept mapping
        with study_app.get_db() as db:
            card_rows = db.execute(
                "SELECT id FROM cards WHERE topic_id=?", (self.tid,)
            ).fetchall()
            for card_row in card_rows:
                concepts = study_app.get_card_concepts(db, card_row["id"])
                self.assertGreaterEqual(
                    len(concepts), 1,
                    f"card {card_row['id']} has no concept mapping after bulk import",
                )
                # concept_state should also exist
                state = db.execute(
                    "SELECT * FROM concept_states WHERE concept_id=?",
                    (concepts[0]["concept_id"],),
                ).fetchone()
                self.assertIsNotNone(state)

    def test_bulk_import_concepts_integrity_clean(self):
        """After bulk import, verify_concept_integrity returns no issues."""
        cards = [{"question": f"Q{i}", "answer": f"A{i}"} for i in range(5)]
        self.client.post(f"/api/topics/{self.tid}/cards/bulk",
                         json={"cards": cards})
        with study_app.get_db() as db:
            issues = study_app.verify_concept_integrity(db)
            card_issues = [i for i in issues if i["type"] == "card_missing_concept"]
            self.assertEqual(len(card_issues), 0)


class TestSaveSessionConceptCorrectness(unittest.TestCase):
    """Verify save_session() updates concept states and derives topic next_review."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        self.client.post("/api/topics", json={"name": "SessionTopic"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        for q in ["S1", "S2", "S3"]:
            self.client.post(f"/api/topics/{self.tid}/cards",
                             json={"question": q, "answer": f"A-{q}"})
        with study_app.get_db() as db:
            self.card_ids = [r["id"] for r in
                             db.execute("SELECT id FROM cards WHERE topic_id=? ORDER BY id",
                                        (self.tid,)).fetchall()]

    def test_session_derives_topic_next_review_from_concepts(self):
        ratings = [{"card_id": cid, "rating": "complete"} for cid in self.card_ids]
        resp = self.client.post(f"/api/topics/{self.tid}/session",
                                json={"card_ratings": ratings})
        data = resp.get_json()
        self.assertTrue(data["ok"])

        # topic.next_review should match concept-derived schedule
        with study_app.get_db() as db:
            topic = db.execute("SELECT next_review FROM topics WHERE id=?",
                               (self.tid,)).fetchone()
            cdata = study_app.compute_topic_schedule_from_concepts(db, self.tid)
            self.assertEqual(topic["next_review"], cdata["next_review"])

    def test_session_updates_all_concept_states(self):
        ratings = [
            {"card_id": self.card_ids[0], "rating": "complete"},
            {"card_id": self.card_ids[1], "rating": "partial"},
            {"card_id": self.card_ids[2], "rating": "failed"},
        ]
        self.client.post(f"/api/topics/{self.tid}/session",
                         json={"card_ratings": ratings})

        with study_app.get_db() as db:
            for cid in self.card_ids:
                concepts = study_app.get_card_concepts(db, cid)
                for c in concepts:
                    state = db.execute(
                        "SELECT review_count FROM concept_states WHERE concept_id=?",
                        (c["concept_id"],),
                    ).fetchone()
                    self.assertEqual(state["review_count"], 1,
                                     f"concept for card {cid} not updated")


class TestUndoReviewExactRestore(unittest.TestCase):
    """Priority 2: undo_review() must restore concept state exactly from snapshots."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        self.client.post("/api/topics", json={"name": "UndoTopic"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        for q in ["U1", "U2", "U3"]:
            self.client.post(f"/api/topics/{self.tid}/cards",
                             json={"question": q, "answer": f"A-{q}"})
        with study_app.get_db() as db:
            self.card_ids = [r["id"] for r in
                             db.execute("SELECT id FROM cards WHERE topic_id=? ORDER BY id",
                                        (self.tid,)).fetchall()]

    def _capture_concept_states(self):
        """Capture all concept states for the topic."""
        states = {}
        with study_app.get_db() as db:
            for cid in self.card_ids:
                concepts = study_app.get_card_concepts(db, cid)
                for c in concepts:
                    row = db.execute(
                        "SELECT a, k, last_review, next_review, review_count, "
                        "success_count, failure_count, history "
                        "FROM concept_states WHERE concept_id=?",
                        (c["concept_id"],),
                    ).fetchone()
                    states[c["concept_id"]] = dict(row)
        return states

    def test_undo_restores_all_fields_exactly(self):
        pre = self._capture_concept_states()

        # Do a session with mixed ratings
        ratings = [
            {"card_id": self.card_ids[0], "rating": "complete"},
            {"card_id": self.card_ids[1], "rating": "failed"},
            {"card_id": self.card_ids[2], "rating": "partial"},
        ]
        self.client.post(f"/api/topics/{self.tid}/session",
                         json={"card_ratings": ratings})

        # Verify states changed
        post = self._capture_concept_states()
        for cid, pre_state in pre.items():
            self.assertNotEqual(post[cid]["review_count"], pre_state["review_count"])

        # Undo
        resp = self.client.post(f"/api/topics/{self.tid}/undo-review")
        self.assertEqual(resp.status_code, 200)

        # Verify exact restoration of all fields
        restored = self._capture_concept_states()
        for cid, pre_state in pre.items():
            r = restored[cid]
            self.assertAlmostEqual(r["a"], pre_state["a"], places=10,
                                   msg=f"concept {cid} a not restored")
            self.assertAlmostEqual(r["k"], pre_state["k"], places=10,
                                   msg=f"concept {cid} k not restored")
            self.assertEqual(r["last_review"], pre_state["last_review"],
                             msg=f"concept {cid} last_review not restored")
            self.assertEqual(r["next_review"], pre_state["next_review"],
                             msg=f"concept {cid} next_review not restored")
            self.assertEqual(r["review_count"], pre_state["review_count"],
                             msg=f"concept {cid} review_count not restored")
            self.assertEqual(r["success_count"], pre_state["success_count"],
                             msg=f"concept {cid} success_count not restored")
            self.assertEqual(r["failure_count"], pre_state["failure_count"],
                             msg=f"concept {cid} failure_count not restored")
            self.assertEqual(r["history"], pre_state["history"],
                             msg=f"concept {cid} history not restored")

    def test_undo_deletes_snapshots(self):
        ratings = [{"card_id": self.card_ids[0], "rating": "complete"}]
        self.client.post(f"/api/topics/{self.tid}/session",
                         json={"card_ratings": ratings})

        with study_app.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) as c FROM concept_session_snapshots WHERE topic_id=?",
                (self.tid,),
            ).fetchone()["c"]
            self.assertGreater(count, 0)

        self.client.post(f"/api/topics/{self.tid}/undo-review")

        with study_app.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) as c FROM concept_session_snapshots WHERE topic_id=?",
                (self.tid,),
            ).fetchone()["c"]
            self.assertEqual(count, 0)

    def test_undo_recomputes_topic_next_review(self):
        pre_topic = None
        with study_app.get_db() as db:
            pre_topic = db.execute(
                "SELECT next_review FROM topics WHERE id=?", (self.tid,)
            ).fetchone()["next_review"]

        ratings = [{"card_id": cid, "rating": "complete"} for cid in self.card_ids]
        self.client.post(f"/api/topics/{self.tid}/session",
                         json={"card_ratings": ratings})

        self.client.post(f"/api/topics/{self.tid}/undo-review")

        with study_app.get_db() as db:
            post_topic = db.execute(
                "SELECT next_review FROM topics WHERE id=?", (self.tid,)
            ).fetchone()["next_review"]
            # Should be restored to match concept-derived schedule
            cdata = study_app.compute_topic_schedule_from_concepts(db, self.tid)
            if cdata["next_review"]:
                self.assertEqual(post_topic, cdata["next_review"])


class TestDeleteCardCleansOrphanedConcepts(unittest.TestCase):
    """Verify delete_card cleans up orphaned concepts and snapshots."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        self.client.post("/api/topics", json={"name": "DelTopic"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics").fetchone()["id"]

    def test_delete_card_removes_orphan_concept_and_state(self):
        resp = self.client.post(f"/api/topics/{self.tid}/cards",
                                json={"question": "DelQ", "answer": "DelA"})
        cid = resp.get_json()["id"]

        with study_app.get_db() as db:
            concepts = study_app.get_card_concepts(db, cid)
            concept_id = concepts[0]["concept_id"]

        self.client.delete(f"/api/cards/{cid}")

        with study_app.get_db() as db:
            concept = db.execute("SELECT * FROM concepts WHERE id=?",
                                 (concept_id,)).fetchone()
            self.assertIsNone(concept, "orphan concept not cleaned up")
            state = db.execute("SELECT * FROM concept_states WHERE concept_id=?",
                               (concept_id,)).fetchone()
            self.assertIsNone(state, "orphan concept_state not cleaned up")

    def test_delete_card_keeps_shared_concept(self):
        # Create two cards and link them to the same concept
        resp1 = self.client.post(f"/api/topics/{self.tid}/cards",
                                 json={"question": "Q1", "answer": "A1"})
        resp2 = self.client.post(f"/api/topics/{self.tid}/cards",
                                 json={"question": "Q2", "answer": "A2"})
        cid1 = resp1.get_json()["id"]
        cid2 = resp2.get_json()["id"]

        with study_app.get_db() as db:
            concepts1 = study_app.get_card_concepts(db, cid1)
            concept_id = concepts1[0]["concept_id"]
            # Also link card2 to card1's concept
            db.execute("INSERT INTO card_concepts (card_id, concept_id, weight) VALUES (?,?,1.0)",
                       (cid2, concept_id))

        # Delete card1 — concept should survive because card2 still links to it
        self.client.delete(f"/api/cards/{cid1}")

        with study_app.get_db() as db:
            concept = db.execute("SELECT * FROM concepts WHERE id=?",
                                 (concept_id,)).fetchone()
            self.assertIsNotNone(concept, "shared concept should not be deleted")


class TestScheduleConceptDriven(unittest.TestCase):
    """Priority 3: /api/schedule must use concept-level curves for projection."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        self.client.post("/api/topics", json={"name": "SchedTopic"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        for q in ["SQ1", "SQ2"]:
            self.client.post(f"/api/topics/{self.tid}/cards",
                             json={"question": q, "answer": f"A-{q}"})

    def test_schedule_returns_reviews(self):
        resp = self.client.get("/api/schedule")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreater(len(data), 0)
        topic_sched = data[0]
        self.assertEqual(len(topic_sched["reviews"]), 8)
        self.assertIn("concept_due_count", topic_sched)
        self.assertIn("concept_total", topic_sched)

    def test_schedule_first_review_matches_concept_derived(self):
        """The first projected review date should match the concept-derived next_review."""
        resp = self.client.get("/api/schedule")
        data = resp.get_json()
        topic_sched = [t for t in data if t["id"] == self.tid][0]

        with study_app.get_db() as db:
            cdata = study_app.compute_topic_schedule_from_concepts(db, self.tid)
        if cdata["next_review"]:
            self.assertEqual(topic_sched["reviews"][0], cdata["next_review"])

    def test_schedule_dates_are_monotonically_increasing(self):
        resp = self.client.get("/api/schedule")
        data = resp.get_json()
        for topic_sched in data:
            reviews = topic_sched["reviews"]
            for i in range(len(reviews) - 1):
                self.assertLessEqual(reviews[i], reviews[i + 1],
                                     f"schedule not monotonic: {reviews}")


class TestMarkReviewedSafety(unittest.TestCase):
    """Priority 4: mark_reviewed() should not corrupt concept granularity."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        self.client.post("/api/topics", json={"name": "MarkTopic"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        for q in ["M1", "M2"]:
            self.client.post(f"/api/topics/{self.tid}/cards",
                             json={"question": q, "answer": f"A-{q}"})

    def test_mark_reviewed_returns_deprecated_flag(self):
        resp = self.client.post(f"/api/topics/{self.tid}/review",
                                json={"rating": "complete"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("deprecated"),
                        "mark_reviewed should return deprecated=True")
        self.assertIn("warning", data)

    def test_mark_reviewed_uses_reduced_global_factor(self):
        """Concept updates from mark_reviewed use reduced factor, so the
        curve moves less than a full save_session."""
        # Capture pre-state
        with study_app.get_db() as db:
            pre_states = {}
            rows = db.execute("""
                SELECT cs.concept_id, cs.a
                FROM concept_states cs
                JOIN concepts c ON c.id = cs.concept_id
                WHERE c.topic_id=?
            """, (self.tid,)).fetchall()
            for r in rows:
                pre_states[r["concept_id"]] = r["a"]

        self.client.post(f"/api/topics/{self.tid}/review",
                         json={"rating": "complete"})

        with study_app.get_db() as db:
            for concept_id, pre_a in pre_states.items():
                state = db.execute(
                    "SELECT a FROM concept_states WHERE concept_id=?",
                    (concept_id,),
                ).fetchone()
                delta = state["a"] - pre_a
                # With reduced global_factor=0.5, the a improvement should be
                # noticeably smaller than the full A_GAIN
                self.assertGreater(delta, 0, "a should still improve")
                full_delta = study_app.A_GAIN * (1.0 - pre_a)
                self.assertLess(delta, full_delta * 0.8,
                                "reduced factor should limit improvement")


class TestConsistencyChecks(unittest.TestCase):
    """Priority 6: verify_concept_integrity and repair_concept_integrity."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()

    def test_clean_db_has_no_issues(self):
        self.client.post("/api/topics", json={"name": "CleanTopic"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        self.client.post(f"/api/topics/{tid}/cards",
                         json={"question": "CQ", "answer": "CA"})
        with study_app.get_db() as db:
            issues = study_app.verify_concept_integrity(db)
        self.assertEqual(len(issues), 0)

    def test_detects_card_without_concept(self):
        self.client.post("/api/topics", json={"name": "Broken"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics").fetchone()["id"]
            # Insert card bypassing add_card (no concept created)
            db.execute("INSERT INTO cards (topic_id, question, answer) VALUES (?,?,?)",
                       (tid, "Orphan", "Card"))
            issues = study_app.verify_concept_integrity(db)
        card_issues = [i for i in issues if i["type"] == "card_missing_concept"]
        self.assertGreater(len(card_issues), 0)

    def test_repair_fixes_orphan_cards(self):
        self.client.post("/api/topics", json={"name": "Repair"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics").fetchone()["id"]
            db.execute("INSERT INTO cards (topic_id, question, answer) VALUES (?,?,?)",
                       (tid, "Fix", "Me"))
            result = study_app.repair_concept_integrity(db)
        self.assertGreater(result["repaired_cards"], 0)

        # Should be clean now
        with study_app.get_db() as db:
            issues = study_app.verify_concept_integrity(db)
        card_issues = [i for i in issues if i["type"] == "card_missing_concept"]
        self.assertEqual(len(card_issues), 0)

    def test_detects_concept_without_state(self):
        self.client.post("/api/topics", json={"name": "NoState"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics").fetchone()["id"]
            # Create concept without state
            db.execute("INSERT INTO concepts (topic_id, name) VALUES (?,?)",
                       (tid, "stateless_concept"))
            issues = study_app.verify_concept_integrity(db)
        state_issues = [i for i in issues if i["type"] == "concept_missing_state"]
        self.assertGreater(len(state_issues), 0)

    def test_repair_fixes_stateless_concepts(self):
        self.client.post("/api/topics", json={"name": "FixState"})
        with study_app.get_db() as db:
            tid = db.execute("SELECT id FROM topics").fetchone()["id"]
            db.execute("INSERT INTO concepts (topic_id, name) VALUES (?,?)",
                       (tid, "needs_state"))
            result = study_app.repair_concept_integrity(db)
        self.assertGreater(result["repaired_states"], 0)

        with study_app.get_db() as db:
            issues = study_app.verify_concept_integrity(db)
        state_issues = [i for i in issues if i["type"] == "concept_missing_state"]
        self.assertEqual(len(state_issues), 0)


class TestDeleteTopicCleansSnapshots(unittest.TestCase):
    """Verify delete_topic cleans up concept_session_snapshots."""

    def setUp(self):
        fresh_db()
        self.client = study_app.app.test_client()
        self.client.post("/api/topics", json={"name": "SnapTopic"})
        with study_app.get_db() as db:
            self.tid = db.execute("SELECT id FROM topics").fetchone()["id"]
        for q in ["T1", "T2"]:
            self.client.post(f"/api/topics/{self.tid}/cards",
                             json={"question": q, "answer": f"A-{q}"})
        with study_app.get_db() as db:
            self.card_ids = [r["id"] for r in
                             db.execute("SELECT id FROM cards WHERE topic_id=? ORDER BY id",
                                        (self.tid,)).fetchall()]

    def test_delete_topic_removes_snapshots(self):
        # Create a session so snapshots exist
        ratings = [{"card_id": cid, "rating": "complete"} for cid in self.card_ids]
        self.client.post(f"/api/topics/{self.tid}/session",
                         json={"card_ratings": ratings})

        with study_app.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) as c FROM concept_session_snapshots WHERE topic_id=?",
                (self.tid,),
            ).fetchone()["c"]
            self.assertGreater(count, 0, "snapshots should exist before delete")

        self.client.delete(f"/api/topics/{self.tid}")

        with study_app.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) as c FROM concept_session_snapshots WHERE topic_id=?",
                (self.tid,),
            ).fetchone()["c"]
            self.assertEqual(count, 0, "snapshots should be cleaned up after topic delete")


def tearDownModule():
    """Clean up temp database directory."""
    import shutil
    try:
        shutil.rmtree(_test_db_dir, ignore_errors=True)
    except OSError:
        pass


if __name__ == "__main__":
    unittest.main()
