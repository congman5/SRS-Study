#!/usr/bin/env python3
"""
Study Tracker — Spaced Repetition via the Ebbinghaus Forgetting Curve
======================================================================
Run:   python app.py
Open:  http://localhost:5000   (opens automatically)

Forgetting curve model:
  R(t) = a + (1−a)·e^(−k·t)     where t = days since last review

  • a  = long-term retained floor (rises after each review)
  • k  = forgetting rate          (falls   after each review)
  • Next review scheduled when R(t) drops to THRESHOLD (80%)

Parameters calibrated so the review schedule for a fresh topic
naturally lands at approximately Day 1 → 3 → 7 → 21, then keeps
extending as the memory consolidates.

Optional desktop notifications: pip install plyer
"""

import math
import json
import os
import sqlite3
import sys
import threading
import io
from datetime import date, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import webview

from flask import Flask, jsonify, render_template_string, request, send_file

try:
    import anthropic as _anthropic_mod
    _HAS_ANTHROPIC = True
except ImportError:
    _anthropic_mod = None
    _HAS_ANTHROPIC = False

ANTHROPIC_CLIENT = None

def _init_anthropic(api_key: str):
    """Create (or replace) the global Anthropic client with a new key."""
    global ANTHROPIC_CLIENT
    if not _HAS_ANTHROPIC or not api_key:
        ANTHROPIC_CLIENT = None
        return
    ANTHROPIC_CLIENT = _anthropic_mod.Anthropic(
        api_key=api_key,
        timeout=120.0,
    )

# Initialise from environment / .env on startup
_init_anthropic(os.environ.get("ANTHROPIC_API_KEY", ""))

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None

# ── Configuration ─────────────────────────────────────────────────────────
THRESHOLD = 0.80   # review when retention drops to 80%
A_INIT    = 0.20   # initial long-term floor  (Ebbinghaus calibration)
K_INIT    = 0.30   # initial forgetting rate  (gives ~1 day first review)
A_GAIN    = 0.12   # floor increase per review:  a ← a + 0.12·(1−a)
K_FACTOR  = 0.50   # rate multiplier per review: k ← k · 0.50
PORT      = 5000
DB_PATH   = Path(__file__).parent / "topics.db"

app = Flask(__name__)


# ── Forgetting Curve Math ─────────────────────────────────────────────────
# These functions operate on (a, k) curve parameters.  They are used for
# both topic-level (LEGACY / analytics only) and concept-level updates.
#
# TOPIC-LEVEL a/k STATUS:
#   topics.a, topics.k, update_curve_rated() applied at topic level are now
#   LEGACY — kept for backward compatibility, session summaries, and the
#   /api/stats/extended analytics endpoint.  They are still written on every
#   session but no longer solely determine topics.next_review.
#
#   Scheduling is now driven by concept_states via
#   compute_topic_schedule_from_concepts().
#
#   TODO(future): once all UIs consume concept-derived retention, topic-level
#   a/k can be removed from the update path (but kept in the schema for
#   historical reference).

def retention(a: float, k: float, days: float) -> float:
    """Current retention t days after a review (or learning)."""
    return a + (1.0 - a) * math.exp(-k * days)


def days_to_threshold(a: float, k: float) -> float:
    """Days from a fresh review (R=1.0) until R drops to THRESHOLD."""
    if a >= THRESHOLD:
        return 365.0          # floor above threshold — nearly permanent
    inner = (THRESHOLD - a) / (1.0 - a)
    if inner <= 0.0:
        return 365.0
    return max(0.5, -math.log(inner) / k)


def update_curve(a: float, k: float):
    """Return improved (a, k) after a completed review."""
    a_new = min(0.95, a + A_GAIN * (1.0 - a))
    k_new = max(0.005, k * K_FACTOR)
    return a_new, k_new


def next_review_date(a: float, k: float) -> str:
    days = days_to_threshold(a, k)
    return (date.today() + timedelta(days=round(days))).isoformat()


# ── Database ──────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")   # enforce FK constraints
    return conn


def update_curve_rated(a: float, k: float, rating: str):
    """Return improved (a, k) scaled by recall quality."""
    if rating == "failed":
        # No improvement; schedule a shorter re-review
        k_new = min(K_INIT, k * 1.3)  # decay faster → sooner re-review
        return a, k_new
    elif rating == "partial":
        a_new = min(0.95, a + (A_GAIN * 0.5) * (1.0 - a))
        k_new = max(0.005, k * (1.0 - (1.0 - K_FACTOR) * 0.5))  # 1/2 of complete
        return a_new, k_new
    else:  # "complete"
        return update_curve(a, k)


def init_db():
    with get_db() as db:
        # NOTE: topics.a and topics.k are LEGACY analytics fields.
        # Scheduling is now derived from concept_states.
        # They are still updated for backward compat and stats display.
        db.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                learned_date TEXT    NOT NULL,
                a            REAL    NOT NULL DEFAULT 0.20,  -- legacy analytics
                k            REAL    NOT NULL DEFAULT 0.30,  -- legacy analytics
                last_review  TEXT,
                next_review  TEXT    NOT NULL,  -- now derived from concept states
                review_count INTEGER NOT NULL DEFAULT 0,
                history      TEXT    NOT NULL DEFAULT '[]',
                tags         TEXT    NOT NULL DEFAULT ''
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                card_type     TEXT    NOT NULL DEFAULT 'qa',
                question      TEXT    NOT NULL,
                answer        TEXT    NOT NULL DEFAULT '',
                wrong_options TEXT    NOT NULL DEFAULT '[]',
                box           INTEGER NOT NULL DEFAULT 1,
                fail_count    INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                last_rating   TEXT    NOT NULL DEFAULT ''
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS session_logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id   INTEGER REFERENCES cards(id) ON DELETE CASCADE,
                topic_id  INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                rating    TEXT    NOT NULL,
                date      TEXT    NOT NULL,
                a_after   REAL,
                k_after   REAL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS review_days (
                date         TEXT PRIMARY KEY,
                topic_count  INTEGER NOT NULL DEFAULT 0,
                card_count   INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS ai_corrections (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                card_id       INTEGER REFERENCES cards(id) ON DELETE CASCADE,
                question      TEXT    NOT NULL,
                correct_answer TEXT   NOT NULL DEFAULT '',
                user_answer   TEXT    NOT NULL,
                ai_rating     TEXT    NOT NULL,
                user_override TEXT,
                explanation   TEXT    NOT NULL DEFAULT '',
                created_at    TEXT    NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS problems (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                title         TEXT    NOT NULL,
                prompt        TEXT    NOT NULL,
                hints         TEXT    NOT NULL DEFAULT '[]',
                final_answer  TEXT    NOT NULL DEFAULT '',
                full_solution TEXT    NOT NULL DEFAULT '',
                skill_tag     TEXT    NOT NULL DEFAULT '',
                difficulty    INTEGER NOT NULL DEFAULT 1,
                source        TEXT    NOT NULL DEFAULT 'generated',
                created_at    TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS problem_attempts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                problem_id    INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
                topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                user_answer   TEXT    NOT NULL DEFAULT '',
                rating        TEXT    NOT NULL DEFAULT '',
                ai_explanation TEXT   NOT NULL DEFAULT '',
                hints_used    INTEGER NOT NULL DEFAULT 0,
                solution_viewed INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL
            )
        """)

        # ── Concept-level memory tables (new) ─────────────────────────
        # Each card maps to one or more concepts; each concept tracks its
        # own forgetting-curve state independently of the topic aggregate.
        db.execute("""
            CREATE TABLE IF NOT EXISTS concepts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                name        TEXT    NOT NULL,
                description TEXT,
                UNIQUE(topic_id, name)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS card_concepts (
                card_id    INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
                weight     REAL    NOT NULL DEFAULT 1.0,
                PRIMARY KEY (card_id, concept_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS concept_states (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                concept_id    INTEGER NOT NULL UNIQUE REFERENCES concepts(id) ON DELETE CASCADE,
                a             REAL    NOT NULL DEFAULT 0.20,
                k             REAL    NOT NULL DEFAULT 0.30,
                last_review   TEXT,
                next_review   TEXT    NOT NULL,
                review_count  INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                history       TEXT    NOT NULL DEFAULT '[]'
            )
        """)

        # Snapshot table: stores pre-update concept state for each session
        # so undo_review can restore exactly rather than approximate reversal.
        db.execute("""
            CREATE TABLE IF NOT EXISTS concept_session_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                concept_id    INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
                topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
                session_date  TEXT    NOT NULL,
                a_before      REAL    NOT NULL,
                k_before      REAL    NOT NULL,
                last_review_before TEXT,
                next_review_before TEXT NOT NULL,
                review_count_before  INTEGER NOT NULL,
                success_count_before INTEGER NOT NULL,
                failure_count_before INTEGER NOT NULL,
                history_before TEXT NOT NULL DEFAULT '[]',
                rating        TEXT    NOT NULL,
                weight        REAL    NOT NULL DEFAULT 1.0,
                global_factor REAL    NOT NULL DEFAULT 1.0
            )
        """)

        # Migrate existing tables — add columns if missing
        _migrate(db)


def _migrate(db):
    """Add columns to existing tables without losing data."""
    existing = {r[1] for r in db.execute("PRAGMA table_info(cards)").fetchall()}
    for col, typedef in [("box", "INTEGER NOT NULL DEFAULT 1"),
                         ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
                         ("success_count", "INTEGER NOT NULL DEFAULT 0"),
                         ("last_rating", "TEXT NOT NULL DEFAULT ''"),
                         ("wrong_options", "TEXT NOT NULL DEFAULT '[]'")]:
        if col not in existing:
            db.execute(f"ALTER TABLE cards ADD COLUMN {col} {typedef}")

    existing_t = {r[1] for r in db.execute("PRAGMA table_info(topics)").fetchall()}
    if "tags" not in existing_t:
        db.execute("ALTER TABLE topics ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
    if "consecutive_reviews" not in existing_t:
        db.execute("ALTER TABLE topics ADD COLUMN consecutive_reviews INTEGER NOT NULL DEFAULT 0")

    # Migrate problems table
    existing_p = {r[1] for r in db.execute("PRAGMA table_info(problems)").fetchall()}
    if existing_p and "source" not in existing_p:
        db.execute("ALTER TABLE problems ADD COLUMN source TEXT NOT NULL DEFAULT 'generated'")

    # ── Bootstrap concept mappings for existing cards ─────────────
    # For every card that has no entry in card_concepts, create a
    # fallback 1-to-1 concept so the system is usable immediately.
    _bootstrap_fallback_concepts(db)

    # ── Backfill NULL last_review in concept_states ───────────────
    # Early versions stored NULL for unreviewed concepts, causing
    # retention to show 100%.  Set to the topic's learned_date so
    # retention decays naturally.
    db.execute("""
        UPDATE concept_states SET last_review = (
            SELECT t.learned_date FROM concepts c
            JOIN topics t ON t.id = c.topic_id
            WHERE c.id = concept_states.concept_id
        ) WHERE last_review IS NULL
    """)


def _bootstrap_fallback_concepts(db):
    """Create fallback concept for every card that lacks a concept mapping.

    Each orphaned card gets a concept named 'card_<id>' with its own
    concept_state initialised from the card's existing box/stats so the
    forgetting curve starts from a reasonable position rather than fresh.
    """
    orphan_cards = db.execute("""
        SELECT c.id, c.topic_id, c.box, c.success_count, c.fail_count,
               t.learned_date
        FROM cards c
        LEFT JOIN card_concepts cc ON cc.card_id = c.id
        JOIN topics t ON t.id = c.topic_id
        WHERE cc.card_id IS NULL
        ORDER BY c.id
    """).fetchall()
    if not orphan_cards:
        return

    today = date.today().isoformat()
    for card in orphan_cards:
        cid = card["id"]
        tid = card["topic_id"]
        concept_name = f"card_{cid}"

        # Ensure idempotent — concept may already exist (rare, e.g. partial migration)
        existing = db.execute(
            "SELECT id FROM concepts WHERE topic_id=? AND name=?", (tid, concept_name)
        ).fetchone()
        if existing:
            concept_id = existing["id"]
        else:
            db.execute(
                "INSERT INTO concepts (topic_id, name, description) VALUES (?,?,?)",
                (tid, concept_name, None),
            )
            concept_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Link card → concept
        db.execute(
            "INSERT OR IGNORE INTO card_concepts (card_id, concept_id, weight) VALUES (?,?,1.0)",
            (cid, concept_id),
        )

        # Initialise concept_state from card's existing stats.
        # Approximate a/k from card box level so stronger cards start stronger.
        box = card["box"] or 1
        # box 1 → fresh (A_INIT, K_INIT); box 5 → well-learned
        simulated_reviews = max(0, box - 1)
        a, k = A_INIT, K_INIT
        for _ in range(simulated_reviews):
            a, k = update_curve(a, k)
        nr = next_review_date(a, k)

        existing_state = db.execute(
            "SELECT id FROM concept_states WHERE concept_id=?", (concept_id,)
        ).fetchone()
        if not existing_state:
            db.execute(
                """INSERT INTO concept_states
                   (concept_id, a, k, last_review, next_review, review_count,
                    success_count, failure_count, history)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (concept_id, a, k, card["learned_date"], nr,
                 card["success_count"] + card["fail_count"],
                 card["success_count"], card["fail_count"], "[]"),
            )


# ── Concept-Level Forgetting Curve Helpers (new) ─────────────────────────
# These functions mirror the topic-level curve model but operate per-concept.
# They are used by save_session() and scheduling logic.  The topic-level
# functions (update_curve_rated, retention, etc.) are preserved for legacy
# compatibility and analytics.

def update_concept_curve_rated(a: float, k: float, rating: str,
                               weight: float = 1.0,
                               global_factor: float = 1.0) -> tuple:
    """Return updated (a, k) for a concept after a rating.

    Like update_curve_rated, but scaled by *weight* (from card_concepts, so
    multi-concept cards share their signal) and *global_factor* (a modest
    modulation from the whole-session score, typically 0.8–1.2).

    The weight and global_factor compress the magnitude of the update toward
    the identity (no change) rather than changing direction.
    """
    # Compute the "full-strength" update exactly like update_curve_rated
    if rating == "failed":
        a_full, k_full = a, min(K_INIT, k * 1.3)
    elif rating == "partial":
        a_full = min(0.95, a + (A_GAIN * 0.5) * (1.0 - a))
        k_full = max(0.005, k * (1.0 - (1.0 - K_FACTOR) * 0.5))
    else:  # "complete"
        a_full = min(0.95, a + A_GAIN * (1.0 - a))
        k_full = max(0.005, k * K_FACTOR)

    # Scale the *delta* by weight × global_factor so the effect is
    # proportional.  A weight of 0.5 means "this card only partially
    # tests this concept" → half the curve movement.
    scale = min(1.0, max(0.1, weight * global_factor))
    a_new = a + (a_full - a) * scale
    k_new = k + (k_full - k) * scale

    # Clamp to valid ranges
    a_new = min(0.95, max(A_INIT * 0.5, a_new))
    k_new = max(0.005, min(K_INIT * 2.0, k_new))
    return a_new, k_new


def compute_concept_retention(a: float, k: float, ref_date) -> float:
    """Compute current retention for a concept given its last review date.

    *ref_date* can be an ISO date string or None.  If None the concept has
    never been reviewed — treat as if retention has fully decayed to the
    asymptote *a* so unreviewed concepts don't falsely appear at 100%.
    """
    if ref_date is None:
        # No review date means the concept is essentially unreviewed.
        # Return the floor retention (asymptote a) rather than 1.0.
        return a
    else:
        days_elapsed = max(0.0, (date.today() - date.fromisoformat(ref_date)).days)
    return retention(a, k, days_elapsed)


def get_card_concepts(db, card_id: int) -> list:
    """Return list of dicts {concept_id, weight} for a card.

    If the card has no concept mappings the list is empty — callers should
    use get_or_create_fallback_concept_for_card() to lazy-create one.
    """
    rows = db.execute(
        "SELECT concept_id, weight FROM card_concepts WHERE card_id=?",
        (card_id,),
    ).fetchall()
    return [{"concept_id": r["concept_id"], "weight": r["weight"]} for r in rows]


def get_or_create_fallback_concept_for_card(db, card_id: int, topic_id: int) -> list:
    """Ensure a card has at least one concept mapping; return the mappings.

    If none exist, create a fallback concept named 'card_<id>' with a fresh
    concept_state and link it.  Returns list of {concept_id, weight}.
    """
    existing = get_card_concepts(db, card_id)
    if existing:
        return existing

    concept_name = f"card_{card_id}"
    row = db.execute(
        "SELECT id FROM concepts WHERE topic_id=? AND name=?",
        (topic_id, concept_name),
    ).fetchone()
    if row:
        concept_id = row["id"]
    else:
        db.execute(
            "INSERT INTO concepts (topic_id, name, description) VALUES (?,?,?)",
            (topic_id, concept_name, None),
        )
        concept_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    db.execute(
        "INSERT OR IGNORE INTO card_concepts (card_id, concept_id, weight) VALUES (?,?,1.0)",
        (card_id, concept_id),
    )

    # Create concept_state if missing
    if not db.execute("SELECT id FROM concept_states WHERE concept_id=?", (concept_id,)).fetchone():
        nr = next_review_date(A_INIT, K_INIT)
        # Use topic learned_date as initial last_review so retention decays
        # naturally from day one instead of showing 100% for unreviewed concepts.
        topic_row = db.execute(
            "SELECT learned_date FROM topics WHERE id=?", (topic_id,)
        ).fetchone()
        initial_lr = topic_row["learned_date"] if topic_row else date.today().isoformat()
        db.execute(
            """INSERT INTO concept_states
               (concept_id, a, k, last_review, next_review, review_count,
                success_count, failure_count, history)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (concept_id, A_INIT, K_INIT, initial_lr, nr, 0, 0, 0, "[]"),
        )

    return [{"concept_id": concept_id, "weight": 1.0}]


def update_concept_state(db, concept_id: int, rating: str, review_date: str,
                         weight: float = 1.0, global_factor: float = 1.0,
                         topic_id: int = None):
    """Apply a single rating to a concept's forgetting-curve state.

    Mirrors what save_session does at the topic level, but per-concept:
      1. Save a pre-update snapshot to concept_session_snapshots (for undo)
      2. Update a, k via update_concept_curve_rated
      3. Increment success/failure counters
      4. Append today to history (if not already present)
      5. Recompute next_review

    topic_id is required for snapshot recording; if None, the snapshot is
    skipped (used only during migration bootstrap).
    """
    state = db.execute(
        "SELECT * FROM concept_states WHERE concept_id=?", (concept_id,)
    ).fetchone()
    if not state:
        # Safety: create a fresh state (should not normally happen)
        nr = next_review_date(A_INIT, K_INIT)
        db.execute(
            """INSERT INTO concept_states
               (concept_id, a, k, last_review, next_review, review_count,
                success_count, failure_count, history)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (concept_id, A_INIT, K_INIT, None, nr, 0, 0, 0, "[]"),
        )
        state = db.execute(
            "SELECT * FROM concept_states WHERE concept_id=?", (concept_id,)
        ).fetchone()

    # --- Save pre-update snapshot for reliable undo ---
    if topic_id is not None:
        db.execute(
            """INSERT INTO concept_session_snapshots
               (concept_id, topic_id, session_date,
                a_before, k_before, last_review_before, next_review_before,
                review_count_before, success_count_before, failure_count_before,
                history_before, rating, weight, global_factor)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (concept_id, topic_id, review_date,
             state["a"], state["k"], state["last_review"], state["next_review"],
             state["review_count"], state["success_count"], state["failure_count"],
             state["history"], rating, weight, global_factor),
        )

    a_new, k_new = update_concept_curve_rated(
        state["a"], state["k"], rating, weight=weight, global_factor=global_factor
    )

    history = json.loads(state["history"])
    if review_date not in history:
        history.append(review_date)

    sc = state["success_count"]
    fc = state["failure_count"]
    if rating in ("complete", "partial"):
        sc += 1
    else:
        fc += 1

    # Compute next_review for this concept
    if rating == "complete":
        lr = review_date
        nr = next_review_date(a_new, k_new)
    else:
        # Partial/failed: keep old last_review so retention stays realistic;
        # schedule next review from today using the (worsened) curve.
        lr = state["last_review"]
        days_gap = round(days_to_threshold(a_new, k_new))
        nr = (date.today() + timedelta(days=max(1, days_gap))).isoformat()

    db.execute(
        """UPDATE concept_states
           SET a=?, k=?, last_review=?, next_review=?,
               review_count=review_count+1,
               success_count=?, failure_count=?, history=?
           WHERE concept_id=?""",
        (a_new, k_new, lr, nr, sc, fc, json.dumps(history), concept_id),
    )


def compute_topic_schedule_from_concepts(db, topic_id: int) -> dict:
    """Derive topic-level scheduling & stats from concept states.

    Returns dict with:
      next_review    — earliest concept next_review (topic becomes due when
                       its weakest concept is due)
      avg_retention  — mean retention across all concepts
      min_retention  — weakest concept retention
      weakest_concept_id / weakest_concept_name
      due_count      — number of concepts whose next_review ≤ today
      total_concepts
    """
    today = date.today().isoformat()
    rows = db.execute("""
        SELECT cs.*, c.name AS concept_name
        FROM concept_states cs
        JOIN concepts c ON c.id = cs.concept_id
        WHERE c.topic_id=?
    """, (topic_id,)).fetchall()

    if not rows:
        # No concepts — return defaults; topic keeps its own schedule
        return {
            "next_review": None,
            "avg_retention": None,
            "min_retention": None,
            "weakest_concept_id": None,
            "weakest_concept_name": None,
            "due_count": 0,
            "total_concepts": 0,
        }

    retentions = []
    weakest_r = 2.0
    weakest_id = None
    weakest_name = None
    earliest_nr = None
    due_count = 0

    for row in rows:
        r = compute_concept_retention(row["a"], row["k"], row["last_review"])
        retentions.append(r)
        if r < weakest_r:
            weakest_r = r
            weakest_id = row["concept_id"]
            weakest_name = row["concept_name"]
        nr = row["next_review"]
        if earliest_nr is None or nr < earliest_nr:
            earliest_nr = nr
        if nr <= today:
            due_count += 1

    avg_r = sum(retentions) / len(retentions) if retentions else 0.0

    return {
        "next_review": earliest_nr,
        "avg_retention": round(avg_r * 100, 1),
        "min_retention": round(weakest_r * 100, 1),
        "weakest_concept_id": weakest_id,
        "weakest_concept_name": weakest_name,
        "due_count": due_count,
        "total_concepts": len(rows),
    }


def get_concept_priority(db, topic_id: int) -> list:
    """Return concepts ordered by review priority (weakest first).

    Priority is based on predicted recall — low recall → high priority.
    Repeated failures also boost priority slightly.
    """
    rows = db.execute("""
        SELECT cs.*, c.name AS concept_name
        FROM concept_states cs
        JOIN concepts c ON c.id = cs.concept_id
        WHERE c.topic_id=?
    """, (topic_id,)).fetchall()

    scored = []
    for row in rows:
        r = compute_concept_retention(row["a"], row["k"], row["last_review"])
        # Lower retention → higher priority (lower score).
        # Bonus penalty for repeated failures.
        fail_penalty = min(0.15, row["failure_count"] * 0.03)
        priority_score = r - fail_penalty
        scored.append({
            "concept_id": row["concept_id"],
            "concept_name": row["concept_name"],
            "retention": round(r * 100, 1),
            "priority_score": round(priority_score, 4),
            "next_review": row["next_review"],
            "failure_count": row["failure_count"],
        })

    scored.sort(key=lambda x: x["priority_score"])
    return scored


def select_cards_for_session(db, topic_id: int, limit: int = None) -> list:
    """Select cards for a study session, weighted toward weak concepts.

    Selection strategy (deterministic enough to test):
      1. Include cards covering *overdue* concepts (next_review <= today) first
      2. Among weak concepts, pick one representative card per concept to
         avoid repeatedly drilling the same weak concept via multiple cards
      3. Fill remaining slots with 50% weak / 30% medium / 20% strong mix
      4. Guarantee at least one strong-but-aging card for maintenance
      5. Apply a mild recency penalty so cards from the most recent session
         are deprioritised (not excluded)

    Returns list of card IDs in shuffled order.
    """
    import random

    today = date.today().isoformat()

    # Get concept priorities
    priorities = get_concept_priority(db, topic_id)
    if not priorities:
        # Fallback: return all cards in default order
        rows = db.execute(
            "SELECT id FROM cards WHERE topic_id=? ORDER BY box, -fail_count, id",
            (topic_id,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        return ids[:limit] if limit else ids

    # Determine total pool
    all_cards_row = db.execute(
        "SELECT COUNT(*) as c FROM cards WHERE topic_id=?", (topic_id,)
    ).fetchone()
    total = all_cards_row["c"] if all_cards_row else 0
    cap = limit if limit else total
    if cap <= 0:
        return []

    # --- Recency penalty: cards from the most recent session ---
    last_session_cards = set()
    last_session = db.execute(
        "SELECT DISTINCT date FROM session_logs WHERE topic_id=? ORDER BY date DESC LIMIT 1",
        (topic_id,),
    ).fetchone()
    if last_session:
        for r in db.execute(
            "SELECT card_id FROM session_logs WHERE topic_id=? AND date=?",
            (topic_id, last_session["date"]),
        ).fetchall():
            last_session_cards.add(r["card_id"])

    # --- Build concept -> cards mapping ---
    concept_cards = {}
    for p in priorities:
        rows = db.execute(
            "SELECT card_id FROM card_concepts WHERE concept_id=?",
            (p["concept_id"],),
        ).fetchall()
        concept_cards[p["concept_id"]] = [r["card_id"] for r in rows]

    # Partition concepts
    overdue = [c for c in priorities if c["next_review"] <= today]
    weak = [c for c in priorities if c["retention"] < 60]
    medium = [c for c in priorities if 60 <= c["retention"] < 85]
    strong = [c for c in priorities if c["retention"] >= 85]

    selected = []
    used_cards = set()
    covered_concepts = set()

    def _pick_best_card(concept_id):
        """Pick best available card for a concept, preferring non-recent."""
        candidates = concept_cards.get(concept_id, [])
        candidates_sorted = sorted(
            candidates,
            key=lambda cid: (cid in used_cards, cid in last_session_cards, cid),
        )
        for cid in candidates_sorted:
            if cid not in used_cards:
                return cid
        return None

    # Phase 1: Overdue concepts first (one card per overdue concept)
    for c in overdue:
        if len(selected) >= cap:
            break
        cid = _pick_best_card(c["concept_id"])
        if cid is not None:
            selected.append(cid)
            used_cards.add(cid)
            covered_concepts.add(c["concept_id"])

    # Phase 2: One card per uncovered weak concept (concept dedup)
    uncovered_weak = [c for c in weak if c["concept_id"] not in covered_concepts]
    for c in uncovered_weak:
        if len(selected) >= cap:
            break
        cid = _pick_best_card(c["concept_id"])
        if cid is not None:
            selected.append(cid)
            used_cards.add(cid)
            covered_concepts.add(c["concept_id"])

    # Phase 3: Allocate remaining slots with 50/30/20 mix
    remaining_cap = cap - len(selected)
    if remaining_cap > 0:
        def _bucket_cards(concept_list):
            cids = []
            for c in concept_list:
                for card_id in concept_cards.get(c["concept_id"], []):
                    if card_id not in used_cards:
                        cids.append(card_id)
            seen = set()
            result = []
            for cid in cids:
                if cid not in seen:
                    seen.add(cid)
                    result.append(cid)
            result.sort(key=lambda cid: (cid in last_session_cards, cid))
            return result

        w_pool = _bucket_cards(weak)
        m_pool = _bucket_cards(medium)
        s_pool = _bucket_cards(strong)

        n_weak = max(1, round(remaining_cap * 0.50)) if w_pool else 0
        n_medium = max(1, round(remaining_cap * 0.30)) if m_pool else 0
        n_strong = max(1, round(remaining_cap * 0.20)) if s_pool else 0

        for cid in w_pool[:n_weak]:
            if len(selected) >= cap:
                break
            selected.append(cid)
            used_cards.add(cid)
        for cid in m_pool[:n_medium]:
            if len(selected) >= cap:
                break
            selected.append(cid)
            used_cards.add(cid)
        for cid in s_pool[:n_strong]:
            if len(selected) >= cap:
                break
            selected.append(cid)
            used_cards.add(cid)

    # Phase 4: Guarantee at least one strong-but-aging maintenance card
    has_strong = any(c["concept_id"] in covered_concepts for c in strong)
    if strong and not has_strong:
        aging = sorted(strong, key=lambda c: c["next_review"])
        for c in aging:
            cid = _pick_best_card(c["concept_id"])
            if cid is not None and cid not in used_cards:
                if len(selected) >= cap:
                    selected[-1] = cid
                else:
                    selected.append(cid)
                used_cards.add(cid)
                break

    # Phase 5: Top up if still under cap
    all_card_ids = db.execute(
        "SELECT id FROM cards WHERE topic_id=? ORDER BY id", (topic_id,)
    ).fetchall()
    for r in all_card_ids:
        if len(selected) >= cap:
            break
        if r["id"] not in used_cards:
            selected.append(r["id"])
            used_cards.add(r["id"])

    random.shuffle(selected)
    return selected[:cap]


# ── Consistency Checks ────────────────────────────────────────────────────

def verify_concept_integrity(db) -> list:
    """Check concept-level data integrity and return a list of issues found.

    Checks:
      1. Every card has at least one concept mapping (card_concepts row).
      2. Every concept has a concept_state row.
      3. topics.next_review is consistent with compute_topic_schedule_from_concepts().

    Returns a list of dicts: [{"type": "...", "detail": "..."}]
    Callers can raise/log as appropriate.
    """
    issues = []

    # 1. Cards without concept mappings
    orphan_cards = db.execute("""
        SELECT c.id, c.topic_id
        FROM cards c
        LEFT JOIN card_concepts cc ON cc.card_id = c.id
        WHERE cc.card_id IS NULL
    """).fetchall()
    for row in orphan_cards:
        issues.append({
            "type": "card_missing_concept",
            "detail": f"card {row['id']} (topic {row['topic_id']}) has no concept mapping",
        })

    # 2. Concepts without concept_state
    stateless = db.execute("""
        SELECT c.id, c.name
        FROM concepts c
        LEFT JOIN concept_states cs ON cs.concept_id = c.id
        WHERE cs.concept_id IS NULL
    """).fetchall()
    for row in stateless:
        issues.append({
            "type": "concept_missing_state",
            "detail": f"concept {row['id']} ({row['name']}) has no concept_state",
        })

    # 3. topics.next_review vs concept-derived next_review
    topics = db.execute("SELECT id, name, next_review FROM topics").fetchall()
    for t in topics:
        cdata = compute_topic_schedule_from_concepts(db, t["id"])
        derived = cdata["next_review"]
        if derived and derived != t["next_review"]:
            issues.append({
                "type": "topic_schedule_mismatch",
                "detail": (f"topic {t['id']} ({t['name']}): "
                           f"stored={t['next_review']}, derived={derived}"),
            })

    return issues


def repair_concept_integrity(db) -> dict:
    """Auto-repair common concept integrity issues.

    Fixes:
      - Creates fallback concepts for cards missing mappings
      - Creates concept_states for concepts missing them
      - Recomputes topics.next_review from concept states

    Returns {"repaired_cards": N, "repaired_states": N, "repaired_topics": N}
    """
    repaired = {"repaired_cards": 0, "repaired_states": 0, "repaired_topics": 0}

    # Fix cards without concepts
    orphan_cards = db.execute("""
        SELECT c.id, c.topic_id
        FROM cards c
        LEFT JOIN card_concepts cc ON cc.card_id = c.id
        WHERE cc.card_id IS NULL
    """).fetchall()
    for row in orphan_cards:
        get_or_create_fallback_concept_for_card(db, row["id"], row["topic_id"])
        repaired["repaired_cards"] += 1

    # Fix concepts without states
    stateless = db.execute("""
        SELECT c.id, c.topic_id
        FROM concepts c
        LEFT JOIN concept_states cs ON cs.concept_id = c.id
        WHERE cs.concept_id IS NULL
    """).fetchall()
    for row in stateless:
        nr = next_review_date(A_INIT, K_INIT)
        # Use topic learned_date as initial last_review
        topic_row = db.execute(
            "SELECT learned_date FROM topics WHERE id=?", (row["topic_id"],)
        ).fetchone()
        initial_lr = topic_row["learned_date"] if topic_row else date.today().isoformat()
        db.execute("""
            INSERT INTO concept_states
                (concept_id, a, k, last_review, next_review,
                 review_count, success_count, failure_count, history)
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, '[]')
        """, (row["id"], A_INIT, K_INIT, initial_lr, nr))
        repaired["repaired_states"] += 1

    # Fix topic schedules
    topics = db.execute("SELECT id, next_review FROM topics").fetchall()
    for t in topics:
        cdata = compute_topic_schedule_from_concepts(db, t["id"])
        derived = cdata["next_review"]
        if derived and derived != t["next_review"]:
            db.execute("UPDATE topics SET next_review=? WHERE id=?",
                       (derived, t["id"]))
            repaired["repaired_topics"] += 1

    return repaired


# ── API Routes ────────────────────────────────────────────────────────────

@app.route("/api/topics", methods=["GET"])
def list_topics():
    today = date.today().isoformat()
    with get_db() as db:
        rows = db.execute("SELECT * FROM topics ORDER BY next_review").fetchall()
        # Pre-fetch card counts
        card_counts = {}
        for cc in db.execute("SELECT topic_id, COUNT(*) as c FROM cards GROUP BY topic_id").fetchall():
            card_counts[cc["topic_id"]] = cc["c"]
        # Pre-fetch problem counts
        problem_counts = {}
        for pc in db.execute("SELECT topic_id, COUNT(*) as c FROM problems GROUP BY topic_id").fetchall():
            problem_counts[pc["topic_id"]] = pc["c"]

        # Pre-fetch concept-level schedule data per topic (new)
        concept_data = {}
        for r in rows:
            concept_data[r["id"]] = compute_topic_schedule_from_concepts(db, r["id"])

    topics = []
    for r in rows:
        ref_date = r["last_review"] or r["learned_date"]
        days_elapsed = (date.today() - date.fromisoformat(ref_date)).days
        # Legacy topic-level retention (kept for analytics/compatibility)
        current_r = retention(r["a"], r["k"], days_elapsed)

        # Use concept-derived retention as the primary display value when
        # concept data is available; fall back to legacy topic retention.
        cdata = concept_data.get(r["id"], {})
        concept_avg_ret = cdata.get("avg_retention")
        # For status/display, prefer concept-based average retention
        display_retention = concept_avg_ret if concept_avg_ret is not None else round(current_r * 100, 1)

        nr = r["next_review"]
        if nr < today:
            d = (date.today() - date.fromisoformat(nr)).days
            status, status_text = "overdue", f"{d}d overdue"
        elif nr == today:
            status, status_text = "due", "Due today"
        else:
            d = (date.fromisoformat(nr) - date.today()).days
            status = "soon" if d <= 7 else "upcoming"
            status_text = f"In {d}d"

        # Override: if concept-based min retention below 40%, mark as overdue
        min_ret = cdata.get("min_retention")
        effective_r = (min_ret / 100.0) if min_ret is not None else current_r
        if effective_r < 0.40 and status not in ("overdue",):
            status, status_text = "overdue", "Low retention"

        next_interval = round(days_to_threshold(r["a"], r["k"]))
        hist = json.loads(r["history"])

        topics.append({
            "id":               r["id"],
            "name":             r["name"],
            "learned_date":     r["learned_date"],
            "last_review":      r["last_review"],
            "next_review":      r["next_review"],
            "review_count":     r["review_count"],
            "a":                round(r["a"], 4),       # legacy — kept for analytics
            "k":                round(r["k"], 4),       # legacy — kept for analytics
            "retention":        display_retention,
            "topic_retention":  round(current_r * 100, 1),  # legacy topic-level retention
            "days_elapsed":     days_elapsed,
            "status":           status,
            "status_text":      status_text,
            "next_interval":    next_interval,
            "history":          hist,
            "reviewed_today":   today in hist,
            "card_count":       card_counts.get(r["id"], 0),
            "problem_count":   problem_counts.get(r["id"], 0),
            "tags":             [t.strip() for t in (r["tags"] or "").split(",") if t.strip()],
            # Concept-level summary (new)
            "concept_avg_retention":   cdata.get("avg_retention"),
            "concept_min_retention":   cdata.get("min_retention"),
            "concept_due_count":       cdata.get("due_count", 0),
            "concept_total":           cdata.get("total_concepts", 0),
            "weakest_concept_name":    cdata.get("weakest_concept_name"),
        })
    return jsonify(topics)


@app.route("/api/topics", methods=["POST"])
def add_topic():
    data = request.json
    name = (data.get("name") or "").strip()
    learned = data.get("learned_date") or date.today().isoformat()
    tags = (data.get("tags") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    nr = next_review_date(A_INIT, K_INIT)
    with get_db() as db:
        db.execute(
            "INSERT INTO topics (name, learned_date, a, k, next_review, tags) VALUES (?,?,?,?,?,?)",
            (name, learned, A_INIT, K_INIT, nr, tags),
        )
    return jsonify({"ok": True})


@app.route("/api/topics/<int:tid>", methods=["PUT"])
def update_topic(tid):
    data = request.json or {}
    with get_db() as db:
        row = db.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        name = (data.get("name") or row["name"]).strip()
        tags = data.get("tags") if "tags" in data else row["tags"]
        learned_date = data.get("learned_date") or row["learned_date"]
        next_rev = data.get("next_review") or row["next_review"]
        last_rev = data.get("last_review") if "last_review" in data else row["last_review"]
        review_count = data.get("review_count") if "review_count" in data else row["review_count"]
        if isinstance(review_count, str) and review_count.isdigit():
            review_count = int(review_count)
        if not isinstance(review_count, int) or review_count < 0:
            review_count = row["review_count"]
        db.execute(
            "UPDATE topics SET name=?, tags=?, learned_date=?, next_review=?, last_review=?, review_count=? WHERE id=?",
            (name, tags, learned_date, next_rev, last_rev, review_count, tid),
        )
    return jsonify({"ok": True})


@app.route("/api/topics/<int:tid>/review", methods=["POST"])
def mark_reviewed(tid):
    """LEGACY / DEPRECATED — manual topic-level review without card ratings.

    This endpoint pre-dates the concept-level memory system.  It still
    updates topic-level a/k for backward compatibility and also propagates
    the rating to all concepts — but with a *reduced* global_factor (0.5)
    so the uniform rating causes less distortion to concept granularity
    than a real per-card session would.

    Prefer POST /api/topics/<tid>/session with card_ratings instead.
    """
    today = date.today().isoformat()
    data = request.json or {}
    rating = data.get("rating", "complete")  # complete | partial | failed
    if rating not in ("complete", "partial", "failed"):
        rating = "complete"
    with get_db() as db:
        row = db.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        history = json.loads(row["history"])
        if today in history:
            return jsonify({"error": "Already reviewed today", "already": True}), 409

        # Legacy topic-level curve update (kept for analytics)
        a_new, k_new = update_curve_rated(row["a"], row["k"], rating)
        history.append(today)

        # --- Propagate rating to all concepts with reduced global_factor ---
        # Using global_factor=0.5 dampens the uniform signal so concept
        # states are nudged gently rather than fully updated.  This avoids
        # overwriting per-concept granularity from real sessions.
        concept_rows = db.execute("""
            SELECT cs.concept_id
            FROM concept_states cs
            JOIN concepts c ON c.id = cs.concept_id
            WHERE c.topic_id=?
        """, (tid,)).fetchall()
        _LEGACY_GLOBAL_FACTOR = 0.5
        for cr in concept_rows:
            update_concept_state(
                db, cr["concept_id"], rating, today,
                weight=1.0, global_factor=_LEGACY_GLOBAL_FACTOR, topic_id=tid,
            )

        # Derive topic next_review from concept states
        concept_schedule = compute_topic_schedule_from_concepts(db, tid)
        if concept_schedule["next_review"]:
            nr = concept_schedule["next_review"]
        else:
            if rating == "complete":
                lr = today
                nr = next_review_date(a_new, k_new)
            else:
                lr = row["last_review"] or row["learned_date"]
                days_gap = round(days_to_threshold(a_new, k_new))
                nr = (date.today() + timedelta(days=max(1, days_gap))).isoformat()

        if rating == "complete":
            lr = today
        else:
            lr = row["last_review"] or row["learned_date"]

        db.execute(
            """UPDATE topics
               SET a=?, k=?, last_review=?, next_review=?,
                   review_count=review_count+1, history=?
               WHERE id=?""",
            (a_new, k_new, lr, nr, json.dumps(history), tid),
        )
    return jsonify({
        "ok": True, "next_review": nr, "rating": rating,
        "deprecated": True,
        "warning": "mark_reviewed is deprecated. Use POST /api/topics/<tid>/session with card_ratings for accurate concept-level updates.",
    })


@app.route("/api/topics/<int:tid>/cards", methods=["GET"])
def get_cards(tid):
    with get_db() as db:
        rows = db.execute("SELECT * FROM cards WHERE topic_id=? ORDER BY box, -fail_count, id", (tid,)).fetchall()
    return jsonify([{"id": r["id"], "topic_id": r["topic_id"], "card_type": r["card_type"],
                     "question": r["question"], "answer": r["answer"],
                     "wrong_options": json.loads(r["wrong_options"]) if r["wrong_options"] else [],
                     "box": r["box"], "fail_count": r["fail_count"],
                     "success_count": r["success_count"], "last_rating": r["last_rating"]} for r in rows])


@app.route("/api/topics/<int:tid>/cards", methods=["POST"])
def add_card(tid):
    data = request.json
    q = (data.get("question") or "").strip()
    a = (data.get("answer") or "").strip()
    ct = data.get("card_type", "qa")
    wo = data.get("wrong_options") or []
    if ct not in ("qa", "recall"):
        ct = "qa"
    if not q:
        return jsonify({"error": "Question required"}), 400
    wo_json = json.dumps(wo) if isinstance(wo, list) else "[]"
    with get_db() as db:
        cur = db.execute("INSERT INTO cards (topic_id, card_type, question, answer, wrong_options) VALUES (?,?,?,?,?)",
                         (tid, ct, q, a, wo_json))
        card_id = cur.lastrowid
        # Create a fallback concept for the new card so it participates in
        # concept-level scheduling immediately.
        get_or_create_fallback_concept_for_card(db, card_id, tid)
    return jsonify({"ok": True, "id": card_id})


@app.route("/api/cards/<int:cid>", methods=["PUT"])
def update_card(cid):
    data = request.json
    q = (data.get("question") or "").strip()
    a = (data.get("answer") or "").strip()
    ct = data.get("card_type", "qa")
    wo = data.get("wrong_options") or []
    if ct not in ("qa", "recall"):
        ct = "qa"
    if not q:
        return jsonify({"error": "Question required"}), 400
    wo_json = json.dumps(wo) if isinstance(wo, list) else "[]"
    with get_db() as db:
        db.execute("UPDATE cards SET question=?, answer=?, card_type=?, wrong_options=? WHERE id=?",
                   (q, a, ct, wo_json, cid))
    return jsonify({"ok": True})


@app.route("/api/cards/<int:cid>", methods=["DELETE"])
def delete_card(cid):
    with get_db() as db:
        # Find concepts linked only to this card (will become orphans)
        linked = db.execute(
            "SELECT concept_id FROM card_concepts WHERE card_id=?", (cid,)
        ).fetchall()
        # Remove card_concepts links first
        db.execute("DELETE FROM card_concepts WHERE card_id=?", (cid,))
        # Clean up orphaned concepts (concepts with no remaining card links)
        for lnk in linked:
            con_id = lnk["concept_id"]
            remaining = db.execute(
                "SELECT COUNT(*) as c FROM card_concepts WHERE concept_id=?",
                (con_id,),
            ).fetchone()
            if remaining["c"] == 0:
                db.execute("DELETE FROM concept_session_snapshots WHERE concept_id=?", (con_id,))
                db.execute("DELETE FROM concept_states WHERE concept_id=?", (con_id,))
                db.execute("DELETE FROM concepts WHERE id=?", (con_id,))
        db.execute("DELETE FROM cards WHERE id=?", (cid,))
    return jsonify({"ok": True})


@app.route("/api/topics/<int:tid>/cards/bulk", methods=["POST"])
def bulk_import_cards(tid):
    """Import multiple cards at once.

    Body: { "cards": [ {"question": "...", "answer": "...", "card_type": "qa"}, ... ] }
    Or:   { "text": "Q1\\nA1\\n\\nQ2\\nA2\\n...", "default_type": "qa" }
    Blank-line separated blocks. Each block can have a [qa] or [recall]
    prefix on the first line to override the default type.
    For qa: first line = question, second = answer.
    For recall: entire block is the prompt (no answer expected).
    """
    import re
    data = request.json or {}
    cards_list = data.get("cards")
    default_type = data.get("default_type", "qa")
    if default_type not in ("qa", "recall"):
        default_type = "qa"
    if not cards_list:
        raw = (data.get("text") or "").strip()
        if not raw:
            return jsonify({"error": "No cards provided"}), 400
        # Normalise line endings and split on blank lines
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        cards_list = []
        blocks = [b.strip() for b in re.split(r"\n\s*\n", raw) if b.strip()]
        type_prefix_re = re.compile(r"^\[(qa|recall)\]\s*", re.IGNORECASE)
        for block in blocks:
            # Check for [type] prefix on first line
            ct = default_type
            match = type_prefix_re.match(block)
            if match:
                ct = match.group(1).lower()
                block = block[match.end():].strip()
            if not block:
                continue
            if ct == "qa":
                lines = block.split("\n", 1)
                q = lines[0].strip()
                a = lines[1].strip() if len(lines) > 1 else ""
            else:
                # For recall, the entire block is the prompt
                q = block.strip()
                a = ""
            if q:
                cards_list.append({"question": q, "answer": a, "card_type": ct,
                                   "wrong_options": []})
    added = 0
    with get_db() as db:
        for c in cards_list:
            q = (c.get("question") or "").strip()
            a = (c.get("answer") or "").strip()
            ct = c.get("card_type", "qa")
            if ct not in ("qa", "recall"):
                ct = "qa"
            if not q:
                continue
            wo = c.get("wrong_options") or []
            wo_json = json.dumps(wo) if isinstance(wo, list) else "[]"
            cur = db.execute("INSERT INTO cards (topic_id, card_type, question, answer, wrong_options) VALUES (?,?,?,?,?)",
                       (tid, ct, q, a, wo_json))
            card_id = cur.lastrowid
            # Ensure every imported card has a concept mapping immediately
            get_or_create_fallback_concept_for_card(db, card_id, tid)
            added += 1
    return jsonify({"ok": True, "added": added})


@app.route("/api/topics/<int:tid>/session", methods=["POST"])
def save_session(tid):
    """Save a full study session with per-card ratings.

    Body: { "card_ratings": [ {"card_id": 1, "rating": "complete"}, ... ] }

    Updates each card's box/fail_count/success_count/last_rating,
    logs each rating to session_logs, updates concept-level forgetting
    curves for every concept linked to each card, derives the topic's
    next_review from concept states, and still computes a whole-session
    aggregate for analytics and legacy topic-level curve.

    NEW (hybrid concept model):
      - Per-card ratings are decomposed into concept-level updates
      - Topic next_review is derived from the weakest concept
      - Topic-level a/k are still updated for legacy/analytics but no
        longer solely determine scheduling
    """
    today = date.today().isoformat()
    data = request.json or {}
    card_ratings = data.get("card_ratings", [])

    with get_db() as db:
        row = db.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        history = json.loads(row["history"])
        if today in history:
            return jsonify({"error": "Already reviewed today", "already": True}), 409

        # --- Overall rating for topic curve (weighted average) ---
        # Computed first so it can be used as global_factor for concept updates.
        ratings_list = [cr.get("rating", "complete") for cr in card_ratings]
        score_map = {"complete": 1.0, "partial": 0.5, "failed": 0.0}
        if ratings_list:
            avg = sum(score_map.get(r, 1.0) for r in ratings_list) / len(ratings_list)
            if avg >= 0.8:
                overall = "complete"
            elif avg >= 0.4:
                overall = "partial"
            else:
                overall = "failed"
        else:
            avg = 1.0
            overall = "complete"

        # global_factor: modest modulation from session performance (0.85–1.15)
        # Good sessions slightly boost concept improvements; bad ones dampen.
        global_factor = 0.85 + avg * 0.30  # avg=0→0.85, avg=0.5→1.0, avg=1→1.15

        # --- Per-card updates + concept-level updates ---
        for cr in card_ratings:
            cid = cr.get("card_id")
            r = cr.get("rating", "complete")
            if r not in ("complete", "partial", "failed"):
                r = "complete"
            card = db.execute("SELECT * FROM cards WHERE id=? AND topic_id=?", (cid, tid)).fetchone()
            if not card:
                continue

            # Card-level stats (preserved from original)
            box = card["box"]
            fc = card["fail_count"]
            sc = card["success_count"]
            if r == "complete":
                box = min(5, box + 1)
                sc += 1
            elif r == "partial":
                # stay in same box
                sc += 1
            else:  # failed
                box = max(1, box - 1)
                fc += 1
            db.execute(
                "UPDATE cards SET box=?, fail_count=?, success_count=?, last_rating=? WHERE id=?",
                (box, fc, sc, r, cid),
            )
            db.execute(
                "INSERT INTO session_logs (card_id, topic_id, rating, date) VALUES (?,?,?,?)",
                (cid, tid, r, today),
            )

            # --- Concept-level updates (new) ---
            concepts = get_card_concepts(db, cid)
            if not concepts:
                concepts = get_or_create_fallback_concept_for_card(db, cid, tid)
            for cm in concepts:
                update_concept_state(
                    db, cm["concept_id"], r, today,
                    weight=cm["weight"], global_factor=global_factor,
                    topic_id=tid,
                )

        # --- Legacy topic-level curve update (kept for analytics/compat) ---
        a_new, k_new = update_curve_rated(row["a"], row["k"], overall)
        history.append(today)

        # --- Derive topic next_review from concept states (new) ---
        concept_schedule = compute_topic_schedule_from_concepts(db, tid)
        if concept_schedule["next_review"]:
            # Topic scheduling is now driven by concept states
            nr = concept_schedule["next_review"]
        else:
            # Fallback: no concepts yet, use legacy topic-level schedule
            if overall == "complete":
                nr = next_review_date(a_new, k_new)
            else:
                days_gap = round(days_to_threshold(a_new, k_new))
                nr = (date.today() + timedelta(days=max(1, days_gap))).isoformat()

        # last_review for the topic — set to today on complete, else keep old
        if overall == "complete":
            lr = today
        else:
            lr = row["last_review"] or row["learned_date"]

        # Update a_after / k_after on session logs for this session
        db.execute(
            "UPDATE session_logs SET a_after=?, k_after=? WHERE topic_id=? AND date=?",
            (a_new, k_new, tid, today),
        )

        db.execute(
            """UPDATE topics
               SET a=?, k=?, last_review=?, next_review=?,
                   review_count=review_count+1, history=?,
                   consecutive_reviews=consecutive_reviews+1
               WHERE id=?""",
            (a_new, k_new, lr, nr, json.dumps(history), tid),
        )

        # Check consecutive reviews for variation trigger
        updated = db.execute("SELECT consecutive_reviews FROM topics WHERE id=?", (tid,)).fetchone()
        consec = updated["consecutive_reviews"] if updated else 0

        # --- Update review_days aggregate ---
        card_count = len(card_ratings)
        existing_day = db.execute("SELECT * FROM review_days WHERE date=?", (today,)).fetchone()
        if existing_day:
            db.execute(
                "UPDATE review_days SET topic_count=topic_count+1, card_count=card_count+? WHERE date=?",
                (card_count, today),
            )
        else:
            db.execute(
                "INSERT INTO review_days (date, topic_count, card_count) VALUES (?,1,?)",
                (today, card_count),
            )

    return jsonify({
        "ok": True,
        "next_review": nr,
        "rating": overall,
        "consecutive_reviews": consec,
        # New concept-level summary data
        "concept_summary": concept_schedule,
    })


# ── AI Card Variation ─────────────────────────────────────────────────────

@app.route("/api/topics/<int:tid>/vary", methods=["POST"])
def vary_cards(tid):
    """Use Claude to generate slight variations of existing cards.

    After 2+ consecutive quizzes on the same topic, this endpoint rewrites
    questions/answers to test the same concepts from a different angle.
    Only modifies the question and answer text — does not change card type,
    wrong_options, box, or stats.
    """
    if not ANTHROPIC_CLIENT:
        return jsonify({"error": "AI not available"}), 503

    with get_db() as db:
        topic = db.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        if not topic:
            return jsonify({"error": "Not found"}), 404
        cards = db.execute(
            "SELECT id, card_type, question, answer FROM cards WHERE topic_id=?",
            (tid,),
        ).fetchall()
        if not cards:
            return jsonify({"error": "No cards to vary"}), 400

    cards_json = json.dumps(
        [{"id": c["id"], "type": c["card_type"], "question": c["question"],
          "answer": c["answer"]} for c in cards],
        ensure_ascii=False,
    )

    system_prompt = (
        "You are a study-card variation assistant. You will receive a JSON array of "
        "flashcards. For each card, produce a SLIGHT variation that tests the SAME "
        "underlying concept but from a different angle, with different wording, or "
        "asking for a related but distinct detail.\n\n"
        "Rules:\n"
        "- Keep the same difficulty level and depth.\n"
        "- For 'qa' cards: rephrase the question and adjust the answer accordingly.\n"
        "- For 'recall' cards: rephrase the prompt so it triggers recall of the same "
        "material from a new perspective.\n"
        "- Preserve any LaTeX math notation ($ ... $ or $$ ... $$).\n"
        "- Keep answers concise (same length as originals).\n"
        "- Do NOT change the card id or type.\n"
        "- Return ONLY a valid JSON array with objects: "
        '{\"id\": <int>, \"question\": \"...\", \"answer\": \"...\"}\n'
        "- No markdown fences, no commentary — pure JSON."
    )

    user_prompt = (
        f"Topic: {topic['name']}\n\n"
        f"Cards to vary:\n{cards_json}"
    )

    try:
        resp = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()
        variations = json.loads(text)
    except Exception as e:
        return jsonify({"error": f"AI variation failed: {e}"}), 500

    # Apply variations to database
    updated = 0
    with get_db() as db:
        for v in variations:
            cid = v.get("id")
            new_q = v.get("question", "").strip()
            new_a = v.get("answer", "").strip()
            if not cid or not new_q:
                continue
            db.execute(
                "UPDATE cards SET question=?, answer=? WHERE id=? AND topic_id=?",
                (new_q, new_a, cid, tid),
            )
            updated += 1
        # Reset consecutive_reviews counter after variation
        db.execute(
            "UPDATE topics SET consecutive_reviews=0 WHERE id=?", (tid,)
        )

    return jsonify({"ok": True, "varied": updated})


# ── Practice Problems CRUD ────────────────────────────────────────────────

@app.route("/api/topics/<int:tid>/problems", methods=["GET"])
def list_problems(tid):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM problems WHERE topic_id=? ORDER BY difficulty, id", (tid,)
        ).fetchall()
        # Fetch attempt counts per problem
        attempt_counts = {}
        for ac in db.execute(
            "SELECT problem_id, COUNT(*) as c FROM problem_attempts WHERE topic_id=? GROUP BY problem_id",
            (tid,),
        ).fetchall():
            attempt_counts[ac["problem_id"]] = ac["c"]
        # Fetch last rating per problem
        last_ratings = {}
        for lr in db.execute(
            """SELECT problem_id, rating FROM problem_attempts
               WHERE topic_id=? AND rating != ''
               ORDER BY id DESC""",
            (tid,),
        ).fetchall():
            if lr["problem_id"] not in last_ratings:
                last_ratings[lr["problem_id"]] = lr["rating"]
    return jsonify([{
        "id": r["id"], "topic_id": r["topic_id"],
        "title": r["title"], "prompt": r["prompt"],
        "hints": json.loads(r["hints"]) if r["hints"] else [],
        "final_answer": r["final_answer"],
        "full_solution": r["full_solution"],
        "skill_tag": r["skill_tag"],
        "difficulty": r["difficulty"],
        "source": r["source"] if "source" in r.keys() else "generated",
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "attempt_count": attempt_counts.get(r["id"], 0),
        "last_rating": last_ratings.get(r["id"], ""),
    } for r in rows])


@app.route("/api/topics/<int:tid>/problems", methods=["POST"])
def add_problem(tid):
    data = request.json or {}
    title = (data.get("title") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    if not title or not prompt:
        return jsonify({"error": "Title and prompt required"}), 400
    hints = data.get("hints") or []
    if not isinstance(hints, list):
        hints = []
    now = date.today().isoformat()
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO problems
               (topic_id, title, prompt, hints, final_answer, full_solution,
                skill_tag, difficulty, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (tid, title, prompt, json.dumps(hints),
             (data.get("final_answer") or "").strip(),
             (data.get("full_solution") or "").strip(),
             (data.get("skill_tag") or "").strip(),
             int(data.get("difficulty", 1)),
             now, now),
        )
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/problems/<int:pid>", methods=["PUT"])
def update_problem(pid):
    data = request.json or {}
    title = (data.get("title") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    if not title or not prompt:
        return jsonify({"error": "Title and prompt required"}), 400
    hints = data.get("hints") or []
    if not isinstance(hints, list):
        hints = []
    now = date.today().isoformat()
    with get_db() as db:
        db.execute(
            """UPDATE problems SET title=?, prompt=?, hints=?, final_answer=?,
               full_solution=?, skill_tag=?, difficulty=?, updated_at=?
               WHERE id=?""",
            (title, prompt, json.dumps(hints),
             (data.get("final_answer") or "").strip(),
             (data.get("full_solution") or "").strip(),
             (data.get("skill_tag") or "").strip(),
             int(data.get("difficulty", 1)),
             now, pid),
        )
    return jsonify({"ok": True})


@app.route("/api/problems/<int:pid>", methods=["DELETE"])
def delete_problem(pid):
    with get_db() as db:
        db.execute("DELETE FROM problem_attempts WHERE problem_id=?", (pid,))
        db.execute("DELETE FROM problems WHERE id=?", (pid,))
    return jsonify({"ok": True})


@app.route("/api/problems/<int:pid>/attempts", methods=["GET"])
def list_attempts(pid):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM problem_attempts WHERE problem_id=? ORDER BY id DESC",
            (pid,),
        ).fetchall()
    return jsonify([{
        "id": r["id"], "problem_id": r["problem_id"],
        "user_answer": r["user_answer"], "rating": r["rating"],
        "ai_explanation": r["ai_explanation"],
        "hints_used": r["hints_used"],
        "solution_viewed": r["solution_viewed"],
        "created_at": r["created_at"],
    } for r in rows])


@app.route("/api/problems/<int:pid>/attempt", methods=["POST"])
def submit_attempt(pid):
    """Submit a practice problem attempt. Optionally triggers AI evaluation."""
    data = request.json or {}
    user_answer = (data.get("user_answer") or "").strip()
    rating = data.get("rating", "")
    hints_used = int(data.get("hints_used", 0))
    solution_viewed = int(data.get("solution_viewed", 0))

    with get_db() as db:
        prob = db.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
        if not prob:
            return jsonify({"error": "Problem not found"}), 404

    ai_explanation = ""

    # AI evaluation if user provided an answer and AI is available
    if user_answer and ANTHROPIC_CLIENT:
        try:
            topic_name = ""
            with get_db() as db:
                tr = db.execute("SELECT name FROM topics WHERE id=?", (prob["topic_id"],)).fetchone()
                if tr:
                    topic_name = tr["name"]

            sys_prompt = (
                "You are a study assistant evaluating a student's attempt at a practice problem. "
                "Topic: " + topic_name + ".\n\n"
                "Evaluate correctness, completeness, and reasoning quality.\n"
                "Consider partial credit for correct approach with minor errors.\n\n"
                "IMPORTANT: Treat LaTeX ($...$) and plain ASCII as equivalent representations.\n\n"
                "Respond ONLY with this JSON (no markdown, no fences):\n"
                '{"rating": "complete|partial|failed", '
                '"explanation": "1-3 sentence feedback", '
                '"key_missing": "what was wrong or missing (empty if nothing)"}\n\n'
                "Rating guidelines:\n"
                "- complete: correct answer with sound reasoning\n"
                "- partial: partially correct or right approach with errors\n"
                "- failed: incorrect or fundamentally flawed"
            )

            user_prompt = (
                f"Problem: {prob['prompt']}\n\n"
                + (f"Correct final answer: {prob['final_answer']}\n\n" if prob["final_answer"] else "")
                + (f"Full solution:\n{prob['full_solution']}\n\n" if prob["full_solution"] else "")
                + f"Student's answer:\n{user_answer}\n\n"
                "Evaluate this attempt."
            )

            resp = ANTHROPIC_CLIENT.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=400,
                system=sys_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = resp.content[0].text.strip()
            parsed = json.loads(raw)
            ai_explanation = parsed.get("explanation", "")
            ai_missing = parsed.get("key_missing", "")
            if ai_missing:
                ai_explanation += " Missing: " + ai_missing
            # Use AI rating if user didn't specify one
            if not rating:
                rating = parsed.get("rating", "partial")
                if rating not in ("complete", "partial", "failed"):
                    rating = "partial"
        except Exception:
            pass

    if not rating:
        rating = "partial"

    now = date.today().isoformat()
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO problem_attempts
               (problem_id, topic_id, user_answer, rating, ai_explanation,
                hints_used, solution_viewed, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, prob["topic_id"], user_answer, rating, ai_explanation,
             hints_used, solution_viewed, now),
        )

    return jsonify({
        "ok": True,
        "id": cur.lastrowid,
        "rating": rating,
        "ai_explanation": ai_explanation,
    })


# ── On-Demand Hint / Solution Generation ──────────────────────────────────

@app.route("/api/problems/<int:pid>/hint", methods=["POST"])
def generate_hint(pid):
    """Generate the next hint for a problem on demand via Claude."""
    if not ANTHROPIC_CLIENT:
        return jsonify({"error": "AI unavailable"}), 503
    data = request.json or {}
    hints_so_far = data.get("hints_so_far", [])
    if not isinstance(hints_so_far, list):
        hints_so_far = []

    with get_db() as db:
        prob = db.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
        if not prob:
            return jsonify({"error": "Problem not found"}), 404
        topic = db.execute("SELECT name FROM topics WHERE id=?", (prob["topic_id"],)).fetchone()
        topic_name = topic["name"] if topic else ""

    # Check if we already have stored hints
    stored_hints = json.loads(prob["hints"]) if prob["hints"] else []
    hint_index = len(hints_so_far)
    if hint_index < len(stored_hints):
        return jsonify({"hint": stored_hints[hint_index], "hint_number": hint_index + 1})

    try:
        prev_hints_text = ""
        if hints_so_far:
            prev_hints_text = "Previously given hints:\n" + "\n".join(
                f"Hint {i+1}: {h}" for i, h in enumerate(hints_so_far)
            ) + "\n\n"

        sys_prompt = (
            "You are a study assistant providing hints for a practice problem. "
            "Topic: " + topic_name + ".\n\n"
            "Generate ONE helpful hint that nudges the student toward the solution "
            "without giving the answer away. "
            "Each successive hint should be more specific than the last.\n\n"
            "Use $...$ for inline LaTeX and $$...$$ for display LaTeX.\n\n"
            "Respond with ONLY a JSON object (no markdown, no fences):\n"
            '{"hint": "your hint text here"}'
        )

        user_prompt = (
            f"Problem: {prob['prompt']}\n\n"
            + prev_hints_text
            + f"Generate hint #{hint_index + 1}."
        )

        resp = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        parsed = json.loads(raw)
        hint_text = parsed.get("hint", "").strip()
        if not hint_text:
            return jsonify({"error": "AI generated an empty hint"}), 502

        # Persist hint into the stored hints array
        stored_hints.append(hint_text)
        with get_db() as db:
            db.execute(
                "UPDATE problems SET hints=?, updated_at=? WHERE id=?",
                (json.dumps(stored_hints), date.today().isoformat(), pid),
            )

        return jsonify({"hint": hint_text, "hint_number": hint_index + 1})
    except Exception as exc:
        return jsonify({"error": f"Failed to generate hint: {exc}"}), 502


@app.route("/api/problems/<int:pid>/solution", methods=["POST"])
def generate_solution(pid):
    """Generate the full solution for a problem on demand via Claude."""
    if not ANTHROPIC_CLIENT:
        return jsonify({"error": "AI unavailable"}), 503

    with get_db() as db:
        prob = db.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
        if not prob:
            return jsonify({"error": "Problem not found"}), 404
        topic = db.execute("SELECT name FROM topics WHERE id=?", (prob["topic_id"],)).fetchone()
        topic_name = topic["name"] if topic else ""

    # Return cached solution if already generated
    if prob["final_answer"] and prob["full_solution"]:
        return jsonify({
            "final_answer": prob["final_answer"],
            "full_solution": prob["full_solution"],
        })

    try:
        sys_prompt = (
            "You are a study assistant providing a full worked solution. "
            "Topic: " + topic_name + ".\n\n"
            "Provide a concise final answer AND a clear step-by-step solution.\n\n"
            "Use $...$ for inline LaTeX and $$...$$ for display LaTeX.\n\n"
            "Respond with ONLY a JSON object (no markdown, no fences):\n"
            '{"final_answer": "concise answer", "full_solution": "step-by-step worked solution"}'
        )

        user_prompt = f"Problem: {prob['prompt']}\n\nProvide the full solution."

        resp = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        parsed = json.loads(raw)
        final_answer = (parsed.get("final_answer") or "").strip()
        full_solution = (parsed.get("full_solution") or "").strip()

        if not final_answer and not full_solution:
            return jsonify({"error": "AI generated an empty solution"}), 502

        # Persist into DB so it's cached for next time
        with get_db() as db:
            db.execute(
                "UPDATE problems SET final_answer=?, full_solution=?, updated_at=? WHERE id=?",
                (final_answer, full_solution, date.today().isoformat(), pid),
            )

        return jsonify({
            "final_answer": final_answer,
            "full_solution": full_solution,
        })
    except Exception as exc:
        return jsonify({"error": f"Failed to generate solution: {exc}"}), 502


@app.route("/api/problems/<int:pid>/regenerate", methods=["POST"])
def regenerate_problem(pid):
    """Regenerate a problem with the same difficulty, replacing title/prompt and clearing hints/solution."""
    if not ANTHROPIC_CLIENT:
        return jsonify({"error": "AI unavailable"}), 503

    with get_db() as db:
        prob = db.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
        if not prob:
            return jsonify({"error": "Problem not found"}), 404
        topic = db.execute("SELECT name FROM topics WHERE id=?", (prob["topic_id"],)).fetchone()
        topic_name = topic["name"] if topic else ""
        # Gather sibling prompts to avoid duplicates
        siblings = db.execute(
            "SELECT prompt FROM problems WHERE topic_id=? AND id!=?",
            (prob["topic_id"], pid),
        ).fetchall()
    existing_prompts = "\n".join(r["prompt"] for r in siblings) if siblings else ""

    diff_labels = {1: "easy", 2: "medium", 3: "hard"}
    difficulty = prob["difficulty"]
    diff_str = diff_labels.get(difficulty, "medium")

    try:
        sys_prompt = (
            "You are a study assistant generating a single practice problem. "
            "Topic: " + topic_name + ".\n\n"
            "Generate ONE new problem at " + diff_str.upper() + " difficulty "
            "(difficulty level " + str(difficulty) + "/3).\n\n"
            "The problem must:\n"
            "- Be DIFFERENT from the existing problems listed below\n"
            "- Test a distinct concept or skill within the topic\n"
            "- Have a specific, precise prompt\n"
            "- Use $...$ for inline LaTeX and $$...$$ for display LaTeX\n\n"
            "Respond with ONLY a JSON object (no markdown, no fences):\n"
            '{"title": "short title (5-10 words)", "prompt": "full problem statement", '
            '"skill_tag": "one skill keyword"}'
        )

        user_prompt = (
            "Current problem being replaced:\n"
            "Title: " + prob["title"] + "\n"
            "Prompt: " + prob["prompt"] + "\n\n"
        )
        if existing_prompts:
            user_prompt += (
                "Other existing problems (avoid duplicating these):\n"
                + existing_prompts[:3000] + "\n\n"
            )
        user_prompt += (
            "Generate a NEW, DIFFERENT " + diff_str + " problem for this topic."
        )

        resp = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        parsed = json.loads(raw)
        new_title = (parsed.get("title") or "").strip()
        new_prompt = (parsed.get("prompt") or "").strip()
        new_skill = (parsed.get("skill_tag") or prob["skill_tag"] or "").strip()
        if not new_title or not new_prompt:
            return jsonify({"error": "AI generated an empty problem"}), 502

        now = date.today().isoformat()
        with get_db() as db:
            db.execute(
                """UPDATE problems SET title=?, prompt=?, skill_tag=?,
                   hints='[]', final_answer='', full_solution='',
                   source='generated', updated_at=?
                   WHERE id=?""",
                (new_title, new_prompt, new_skill, now, pid),
            )
            # Delete old attempts since the problem changed
            db.execute("DELETE FROM problem_attempts WHERE problem_id=?", (pid,))

        return jsonify({
            "ok": True,
            "id": pid,
            "title": new_title,
            "prompt": new_prompt,
            "skill_tag": new_skill,
            "difficulty": difficulty,
        })
    except json.JSONDecodeError:
        return jsonify({"error": "AI returned invalid JSON. Please try again."}), 502
    except Exception as exc:
        return jsonify({"error": f"Failed to regenerate: {exc}"}), 502


# ── AI Answer Evaluation ──────────────────────────────────────────────────

@app.route("/api/evaluate", methods=["POST"])
def ai_evaluate():
    """Use Claude to evaluate a free-response answer against the correct answer."""
    if not ANTHROPIC_CLIENT:
        return jsonify({"error": "AI evaluation unavailable (anthropic not installed)"}), 503

    data = request.get_json(force=True)
    question = data.get("question", "").strip()
    correct_answer = data.get("correct_answer", "").strip()
    user_answer = data.get("user_answer", "").strip()
    topic_id = data.get("topic_id", 0)
    card_id = data.get("card_id", 0)
    topic_name = data.get("topic_name", "")

    if not user_answer:
        return jsonify({"error": "No user answer provided"}), 400
    if not question:
        return jsonify({"error": "No question provided"}), 400

    # Fetch past corrections for this topic to give as few-shot context
    corrections_context = ""
    with get_db() as db:
        corrections = db.execute(
            """SELECT question, correct_answer, user_answer, ai_rating, user_override, explanation
               FROM ai_corrections
               WHERE topic_id=? AND user_override IS NOT NULL
               ORDER BY id DESC LIMIT 10""",
            (topic_id,),
        ).fetchall()
    if corrections:
        examples = []
        for c in reversed(corrections):
            examples.append(
                f"Q: {c['question']}\n"
                f"Correct: {c['correct_answer']}\n"
                f"Student wrote: {c['user_answer']}\n"
                f"You rated: {c['ai_rating']} — but the correct rating was: {c['user_override']}\n"
                f"Lesson: adjust your strictness accordingly."
            )
        corrections_context = (
            "\n\nIMPORTANT — past corrections from this user (learn from these):\n"
            + "\n---\n".join(examples)
            + "\n\nApply these lessons to your current evaluation.\n"
        )

    system_prompt = (
        "You are a study assistant evaluating a student's free-response answer. "
        "The student is studying the topic: " + topic_name + ".\n\n"
        "Evaluate how well the student's answer matches the correct answer.\n"
        "Consider: accuracy of key concepts, completeness, and understanding.\n"
        "Minor wording differences are acceptable. Focus on conceptual correctness.\n\n"
        "IMPORTANT — LaTeX delimiters: Both the correct answer and the student's answer may contain "
        "LaTeX notation wrapped in $...$ (inline) or $$...$$ (display) delimiters. "
        "Treat the LaTeX content as its rendered mathematical meaning. For example:\n"
        "- '$\\\\frac{a}{b}$' means the fraction a/b\n"
        "- '$\\\\int_0^1 f(x)\\\\,dx$' means the integral from 0 to 1 of f(x) dx\n"
        "- '$\\\\alpha$' means the Greek letter alpha\n"
        "- '$\\\\text{H}_2\\\\text{O}$' means H2O (water)\n"
        "When comparing answers, ignore the $...$ delimiters and LaTeX syntax — compare the MATHEMATICAL MEANING. "
        "A student writing '$PV = nRT$' and another writing 'PV = nRT' are saying the same thing.\n\n"
        "IMPORTANT — Symbol equivalences: Students may type plain ASCII instead of Unicode or LaTeX symbols. "
        "Treat these as EQUIVALENT and fully correct:\n"
        "- -> or --> for \u2192 (right arrow / reaction arrow)\n"
        "- <- for \u2190 (left arrow)\n"
        "- <-> or <=> for \u21CC or \u21D4 (equilibrium / iff)\n"
        "- => or ==> for \u21D2 (implies)\n"
        "- >= for \u2265, <= for \u2264, != for \u2260\n"
        "- alpha, beta, gamma, delta, pi, theta, sigma, omega etc. for their Greek letter equivalents\n"
        "- sqrt() for \u221A, inf or infinity for \u221E\n"
        "- +/- or +- for \u00B1, * or . for \u22C5 (multiplication)\n"
        "- H2O for H\u2082O, CO2 for CO\u2082, Na+ for Na\u207A, etc. (plain text subscripts/superscripts)\n"
        "- ^2 or ^n for superscripts, _0 or _n for subscripts\n"
        "- Any LaTeX notation like \\\\frac{a}{b}, \\\\int, \\\\sum should be treated as equivalent to rendered forms.\n"
        "In general, if the student's meaning is clear and scientifically correct, do NOT penalize for using "
        "ASCII approximations, LaTeX syntax with $ delimiters, or plain text instead of special characters.\n\n"
        "You MUST respond in this exact JSON format (no markdown, no code fences):\n"
        '{"rating": "complete|partial|failed", "explanation": "brief 1-2 sentence explanation", '
        '"key_missing": "what was wrong or missing (empty string if nothing)"}\n\n'
        "Rating guidelines:\n"
        "- complete: answer captures the core concepts correctly, even if phrased differently\n"
        "- partial: answer shows some understanding but misses important parts or has minor errors\n"
        "- failed: answer is mostly wrong, irrelevant, or shows fundamental misunderstanding\n"
        "Be fair but accurate. Students learn best from honest assessment."
        + corrections_context
    )

    user_prompt = (
        f"Question: {question}\n\n"
        + (f"Correct answer: {correct_answer}\n\n" if correct_answer else "")
        + f"Student's answer: {user_answer}\n\n"
        "Evaluate this answer. Respond ONLY with the JSON object."
    )

    try:
        message = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()
        # Parse the JSON response
        result = json.loads(raw)
        rating = result.get("rating", "partial")
        if rating not in ("complete", "partial", "failed"):
            rating = "partial"
        return jsonify({
            "rating": rating,
            "explanation": result.get("explanation", ""),
            "key_missing": result.get("key_missing", ""),
        })
    except json.JSONDecodeError:
        return jsonify({"rating": "partial", "explanation": "AI returned an unparseable response.", "key_missing": ""}), 200
    except Exception as e:
        return jsonify({"error": f"AI evaluation failed: {str(e)}"}), 500


@app.route("/api/evaluate/override", methods=["POST"])
def ai_override():
    """Store a user correction when AI got the rating wrong. This trains future evaluations."""
    data = request.get_json(force=True)
    topic_id = data.get("topic_id", 0)
    card_id = data.get("card_id", 0)
    question = data.get("question", "")
    correct_answer = data.get("correct_answer", "")
    user_answer = data.get("user_answer", "")
    ai_rating = data.get("ai_rating", "")
    user_override = data.get("user_override", "")
    explanation = data.get("explanation", "")

    if not user_override or user_override not in ("complete", "partial", "failed"):
        return jsonify({"error": "Invalid override rating"}), 400

    today = date.today().isoformat()
    with get_db() as db:
        db.execute(
            """INSERT INTO ai_corrections
               (topic_id, card_id, question, correct_answer, user_answer,
                ai_rating, user_override, explanation, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (topic_id, card_id, question, correct_answer, user_answer,
             ai_rating, user_override, explanation, today),
        )
    return jsonify({"ok": True})


def _undo_concept_state(db, concept_id: int, rating: str, review_date: str):
    """Restore a concept's state from concept_session_snapshots (exact undo).

    If a snapshot exists for this concept + date, restore the pre-update
    state exactly.  Falls back to approximate algebraic reversal only if
    no snapshot is found (legacy data before snapshots were introduced).
    """
    # Try snapshot-based restore first
    snap = db.execute(
        """SELECT * FROM concept_session_snapshots
           WHERE concept_id=? AND session_date=?
           ORDER BY id DESC LIMIT 1""",
        (concept_id, review_date),
    ).fetchone()

    if snap:
        db.execute(
            """UPDATE concept_states
               SET a=?, k=?, last_review=?, next_review=?,
                   review_count=?, success_count=?, failure_count=?, history=?
               WHERE concept_id=?""",
            (snap["a_before"], snap["k_before"],
             snap["last_review_before"], snap["next_review_before"],
             snap["review_count_before"], snap["success_count_before"],
             snap["failure_count_before"], snap["history_before"],
             concept_id),
        )
        db.execute(
            "DELETE FROM concept_session_snapshots WHERE concept_id=? AND session_date=?",
            (concept_id, review_date),
        )
        return

    # --- Fallback: approximate algebraic reversal (legacy, pre-snapshot) ---
    state = db.execute(
        "SELECT * FROM concept_states WHERE concept_id=?", (concept_id,)
    ).fetchone()
    if not state:
        return

    sc = state["success_count"]
    fc = state["failure_count"]
    if rating in ("complete", "partial"):
        sc = max(0, sc - 1)
    else:
        fc = max(0, fc - 1)

    a = state["a"]
    k = state["k"]
    if rating == "complete":
        a_old = (a - A_GAIN) / (1.0 - A_GAIN) if a > A_INIT else A_INIT
        k_old = min(K_INIT, k / K_FACTOR) if k < K_INIT else K_INIT
    elif rating == "partial":
        half_gain = A_GAIN * 0.5
        a_old = (a - half_gain) / (1.0 - half_gain) if a > A_INIT else A_INIT
        half_k_factor = 1.0 - (1.0 - K_FACTOR) * 0.5
        k_old = min(K_INIT, k / half_k_factor) if k < K_INIT else K_INIT
    else:
        k_old = k / 1.3 if k > 0.005 else k
        a_old = a
    a_old = max(A_INIT * 0.5, min(0.95, a_old))
    k_old = max(0.005, min(K_INIT * 2.0, k_old))

    history = json.loads(state["history"])
    if review_date in history:
        history.remove(review_date)

    lr = history[-1] if history else None
    nr = next_review_date(a_old, k_old)

    db.execute(
        """UPDATE concept_states
           SET a=?, k=?, last_review=?, next_review=?,
               review_count=MAX(0, review_count-1),
               success_count=?, failure_count=?, history=?
           WHERE concept_id=?""",
        (a_old, k_old, lr, nr, sc, fc, json.dumps(history), concept_id),
    )


def _restore_all_concept_snapshots(db, topic_id: int, session_date: str):
    """Restore ALL concept states for a topic from session snapshots.

    This is more robust than per-card reversal because it handles the case
    where one concept was updated by multiple cards in a single session.
    Snapshots are processed in reverse order so the earliest snapshot
    (the true pre-session state) is what finally remains.
    """
    snaps = db.execute(
        """SELECT * FROM concept_session_snapshots
           WHERE topic_id=? AND session_date=?
           ORDER BY id DESC""",
        (topic_id, session_date),
    ).fetchall()

    if not snaps:
        return set()

    # Process in reverse-id order (DESC) so the last write per concept is
    # the snapshot with the lowest id (= the true pre-session state).
    restored = set()
    for snap in snaps:
        cid = snap["concept_id"]
        db.execute(
            """UPDATE concept_states
               SET a=?, k=?, last_review=?, next_review=?,
                   review_count=?, success_count=?, failure_count=?, history=?
               WHERE concept_id=?""",
            (snap["a_before"], snap["k_before"],
             snap["last_review_before"], snap["next_review_before"],
             snap["review_count_before"], snap["success_count_before"],
             snap["failure_count_before"], snap["history_before"],
             cid),
        )
        restored.add(cid)

    # Clean up all snapshots for this session
    db.execute(
        "DELETE FROM concept_session_snapshots WHERE topic_id=? AND session_date=?",
        (topic_id, session_date),
    )
    return restored


@app.route("/api/topics/<int:tid>/undo-review", methods=["POST"])
def undo_review(tid):
    today = date.today().isoformat()
    with get_db() as db:
        row = db.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        if row["last_review"] != today:
            return jsonify({"error": "Can only undo today's review"}), 400
        if row["review_count"] < 1:
            return jsonify({"error": "No reviews to undo"}), 400
        history = json.loads(row["history"])
        if history and history[-1] == today:
            history.pop()
        prev_review = history[-1] if history else None

        # --- Restore concept states from snapshots (exact undo) ---
        # Do this BEFORE reversing card stats so if snapshot-restore touches
        # concepts from deleted cards, it still works.
        restored_concepts = _restore_all_concept_snapshots(db, tid, today)

        # --- Reverse per-card stats from today's session_logs ---
        logs = db.execute(
            "SELECT card_id, rating FROM session_logs WHERE topic_id=? AND date=?",
            (tid, today),
        ).fetchall()
        for log in logs:
            cid = log["card_id"]
            r = log["rating"]
            card = db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
            if not card:
                continue
            box = card["box"]
            fc = card["fail_count"]
            sc = card["success_count"]
            if r == "complete":
                box = max(1, box - 1)
                sc = max(0, sc - 1)
            elif r == "partial":
                sc = max(0, sc - 1)
            else:  # failed
                box = min(5, box + 1)
                fc = max(0, fc - 1)
            prev_rating = card["last_rating"] if card["last_rating"] != r else ""
            db.execute(
                "UPDATE cards SET box=?, fail_count=?, success_count=?, last_rating=? WHERE id=?",
                (box, fc, sc, prev_rating, cid),
            )

            # Fallback for concepts not covered by snapshots (legacy data)
            if not restored_concepts:
                concepts = get_card_concepts(db, cid)
                for cm in concepts:
                    _undo_concept_state(db, cm["concept_id"], r, today)

        db.execute(
            "DELETE FROM session_logs WHERE topic_id=? AND date=?", (tid, today)
        )

        # --- Reverse review_days aggregate ---
        card_count = len(logs)
        day_row = db.execute("SELECT * FROM review_days WHERE date=?", (today,)).fetchone()
        if day_row:
            new_tc = max(0, day_row["topic_count"] - 1)
            new_cc = max(0, day_row["card_count"] - card_count)
            if new_tc <= 0 and new_cc <= 0:
                db.execute("DELETE FROM review_days WHERE date=?", (today,))
            else:
                db.execute(
                    "UPDATE review_days SET topic_count=?, card_count=? WHERE date=?",
                    (new_tc, new_cc, today),
                )

        # Reverse the legacy topic curve update
        a_old = (row["a"] - A_GAIN) / (1.0 - A_GAIN) if row["a"] > A_INIT else A_INIT
        k_old = min(K_INIT, row["k"] / K_FACTOR) if row["k"] < K_INIT else K_INIT
        a_old = max(A_INIT, min(0.95, a_old))
        k_old = max(0.005, min(K_INIT, k_old))

        # Recompute topic next_review from concept states (now restored)
        concept_schedule = compute_topic_schedule_from_concepts(db, tid)
        if concept_schedule["next_review"]:
            nr = concept_schedule["next_review"]
        else:
            nr = next_review_date(a_old, k_old)

        db.execute(
            """UPDATE topics
               SET a=?, k=?, last_review=?, next_review=?,
                   review_count=review_count-1, history=?
               WHERE id=?""",
            (a_old, k_old, prev_review, nr, json.dumps(history), tid),
        )
    return jsonify({"ok": True})


@app.route("/api/topics/<int:tid>", methods=["DELETE"])
def delete_topic(tid):
    with get_db() as db:
        db.execute("DELETE FROM problem_attempts WHERE topic_id=?", (tid,))
        db.execute("DELETE FROM problems WHERE topic_id=?", (tid,))
        db.execute("DELETE FROM session_logs WHERE topic_id=?", (tid,))
        # Concept cleanup (new) — must delete before cards due to FK references
        db.execute("DELETE FROM concept_session_snapshots WHERE topic_id=?", (tid,))
        db.execute("""DELETE FROM concept_states WHERE concept_id IN
                      (SELECT id FROM concepts WHERE topic_id=?)""", (tid,))
        db.execute("""DELETE FROM card_concepts WHERE concept_id IN
                      (SELECT id FROM concepts WHERE topic_id=?)""", (tid,))
        db.execute("DELETE FROM concepts WHERE topic_id=?", (tid,))
        db.execute("DELETE FROM cards WHERE topic_id=?", (tid,))
        db.execute("DELETE FROM topics WHERE id=?", (tid,))
    return jsonify({"ok": True})


# ── Concept CRUD & Query Endpoints (new) ─────────────────────────────────

@app.route("/api/topics/<int:tid>/concepts", methods=["GET"])
def get_topic_concepts(tid):
    """Return all concepts for a topic with their retention/priority data."""
    with get_db() as db:
        row = db.execute("SELECT id FROM topics WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        priorities = get_concept_priority(db, tid)
        # Enrich with card count per concept
        for p in priorities:
            card_count = db.execute(
                "SELECT COUNT(*) as c FROM card_concepts WHERE concept_id=?",
                (p["concept_id"],),
            ).fetchone()
            p["card_count"] = card_count["c"] if card_count else 0
    return jsonify(priorities)


@app.route("/api/concepts/<int:cid>", methods=["PUT"])
def update_concept(cid):
    """Update a concept's name or description."""
    data = request.json or {}
    with get_db() as db:
        row = db.execute("SELECT * FROM concepts WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        name = (data.get("name") or row["name"]).strip()
        description = data.get("description") if "description" in data else row["description"]
        db.execute(
            "UPDATE concepts SET name=?, description=? WHERE id=?",
            (name, description, cid),
        )
    return jsonify({"ok": True})


@app.route("/api/cards/<int:cid>/concepts", methods=["GET"])
def get_card_concepts_endpoint(cid):
    """Return concepts linked to a card."""
    with get_db() as db:
        mappings = get_card_concepts(db, cid)
        result = []
        for m in mappings:
            concept = db.execute(
                "SELECT * FROM concepts WHERE id=?", (m["concept_id"],)
            ).fetchone()
            if concept:
                result.append({
                    "concept_id": m["concept_id"],
                    "weight": m["weight"],
                    "name": concept["name"],
                    "description": concept["description"],
                })
    return jsonify(result)


@app.route("/api/cards/<int:cid>/concepts", methods=["POST"])
def link_card_concept(cid):
    """Link a card to a concept (create concept if needed).

    Body: { "concept_name": "...", "weight": 1.0, "topic_id": ... }
    If the concept doesn't exist, it is created under the given topic.
    """
    data = request.json or {}
    concept_name = (data.get("concept_name") or "").strip()
    weight = data.get("weight", 1.0)
    with get_db() as db:
        card = db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
        if not card:
            return jsonify({"error": "Card not found"}), 404
        tid = data.get("topic_id") or card["topic_id"]
        if not concept_name:
            return jsonify({"error": "concept_name required"}), 400

        existing = db.execute(
            "SELECT id FROM concepts WHERE topic_id=? AND name=?",
            (tid, concept_name),
        ).fetchone()
        if existing:
            concept_id = existing["id"]
        else:
            db.execute(
                "INSERT INTO concepts (topic_id, name, description) VALUES (?,?,?)",
                (tid, concept_name, data.get("description")),
            )
            concept_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            # Create concept_state
            nr = next_review_date(A_INIT, K_INIT)
            db.execute(
                """INSERT INTO concept_states
                   (concept_id, a, k, last_review, next_review, review_count,
                    success_count, failure_count, history)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (concept_id, A_INIT, K_INIT, None, nr, 0, 0, 0, "[]"),
            )

        db.execute(
            "INSERT OR REPLACE INTO card_concepts (card_id, concept_id, weight) VALUES (?,?,?)",
            (cid, concept_id, weight),
        )
    return jsonify({"ok": True, "concept_id": concept_id})


@app.route("/api/cards/<int:cid>/concepts/<int:concept_id>", methods=["DELETE"])
def unlink_card_concept(cid, concept_id):
    """Remove a card-concept link."""
    with get_db() as db:
        db.execute(
            "DELETE FROM card_concepts WHERE card_id=? AND concept_id=?",
            (cid, concept_id),
        )
    return jsonify({"ok": True})


@app.route("/api/topics/<int:tid>/session-cards", methods=["GET"])
def get_session_cards(tid):
    """Return concept-weighted card selection for a study session.

    Query param: ?limit=N (optional, defaults to all cards)
    """
    limit = request.args.get("limit", type=int)
    with get_db() as db:
        row = db.execute("SELECT id FROM topics WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        card_ids = select_cards_for_session(db, tid, limit=limit)
        cards = []
        for card_id in card_ids:
            card = db.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
            if card:
                cards.append({
                    "id": card["id"],
                    "topic_id": card["topic_id"],
                    "card_type": card["card_type"],
                    "question": card["question"],
                    "answer": card["answer"],
                    "wrong_options": json.loads(card["wrong_options"]) if card["wrong_options"] else [],
                    "box": card["box"],
                    "fail_count": card["fail_count"],
                    "success_count": card["success_count"],
                    "last_rating": card["last_rating"],
                })
    return jsonify(cards)


@app.route("/api/schedule")
def schedule():
    """Project the next ~8 review dates for every topic.

    Simulates future reviews from concept-level a/k curves.  For each
    projected step the *earliest* concept due date becomes the topic's
    review date, then all concepts due on that date are simulated as
    "completed" to advance their curves for the next projection.

    Falls back to legacy topic-level a/k only when a topic has no
    concepts at all.
    """
    with get_db() as db:
        rows = db.execute("SELECT * FROM topics ORDER BY name").fetchall()
        result = []
        for r in rows:
            cdata = compute_topic_schedule_from_concepts(db, r["id"])

            # Gather concept states for simulation
            concept_rows = db.execute("""
                SELECT cs.concept_id, cs.a, cs.k, cs.next_review
                FROM concept_states cs
                JOIN concepts c ON c.id = cs.concept_id
                WHERE c.topic_id=?
            """, (r["id"],)).fetchall()

            reviews = []
            if concept_rows:
                # Simulate on mutable copies of concept curves
                sim = [{"a": cr["a"], "k": cr["k"],
                        "nr": cr["next_review"]} for cr in concept_rows]
                for _ in range(8):
                    # Topic review date = earliest concept next_review
                    earliest = min(s["nr"] for s in sim)
                    reviews.append(earliest)
                    # Simulate "complete" for all concepts due on this date
                    for s in sim:
                        if s["nr"] <= earliest:
                            s["a"], s["k"] = update_concept_curve_rated(
                                s["a"], s["k"], "complete")
                            interval = round(days_to_threshold(s["a"], s["k"]))
                            s["nr"] = (date.fromisoformat(earliest)
                                       + timedelta(days=max(1, interval))).isoformat()
            else:
                # Legacy fallback: no concepts, use topic-level a/k
                ref = r["next_review"]
                a, k = r["a"], r["k"]
                for _ in range(8):
                    reviews.append(ref)
                    a_sim, k_sim = update_curve(a, k)
                    interval = round(days_to_threshold(a_sim, k_sim))
                    ref = (date.fromisoformat(ref)
                           + timedelta(days=interval)).isoformat()
                    a, k = a_sim, k_sim

            result.append({
                "id": r["id"],
                "name": r["name"],
                "reviews": reviews,
                # Concept-level summary for richer schedule display
                "concept_due_count": cdata.get("due_count", 0),
                "concept_total": cdata.get("total_concepts", 0),
                "avg_retention": cdata.get("avg_retention"),
                "min_retention": cdata.get("min_retention"),
            })
    return jsonify(result)


@app.route("/api/stats")
def stats():
    """Return streak data, totals, and review_days for calendar heatmap."""
    with get_db() as db:
        days = db.execute("SELECT date, topic_count, card_count FROM review_days ORDER BY date").fetchall()
        # Also gather from topic histories in case review_days wasn't populated
        hist_dates = set()
        for row in db.execute("SELECT history FROM topics").fetchall():
            for d in json.loads(row["history"]):
                hist_dates.add(d)

    # Merge: review_days table + topic history dates
    day_map = {}
    for d in days:
        day_map[d["date"]] = {"topics": d["topic_count"], "cards": d["card_count"]}
    for d in hist_dates:
        if d not in day_map:
            day_map[d] = {"topics": 1, "cards": 0}

    all_dates = sorted(day_map.keys())
    total_reviews = len(all_dates)

    # Calculate current streak + longest streak
    today = date.today()
    date_set = set(all_dates)
    current_streak = 0
    d = today
    while d.isoformat() in date_set:
        current_streak += 1
        d -= timedelta(days=1)

    longest_streak = 0
    streak = 0
    prev = None
    for ds in all_dates:
        dt = date.fromisoformat(ds)
        if prev and (dt - prev).days == 1:
            streak += 1
        else:
            streak = 1
        longest_streak = max(longest_streak, streak)
        prev = dt

    heatmap = [{"date": d, "topics": day_map[d]["topics"], "cards": day_map[d]["cards"]} for d in all_dates]

    total_cards = sum(day_map[d]["cards"] for d in all_dates)

    return jsonify({
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "total_review_days": total_reviews,
        "total_topics": len(hist_dates),
        "total_cards": total_cards,
        "heatmap": heatmap,
    })


@app.route("/api/stats/extended")
def stats_extended():
    """Return extended stats: per-topic retention data, rating distribution,
    and concept-level retention summary."""
    with get_db() as db:
        topics = db.execute(
            "SELECT id, name, a, k, review_count FROM topics ORDER BY review_count DESC LIMIT 10"
        ).fetchall()
        topic_list = []
        for t in topics:
            cdata = compute_topic_schedule_from_concepts(db, t["id"])
            topic_list.append({
                "name": t["name"],
                "a": t["a"],             # legacy topic-level
                "k": t["k"],             # legacy topic-level
                "review_count": t["review_count"],
                "concept_avg_retention": cdata.get("avg_retention"),
                "concept_min_retention": cdata.get("min_retention"),
                "concept_due_count": cdata.get("due_count", 0),
                "concept_total": cdata.get("total_concepts", 0),
            })

        # Rating distribution from session_logs
        ratings_rows = db.execute(
            "SELECT rating, COUNT(*) as cnt FROM session_logs GROUP BY rating"
        ).fetchall()
        ratings = {"complete": 0, "partial": 0, "failed": 0}
        for r in ratings_rows:
            if r["rating"] in ratings:
                ratings[r["rating"]] = r["cnt"]

        # Global concept retention distribution (new)
        all_concepts = db.execute("""
            SELECT cs.a, cs.k, cs.last_review FROM concept_states cs
        """).fetchall()
        concept_retention_buckets = {"0-20": 0, "20-40": 0, "40-60": 0,
                                     "60-80": 0, "80-100": 0}
        for c in all_concepts:
            ret = compute_concept_retention(c["a"], c["k"], c["last_review"]) * 100
            if ret < 20:
                concept_retention_buckets["0-20"] += 1
            elif ret < 40:
                concept_retention_buckets["20-40"] += 1
            elif ret < 60:
                concept_retention_buckets["40-60"] += 1
            elif ret < 80:
                concept_retention_buckets["60-80"] += 1
            else:
                concept_retention_buckets["80-100"] += 1

    return jsonify({
        "topics": topic_list,
        "ratings": ratings,
        "concept_retention_distribution": concept_retention_buckets,
        "total_concepts": len(all_concepts),
    })


@app.route("/api/topics/<int:tid>/history")
def topic_history(tid):
    """Return per-topic retention/curve history from session_logs.

    Also includes concept-level retention summary for the topic.
    """
    with get_db() as db:
        row = db.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        logs = db.execute(
            "SELECT date, rating, a_after, k_after FROM session_logs WHERE topic_id=? GROUP BY date ORDER BY date",
            (tid,),
        ).fetchall()
        cdata = compute_topic_schedule_from_concepts(db, tid)
        concept_details = get_concept_priority(db, tid)
    history = json.loads(row["history"])
    points = []
    for log in logs:
        points.append({
            "date": log["date"],
            "rating": log["rating"],
            "a": round(log["a_after"], 4) if log["a_after"] else None,
            "k": round(log["k_after"], 4) if log["k_after"] else None,
        })
    return jsonify({
        "id": tid,
        "name": row["name"],
        "history_dates": history,
        "curve_snapshots": points,
        # Concept-level data (new)
        "concept_summary": cdata,
        "concepts": concept_details,
    })


# ── Export / Import ───────────────────────────────────────────────────────

@app.route("/api/export")
def export_data():
    """Export all topics, cards, concepts, and related data as JSON."""
    with get_db() as db:
        topics = db.execute("SELECT * FROM topics ORDER BY id").fetchall()
        cards = db.execute("SELECT * FROM cards ORDER BY id").fetchall()
        logs = db.execute("SELECT * FROM session_logs ORDER BY id").fetchall()
        problems = db.execute("SELECT * FROM problems ORDER BY id").fetchall()
        attempts = db.execute("SELECT * FROM problem_attempts ORDER BY id").fetchall()
        concepts = db.execute("SELECT * FROM concepts ORDER BY id").fetchall()
        card_concepts = db.execute("SELECT * FROM card_concepts ORDER BY card_id, concept_id").fetchall()
        cstates = db.execute("SELECT * FROM concept_states ORDER BY id").fetchall()
    return jsonify({
        "version": 3,
        "exported": date.today().isoformat(),
        "topics": [{
            "id": t["id"], "name": t["name"], "learned_date": t["learned_date"],
            "a": t["a"], "k": t["k"], "last_review": t["last_review"],
            "next_review": t["next_review"], "review_count": t["review_count"],
            "history": json.loads(t["history"]), "tags": t["tags"],
        } for t in topics],
        "cards": [{
            "id": c["id"], "topic_id": c["topic_id"], "card_type": c["card_type"],
            "question": c["question"], "answer": c["answer"],
            "wrong_options": json.loads(c["wrong_options"]) if c["wrong_options"] else [],
            "box": c["box"], "fail_count": c["fail_count"],
            "success_count": c["success_count"], "last_rating": c["last_rating"],
        } for c in cards],
        "session_logs": [{
            "card_id": l["card_id"], "topic_id": l["topic_id"],
            "rating": l["rating"], "date": l["date"],
            "a_after": l["a_after"], "k_after": l["k_after"],
        } for l in logs],
        "problems": [{
            "id": p["id"], "topic_id": p["topic_id"],
            "title": p["title"], "prompt": p["prompt"],
            "hints": json.loads(p["hints"]) if p["hints"] else [],
            "final_answer": p["final_answer"],
            "full_solution": p["full_solution"],
            "skill_tag": p["skill_tag"],
            "difficulty": p["difficulty"],
        } for p in problems],
        "problem_attempts": [{
            "problem_id": a["problem_id"], "topic_id": a["topic_id"],
            "user_answer": a["user_answer"], "rating": a["rating"],
            "ai_explanation": a["ai_explanation"],
            "hints_used": a["hints_used"],
            "solution_viewed": a["solution_viewed"],
            "created_at": a["created_at"],
        } for a in attempts],
        # Concept-level data (new in v3)
        "concepts": [{
            "id": c["id"], "topic_id": c["topic_id"],
            "name": c["name"], "description": c["description"],
        } for c in concepts],
        "card_concepts": [{
            "card_id": cc["card_id"], "concept_id": cc["concept_id"],
            "weight": cc["weight"],
        } for cc in card_concepts],
        "concept_states": [{
            "concept_id": cs["concept_id"],
            "a": cs["a"], "k": cs["k"],
            "last_review": cs["last_review"], "next_review": cs["next_review"],
            "review_count": cs["review_count"],
            "success_count": cs["success_count"], "failure_count": cs["failure_count"],
            "history": json.loads(cs["history"]),
        } for cs in cstates],
    })


@app.route("/api/import", methods=["POST"])
def import_data():
    """Import topics, cards, and concepts from JSON export. Merges by topic name.

    Supports export versions 1, 2 (legacy, no concepts) and 3 (with concepts).
    For v1/v2 imports, _bootstrap_fallback_concepts will auto-create concept
    mappings on the next init_db() / migration pass.
    """
    data = request.json or {}
    if data.get("version") not in (1, 2, 3):
        return jsonify({"error": "Unsupported export version"}), 400
    imported_topics = 0
    imported_cards = 0
    imported_problems = 0
    imported_concepts = 0
    today_str = date.today().isoformat()
    with get_db() as db:
        # ID remapping: old_id → new_id for topics, cards, concepts
        topic_id_map = {}   # old_topic_id → new_topic_id
        card_id_map = {}    # old_card_id  → new_card_id
        concept_id_map = {} # old_concept_id → new_concept_id

        for t in data.get("topics", []):
            name = (t.get("name") or "").strip()
            if not name:
                continue
            existing = db.execute("SELECT id FROM topics WHERE name=?", (name,)).fetchone()
            if existing:
                # Map old id to existing so cards/concepts can still attach
                old_id = t.get("id")
                if old_id is not None:
                    topic_id_map[old_id] = existing["id"]
                continue  # skip duplicate topic creation
            hist = json.dumps(t.get("history", []))
            db.execute(
                """INSERT INTO topics (name, learned_date, a, k, last_review,
                   next_review, review_count, history, tags)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (name, t.get("learned_date", today_str),
                 t.get("a", A_INIT), t.get("k", K_INIT),
                 t.get("last_review"), t.get("next_review", today_str),
                 t.get("review_count", 0), hist, t.get("tags", "")),
            )
            new_tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            imported_topics += 1
            old_id = t.get("id")
            if old_id is not None:
                topic_id_map[old_id] = new_tid

        # Import cards (with ID remapping)
        for c in data.get("cards", []):
            old_tid = c.get("topic_id")
            new_tid = topic_id_map.get(old_tid)
            if new_tid is None:
                continue
            q = (c.get("question") or "").strip()
            if not q:
                continue
            db.execute(
                """INSERT INTO cards (topic_id, card_type, question, answer,
                   box, fail_count, success_count, last_rating, wrong_options)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (new_tid, c.get("card_type", "qa"), q,
                 c.get("answer", ""), c.get("box", 1),
                 c.get("fail_count", 0), c.get("success_count", 0),
                 c.get("last_rating"),
                 json.dumps(c.get("wrong_options", []))),
            )
            new_cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            old_cid = c.get("id")
            if old_cid is not None:
                card_id_map[old_cid] = new_cid
            imported_cards += 1

        # Import problems (with ID remapping)
        for p in data.get("problems", []):
            old_tid = p.get("topic_id")
            new_tid = topic_id_map.get(old_tid)
            if new_tid is None:
                continue
            title = (p.get("title") or "").strip()
            prompt = (p.get("prompt") or "").strip()
            if not title or not prompt:
                continue
            db.execute(
                """INSERT INTO problems
                   (topic_id, title, prompt, hints, final_answer, full_solution,
                    skill_tag, difficulty, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (new_tid, title, prompt,
                 json.dumps(p.get("hints", [])),
                 p.get("final_answer", ""),
                 p.get("full_solution", ""),
                 p.get("skill_tag", ""),
                 p.get("difficulty", 1),
                 today_str, today_str),
            )
            imported_problems += 1

        # --- Import concept-level data (v3+) ---
        if data.get("version", 0) >= 3:
            # Import concepts with ID remapping
            for con in data.get("concepts", []):
                old_tid = con.get("topic_id")
                new_tid = topic_id_map.get(old_tid)
                if new_tid is None:
                    continue
                cname = (con.get("name") or "").strip()
                if not cname:
                    continue
                # Skip if concept already exists for this topic
                existing_c = db.execute(
                    "SELECT id FROM concepts WHERE topic_id=? AND name=?",
                    (new_tid, cname),
                ).fetchone()
                if existing_c:
                    old_con_id = con.get("id")
                    if old_con_id is not None:
                        concept_id_map[old_con_id] = existing_c["id"]
                    continue
                db.execute(
                    "INSERT INTO concepts (topic_id, name, description) VALUES (?,?,?)",
                    (new_tid, cname, con.get("description")),
                )
                new_con_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                old_con_id = con.get("id")
                if old_con_id is not None:
                    concept_id_map[old_con_id] = new_con_id
                imported_concepts += 1

            # Import card_concepts with remapped IDs
            for cc in data.get("card_concepts", []):
                new_card = card_id_map.get(cc.get("card_id"))
                new_concept = concept_id_map.get(cc.get("concept_id"))
                if new_card is None or new_concept is None:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO card_concepts (card_id, concept_id, weight) VALUES (?,?,?)",
                    (new_card, new_concept, cc.get("weight", 1.0)),
                )

            # Import concept_states with remapped IDs
            for cs in data.get("concept_states", []):
                new_concept = concept_id_map.get(cs.get("concept_id"))
                if new_concept is None:
                    continue
                # Skip if state already exists
                if db.execute("SELECT id FROM concept_states WHERE concept_id=?", (new_concept,)).fetchone():
                    continue
                db.execute(
                    """INSERT INTO concept_states
                       (concept_id, a, k, last_review, next_review, review_count,
                        success_count, failure_count, history)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (new_concept, cs.get("a", A_INIT), cs.get("k", K_INIT),
                     cs.get("last_review"), cs.get("next_review", today_str),
                     cs.get("review_count", 0),
                     cs.get("success_count", 0), cs.get("failure_count", 0),
                     json.dumps(cs.get("history", []))),
                )

        # For v1/v2 imports, bootstrap fallback concepts for new cards
        if data.get("version", 0) < 3:
            _bootstrap_fallback_concepts(db)

    return jsonify({
        "ok": True,
        "imported_topics": imported_topics,
        "imported_cards": imported_cards,
        "imported_problems": imported_problems,
        "imported_concepts": imported_concepts,
    })


# ── PDF Import with AI Card Generation ────────────────────────────────────

@app.route("/api/estimate-pdf", methods=["POST"])
def estimate_pdf():
    """Extract text from PDF and return stats + recommended card count."""
    if PdfReader is None:
        return jsonify({"error": "PDF support unavailable (PyPDF2 not installed)"}), 503
    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a .pdf file"}), 400
    try:
        reader = PdfReader(io.BytesIO(f.read()))
        page_count = len(reader.pages)
    except Exception as exc:
        return jsonify({"error": f"Failed to read PDF: {exc}"}), 400
    if page_count == 0:
        return jsonify({"error": "PDF has no pages"}), 400
    # Sample up to 5 pages to estimate character count quickly
    sample_indices = list(range(min(5, page_count)))
    sample_chars = 0
    sample_has_text = False
    for i in sample_indices:
        try:
            txt = reader.pages[i].extract_text()
            if txt and txt.strip():
                sample_chars += len(txt.strip())
                sample_has_text = True
        except Exception:
            pass
    if not sample_has_text:
        return jsonify({"error": "Could not extract any text from this PDF"}), 400
    # Extrapolate total character count from sample
    avg_chars_per_page = sample_chars / len(sample_indices)
    char_count = int(avg_chars_per_page * page_count)
    # Heuristic: ~1 card per 400-600 chars of content, clamped to 5-30
    recommended = max(5, min(30, round(char_count / 500)))
    return jsonify({
        "ok": True,
        "page_count": page_count,
        "char_count": char_count,
        "recommended_cards": recommended,
    })

def _salvage_cards_json(raw):
    """Try to extract valid cards from truncated Claude JSON response."""
    import re
    idx = raw.find('"cards"')
    if idx == -1:
        return None
    bracket = raw.find('[', idx)
    if bracket == -1:
        return None
    cards = []
    depth = 0
    obj_start = None
    i = bracket + 1
    while i < len(raw):
        ch = raw[i]
        if ch == '{' and depth == 0:
            obj_start = i
            depth = 1
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    obj = json.loads(raw[obj_start:i+1])
                    if obj.get("question"):
                        cards.append(obj)
                except json.JSONDecodeError:
                    pass
                obj_start = None
        elif ch == '"':
            i += 1
            while i < len(raw) and raw[i] != '"':
                if raw[i] == '\\':
                    i += 1
                i += 1
        i += 1
    if not cards:
        return None
    topic_name = "Imported PDF Notes"
    tags = ""
    tn_match = re.search(r'"topic_name"\s*:\s*"([^"]+)"', raw[:500])
    if tn_match:
        topic_name = tn_match.group(1)
    tg_match = re.search(r'"tags"\s*:\s*"([^"]*)"', raw[:500])
    if tg_match:
        tags = tg_match.group(1)
    print(f"[PDF Import] Salvaged {len(cards)} complete cards from truncated response")
    return {"topic_name": topic_name, "tags": tags, "cards": cards}

@app.route("/api/import-pdf", methods=["POST"])
def import_pdf():
    """Accept a PDF upload, extract text, use Claude to generate a topic with flashcards."""
    if not ANTHROPIC_CLIENT:
        return jsonify({"error": "AI unavailable (anthropic not installed)"}), 503
    if PdfReader is None:
        return jsonify({"error": "PDF support unavailable (PyPDF2 not installed)"}), 503

    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a .pdf file"}), 400

    page_start = request.form.get("page_start", type=int) or 1
    page_end = request.form.get("page_end", type=int) or 9999
    user_guidance = (request.form.get("prompt") or "").strip()

    # Extract text from PDF (only selected page range)
    try:
        reader = PdfReader(io.BytesIO(f.read()))
        total_pages = len(reader.pages)
        page_start = max(1, min(page_start, total_pages))
        page_end = max(page_start, min(page_end, total_pages))
        pages_text = []
        for i in range(page_start - 1, page_end):
            txt = reader.pages[i].extract_text()
            if txt:
                pages_text.append(txt.strip())
        full_text = "\n\n".join(pages_text)
    except Exception as exc:
        return jsonify({"error": f"Failed to read PDF: {exc}"}), 400

    if not full_text.strip():
        return jsonify({"error": "Could not extract any text from the selected pages"}), 400

    # Get requested card count from form data
    num_cards = request.form.get("num_cards", type=int) or 0
    if num_cards < 5 or num_cards > 30:
        num_cards = max(5, min(30, round(len(full_text) / 500)))

    # Dynamic text limit based on page range
    max_chars = min(80000, max(12000, (page_end - page_start + 1) * 2000))
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n...(truncated)"

    system_prompt = (
        "You are a study-aid assistant. Given text from a student's notes, create a topic name, "
        "relevant tags, and a set of flashcards that thoroughly cover the key concepts.\n\n"

        "═══ CARD TYPES ═══\n"
        "There are exactly two card types. You MUST use both:\n\n"

        "1. recall (free-response) — 60-70%% of cards\n"
        "   The student types their answer from memory. No options are shown.\n"
        "   JSON: {\"card_type\": \"recall\", \"question\": \"...\", \"answer\": \"...\"}\n"
        "   Do NOT include wrong_options for recall cards.\n\n"

        "2. qa (multiple-choice) — 30-40%% of cards\n"
        "   The student picks from 4 options (1 correct + 3 wrong). Best for definitions, "
        "factual distinctions, formulas, or 'which of these' comparisons.\n"
        "   JSON: {\"card_type\": \"qa\", \"question\": \"...\", \"answer\": \"...\", "
        "\"wrong_options\": [\"wrong1\", \"wrong2\", \"wrong3\"]}\n"
        "   *** EVERY qa card MUST have a wrong_options array with EXACTLY 3 items. "
        "A qa card without wrong_options is INVALID and breaks the app. ***\n\n"

        "═══ CARD RATIO ENFORCEMENT ═══\n"
        "If you generate 10 cards, at least 3 MUST be qa with wrong_options.\n"
        "If you generate 15 cards, at least 5 MUST be qa with wrong_options.\n"
        "NEVER generate all recall cards. The student needs both formats.\n\n"

        "═══ QUESTION QUALITY RULES ═══\n"
        "Each card must test a DISTINCT concept. Never create two cards that test the same idea.\n\n"
        "Make every question SPECIFIC and PRECISE. Bad: 'What happens to the determinant when you swap rows?' "
        "Good: 'If you swap two rows of an $n \\times n$ matrix $A$, how does $\\det(A)$ change?'\n\n"
        "Vary question styles across these categories:\n"
        "- Compute: 'Calculate $\\det(B)$ where $B = ...$' or 'Evaluate $\\int_0^1 ...$'\n"
        "- Define/State: 'State the definition of ...' or 'What is the formal definition of ...?'\n"
        "- Derive/Prove: 'Prove that ...' or 'Derive the formula for ...'\n"
        "- Explain/Why: 'Why does ... imply ...?' or 'Explain the geometric meaning of ...'\n"
        "- Compare: 'What is the difference between ... and ...?'\n"
        "- Apply: 'Given ..., determine whether ...' or 'Use ... to find ...'\n"
        "Try to include at least 3 different styles.\n\n"

        "═══ ANSWER QUALITY RULES ═══\n"
        "Answers must be CONCISE — this is a flashcard, not a textbook.\n"
        "- For formulas: just the formula, e.g. '$\\det(A) = ad - bc$'\n"
        "- For definitions: one clear sentence\n"
        "- For proofs/derivations: key steps only, 2-4 lines max\n"
        "- For explanations: 1-2 sentences max\n"
        "NEVER pad answers with unnecessary context, preambles, or restatements of the question.\n"
        "Bad answer: 'The determinant of a 2x2 matrix is calculated by taking the product of the main "
        "diagonal entries and subtracting the product of the off-diagonal entries, giving us det(A) = ad - bc.'\n"
        "Good answer: '$\\det(A) = ad - bc$'\n\n"

        "═══ MULTIPLE-CHOICE (qa) RULES ═══\n"
        "- wrong_options MUST contain EXACTLY 3 strings — no more, no less\n"
        "- All 4 options (answer + 3 wrong) must be similar in length, detail, and style\n"
        "- Wrong options must be genuinely plausible: common misconceptions, related-but-incorrect terms, "
        "or close-but-wrong values. No absurd distractors.\n"
        "- The correct answer must NOT be distinguishable by being longer or more detailed\n"
        "- If the answer is a formula, make wrong options similar formulas with plausible errors\n"
        "- If the answer is a term, make wrong options related terms from the same domain\n\n"

        "═══ OUTPUT FORMAT ═══\n"
        "Respond with ONLY a JSON object. No markdown, no code fences, no commentary.\n"
        "{\n"
        "  \"topic_name\": \"Concise Topic Name\",\n"
        "  \"tags\": \"keyword1,keyword2,keyword3\",\n"
        "  \"cards\": [\n"
        "    {\"card_type\": \"recall\", \"question\": \"...\", \"answer\": \"...\"},\n"
        "    {\"card_type\": \"qa\", \"question\": \"...\", \"answer\": \"...\", "
        "\"wrong_options\": [\"...\", \"...\", \"...\"]},\n"
        "    ...\n"
        "  ]\n"
        "}\n\n"

        "Rules:\n"
        "- topic_name: concise descriptive name for the notes\n"
        "- tags: 2-5 relevant keywords separated by commas\n"
        "- Aim for 10-20 cards depending on content density\n"
        "- Every card must have non-empty question and answer\n"
        "- recall cards must NOT have wrong_options\n"
        "- qa cards MUST have wrong_options with exactly 3 entries\n\n"

        "═══ MANDATORY LaTeX NOTATION ═══\n"
        "Use LaTeX wrapped in $...$ (inline) or $$...$$ (display) for ALL of the following. "
        "NEVER write these as plain text:\n"
        "- Equations, formulas, expressions: $E = mc^2$, $\\frac{d}{dx}$\n"
        "- Variable names, even single letters: $x$, $n$, $A$, $T$\n"
        "- Greek letters: $\\alpha$, $\\beta$, $\\gamma$, $\\Delta$, $\\Sigma$, $\\lambda$, $\\pi$\n"
        "- Operators: $\\int$, $\\sum$, $\\prod$, $\\lim$, $\\frac{a}{b}$, $\\sqrt{x}$, $\\partial$, $\\nabla$\n"
        "- Subscripts/superscripts: $x_i$, $a_{n+1}$, $e^{i\\pi}$, $x^2$ (NOT x_i or x^2 in plain text)\n"
        "- Chemical formulas: $\\text{H}_2\\text{O}$, $\\text{CO}_2$ (NOT H2O)\n"
        "- Physical quantities: $F = 9.8\\,\\text{N}$, $c = 3 \\times 10^8\\,\\text{m/s}$\n"
        "- Matrices: $\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}$\n"
        "- Vectors: $\\vec{F}$, $\\hat{n}$\n"
        "- Set notation: $\\in$, $\\subset$, $\\cup$, $\\cap$, $\\mathbb{R}$, $\\emptyset$\n"
        "- Logic: $\\forall$, $\\exists$, $\\Rightarrow$, $\\iff$, $\\neg$\n"
        "- Comparisons: $\\neq$, $\\leq$, $\\geq$, $\\approx$\n"
        "- Functions: $f(x)$, $\\sin(\\theta)$, $\\det(A)$\n\n"

        "═══ EXAMPLE CARDS ═══\n"
        "GOOD recall card:\n"
        "{\"card_type\": \"recall\", \"question\": \"State the formula for the determinant of a "
        "$2 \\\\times 2$ matrix $A = \\\\begin{pmatrix} a & b \\\\\\\\ c & d \\\\end{pmatrix}$.\", "
        "\"answer\": \"$\\\\det(A) = ad - bc$\"}\n\n"
        "GOOD qa card:\n"
        "{\"card_type\": \"qa\", \"question\": \"Which expression gives $\\\\det(AB)$ for square matrices "
        "$A$ and $B$?\", \"answer\": \"$\\\\det(A) \\\\cdot \\\\det(B)$\", "
        "\"wrong_options\": [\"$\\\\det(A) + \\\\det(B)$\", \"$\\\\det(A + B)$\", "
        "\"$\\\\det(A) - \\\\det(B)$\"]}\n\n"
        "BAD (vague question): 'What about determinants and row operations?'\n"
        "BAD (verbose answer): 'The determinant is a scalar value that can be computed from a square matrix "
        "and it tells us many things including...'\n"
        "BAD (qa without wrong_options): {\"card_type\": \"qa\", \"question\": \"...\", \"answer\": \"...\"}\n"
    )

    user_prompt = (
        "Here are the student's notes. Create a topic with flashcards from them:\n\n"
        "--- NOTES START ---\n"
        + full_text + "\n"
        "--- NOTES END ---\n\n"
    )
    if user_guidance:
        user_prompt += (
            "═══ STUDENT INSTRUCTIONS ═══\n"
            + user_guidance + "\n\n"
            "Follow the student's instructions above when choosing which concepts to cover "
            "and how to frame the questions.\n\n"
        )
    user_prompt += (
        "Generate EXACTLY " + str(num_cards) + " cards in the JSON. Remember:\n"
        "- At least 30%% of cards MUST be card_type 'qa' with a wrong_options array of exactly 3 items\n"
        "- Every card must test a DISTINCT concept — no overlapping or redundant cards\n"
        "- Keep answers SHORT (flashcard-length, not paragraph-length)\n"
        "- Use $...$ LaTeX for all math, variables, and symbols"
    )

    try:
        print(f"[PDF Import] Sending {len(full_text)} chars to Claude...")
        message = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=12000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()
        print(f"[PDF Import] Got response ({len(raw)} chars, stop={message.stop_reason})")
        # Strip markdown fences if Claude adds them despite instructions
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        result = json.loads(raw)
    except json.JSONDecodeError as jde:
        print(f"[PDF Import] JSON parse error: {jde}\\nRaw: {raw[:500]}")
        # Attempt to salvage truncated JSON — extract complete card objects
        result = _salvage_cards_json(raw)
        if result is None:
            return jsonify({"error": "AI returned invalid JSON. Please try again."}), 502
    except Exception as exc:
        print(f"[PDF Import] Error: {type(exc).__name__}: {exc}")
        return jsonify({"error": f"AI request failed: {exc}"}), 502

    topic_name = (result.get("topic_name") or "Imported PDF Notes").strip()
    tags = (result.get("tags") or "").strip()
    cards_data = result.get("cards", [])
    if not isinstance(cards_data, list) or len(cards_data) == 0:
        return jsonify({"error": "AI did not generate any cards. Please try again."}), 502

    # Insert topic and cards into DB
    today = date.today().isoformat()
    nr = next_review_date(A_INIT, K_INIT)
    card_count = 0
    with get_db() as db:
        # Check for duplicate topic name and make unique
        existing = db.execute("SELECT id FROM topics WHERE name=?", (topic_name,)).fetchone()
        if existing:
            topic_name = topic_name + " (" + today + ")"

        db.execute(
            "INSERT INTO topics (name, learned_date, a, k, next_review, tags) VALUES (?,?,?,?,?,?)",
            (topic_name, today, A_INIT, K_INIT, nr, tags),
        )
        new_tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        for c in cards_data:
            q = (c.get("question") or "").strip()
            a = (c.get("answer") or "").strip()
            ct = c.get("card_type", "qa")
            if ct not in ("qa", "recall"):
                ct = "qa"
            if not q:
                continue
            wo = c.get("wrong_options") or []
            wo_json = json.dumps(wo) if isinstance(wo, list) else "[]"
            db.execute(
                "INSERT INTO cards (topic_id, card_type, question, answer, wrong_options) VALUES (?,?,?,?,?)",
                (new_tid, ct, q, a, wo_json),
            )
            card_count += 1

    return jsonify({
        "ok": True,
        "topic_id": new_tid,
        "topic_name": topic_name,
        "card_count": card_count,
        "tags": tags,
    })


# ── Prompt → Flashcards ──────────────────────────────────────────────────

@app.route("/api/generate-cards-prompt", methods=["POST"])
def generate_cards_prompt():
    """Accept a text prompt and use Claude to generate a topic with flashcards."""
    if not ANTHROPIC_CLIENT:
        return jsonify({"error": "AI unavailable (anthropic not installed)"}), 503

    body = request.get_json(force=True, silent=True) or {}
    prompt_text = (body.get("prompt") or "").strip()
    if not prompt_text:
        return jsonify({"error": "Please enter a prompt describing what to study"}), 400

    num_cards = body.get("num_cards", 15)
    if not isinstance(num_cards, int) or num_cards < 5 or num_cards > 30:
        num_cards = 15
    topic_name_hint = (body.get("topic_name") or "").strip()

    system_prompt = (
        "You are a study-aid assistant. Given a student's description of what they want to study, "
        "create a topic name, relevant tags, and a set of flashcards that thoroughly cover the key concepts.\n\n"

        "═══ CARD TYPES ═══\n"
        "There are exactly two card types. You MUST use both:\n\n"

        "1. recall (free-response) — 60-70%% of cards\n"
        "   The student types their answer from memory. No options are shown.\n"
        "   JSON: {\"card_type\": \"recall\", \"question\": \"...\", \"answer\": \"...\"}\n"
        "   Do NOT include wrong_options for recall cards.\n\n"

        "2. qa (multiple-choice) — 30-40%% of cards\n"
        "   The student picks from 4 options (1 correct + 3 wrong). Best for definitions, "
        "factual distinctions, formulas, or 'which of these' comparisons.\n"
        "   JSON: {\"card_type\": \"qa\", \"question\": \"...\", \"answer\": \"...\", "
        "\"wrong_options\": [\"wrong1\", \"wrong2\", \"wrong3\"]}\n"
        "   *** EVERY qa card MUST have a wrong_options array with EXACTLY 3 items. ***\n\n"

        "═══ CARD RATIO ENFORCEMENT ═══\n"
        "If you generate 10 cards, at least 3 MUST be qa with wrong_options.\n"
        "If you generate 15 cards, at least 5 MUST be qa with wrong_options.\n"
        "NEVER generate all recall cards. The student needs both formats.\n\n"

        "═══ QUESTION QUALITY RULES ═══\n"
        "Each card must test a DISTINCT concept. Never create two cards that test the same idea.\n"
        "Make every question SPECIFIC and PRECISE.\n"
        "Vary question styles: Compute, Define/State, Derive/Prove, Explain/Why, Compare, Apply.\n"
        "Try to include at least 3 different styles.\n\n"

        "═══ ANSWER QUALITY RULES ═══\n"
        "Answers must be CONCISE — this is a flashcard, not a textbook.\n"
        "- For formulas: just the formula\n"
        "- For definitions: one clear sentence\n"
        "- For proofs/derivations: key steps only, 2-4 lines max\n"
        "- For explanations: 1-2 sentences max\n\n"

        "═══ MULTIPLE-CHOICE (qa) RULES ═══\n"
        "- wrong_options MUST contain EXACTLY 3 strings\n"
        "- All 4 options must be similar in length, detail, and style\n"
        "- Wrong options must be genuinely plausible\n\n"

        "═══ OUTPUT FORMAT ═══\n"
        "Respond with ONLY a JSON object. No markdown, no code fences, no commentary.\n"
        "{\n"
        "  \"topic_name\": \"Concise Topic Name\",\n"
        "  \"tags\": \"keyword1,keyword2,keyword3\",\n"
        "  \"cards\": [\n"
        "    {\"card_type\": \"recall\", \"question\": \"...\", \"answer\": \"...\"},\n"
        "    {\"card_type\": \"qa\", \"question\": \"...\", \"answer\": \"...\", "
        "\"wrong_options\": [\"...\", \"...\", \"...\"]},\n"
        "    ...\n"
        "  ]\n"
        "}\n\n"

        "═══ MANDATORY LaTeX NOTATION ═══\n"
        "Use LaTeX wrapped in $...$ (inline) or $$...$$ (display) for ALL math, variables, "
        "greek letters, operators, subscripts/superscripts, formulas, etc.\n"
    )

    topic_hint = ""
    if topic_name_hint:
        topic_hint = f'\nThe student wants the topic named: "{topic_name_hint}". Use this as the topic_name.\n'

    user_prompt = (
        "The student wants to study the following:\n\n"
        "--- PROMPT START ---\n"
        + prompt_text + "\n"
        "--- PROMPT END ---\n\n"
        + topic_hint +
        "Generate EXACTLY " + str(num_cards) + " cards in the JSON. Remember:\n"
        "- At least 30% of cards MUST be card_type 'qa' with a wrong_options array of exactly 3 items\n"
        "- Every card must test a DISTINCT concept\n"
        "- Keep answers SHORT (flashcard-length)\n"
        "- Use $...$ LaTeX for all math, variables, and symbols"
    )

    try:
        message = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=12000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = _salvage_cards_json(raw)
        if result is None:
            return jsonify({"error": "AI returned invalid JSON. Please try again."}), 502
    except Exception as exc:
        return jsonify({"error": f"AI request failed: {exc}"}), 502

    topic_name = (result.get("topic_name") or topic_name_hint or "Generated Cards").strip()
    tags = (result.get("tags") or "").strip()
    cards_data = result.get("cards", [])
    if not isinstance(cards_data, list) or len(cards_data) == 0:
        return jsonify({"error": "AI did not generate any cards. Please try again."}), 502

    today_str = date.today().isoformat()
    nr = next_review_date(A_INIT, K_INIT)
    card_count = 0
    with get_db() as db:
        existing = db.execute("SELECT id FROM topics WHERE name=?", (topic_name,)).fetchone()
        if existing:
            topic_name = topic_name + " (" + today_str + ")"
        db.execute(
            "INSERT INTO topics (name, learned_date, a, k, next_review, tags) VALUES (?,?,?,?,?,?)",
            (topic_name, today_str, A_INIT, K_INIT, nr, tags),
        )
        new_tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        for c in cards_data:
            q = (c.get("question") or "").strip()
            a_val = (c.get("answer") or "").strip()
            ct = c.get("card_type", "qa")
            if ct not in ("qa", "recall"):
                ct = "qa"
            if not q:
                continue
            wo = c.get("wrong_options") or []
            wo_json = json.dumps(wo) if isinstance(wo, list) else "[]"
            db.execute(
                "INSERT INTO cards (topic_id, card_type, question, answer, wrong_options) VALUES (?,?,?,?,?)",
                (new_tid, ct, q, a_val, wo_json),
            )
            card_count += 1

    return jsonify({
        "ok": True,
        "topic_id": new_tid,
        "topic_name": topic_name,
        "card_count": card_count,
        "tags": tags,
    })


# ── PDF → Practice Problems ──────────────────────────────────────────────

def _salvage_problems_json(raw):
    """Try to extract valid problems from truncated Claude JSON response."""
    import re
    # Try to find the problems array and extract complete problem objects
    # Look for "problems" key and collect complete {...} objects
    idx = raw.find('"problems"')
    if idx == -1:
        return None
    # Find the opening bracket of the array
    bracket = raw.find('[', idx)
    if bracket == -1:
        return None
    # Walk through and collect complete JSON objects within the array
    problems = []
    depth = 0
    obj_start = None
    i = bracket + 1
    while i < len(raw):
        ch = raw[i]
        if ch == '{' and depth == 0:
            obj_start = i
            depth = 1
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    obj = json.loads(raw[obj_start:i+1])
                    if obj.get("prompt") or obj.get("title"):
                        problems.append(obj)
                except json.JSONDecodeError:
                    pass
                obj_start = None
        elif ch == '"':
            # Skip string contents (handles escaped quotes)
            i += 1
            while i < len(raw) and raw[i] != '"':
                if raw[i] == '\\':
                    i += 1
                i += 1
        i += 1

    if not problems:
        return None
    # Try to extract doc_type and topic_name from the beginning of the response
    doc_type = "notes"
    topic_name = "Imported Problems"
    tags = ""
    dt_match = re.search(r'"doc_type"\s*:\s*"(\w+)"', raw[:500])
    if dt_match:
        doc_type = dt_match.group(1)
    tn_match = re.search(r'"topic_name"\s*:\s*"([^"]+)"', raw[:500])
    if tn_match:
        topic_name = tn_match.group(1)
    tg_match = re.search(r'"tags"\s*:\s*"([^"]*)"', raw[:500])
    if tg_match:
        tags = tg_match.group(1)
    print(f"[PDF Problems] Salvaged {len(problems)} complete problems from truncated response")
    return {"doc_type": doc_type, "topic_name": topic_name, "tags": tags, "problems": problems}

@app.route("/api/import-pdf-problems", methods=["POST"])
def import_pdf_problems():
    """Accept a PDF, use Claude to generate practice problems for an existing topic."""
    if not ANTHROPIC_CLIENT:
        return jsonify({"error": "AI unavailable"}), 503
    if PdfReader is None:
        return jsonify({"error": "PDF support unavailable (PyPDF2 not installed)"}), 503

    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a .pdf file"}), 400

    topic_id = request.form.get("topic_id", type=int)
    new_topic_name = request.form.get("new_topic_name", "").strip()
    num_problems = request.form.get("num_problems", type=int) or 5
    if num_problems < 1 or num_problems > 20:
        num_problems = max(1, min(20, num_problems))
    page_start = request.form.get("page_start", type=int) or 1
    page_end = request.form.get("page_end", type=int) or 9999
    user_instructions = request.form.get("prompt", "").strip()

    try:
        reader = PdfReader(io.BytesIO(f.read()))
        total_pages = len(reader.pages)
        # Clamp page range to valid bounds (1-indexed)
        page_start = max(1, min(page_start, total_pages))
        page_end = max(page_start, min(page_end, total_pages))
        pages_text = []
        for i in range(page_start - 1, page_end):
            txt = reader.pages[i].extract_text()
            if txt:
                pages_text.append(txt.strip())
        full_text = "\n\n".join(pages_text)
    except Exception as exc:
        return jsonify({"error": f"Failed to read PDF: {exc}"}), 400

    if not full_text.strip():
        return jsonify({"error": "Could not extract any text from the selected pages"}), 400

    # Allow more text for targeted page ranges (up to ~50 pages worth)
    max_chars = min(80000, max(12000, (page_end - page_start + 1) * 2000))
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n...(truncated)"

    system_prompt = (
        "You are a study assistant that creates practice problems from uploaded documents.\n\n"

        "═══ STEP 1: DETECT DOCUMENT TYPE ═══\n"
        "First, determine what kind of document this is:\n"
        "- TEXTBOOK: Contains chapters, sections, definitions, theorems, AND existing "
        "exercises/problems/worked examples. Usually has structured formatting, numbered "
        "problems, or 'Exercise'/'Problem'/'Example' sections.\n"
        "- NOTES: Lecture notes, study notes, summaries, slides, or any document WITHOUT "
        "pre-existing practice problems.\n\n"

        "Set the 'doc_type' field to 'textbook' or 'notes' in your response.\n\n"

        "═══ STEP 2: CREATE PROBLEMS ═══\n"
        "Based on doc_type, follow the appropriate strategy:\n\n"

        "IF TEXTBOOK:\n"
        "- EXTRACT existing exercises, problems, and worked examples found in the text\n"
        "- Reproduce them faithfully — keep the original wording, notation, and numbering\n"
        "- For each extracted problem, set 'source' to 'extracted'\n"
        "- If the extracted problems are fewer than the requested count, GENERATE "
        "additional original problems inspired by the textbook content to reach the target\n"
        "- For generated problems, set 'source' to 'generated'\n\n"

        "IF NOTES:\n"
        "- GENERATE original practice problems that test understanding of the material\n"
        "- Problems should require multi-step reasoning, not just recall\n"
        "- Set 'source' to 'generated' for all problems\n\n"

        "═══ PROBLEM STRUCTURE ═══\n"
        "Each problem must have:\n"
        "- title: short descriptive title (5-10 words)\n"
        "- prompt: the full problem statement (specific, precise, self-contained)\n"
        "- skill_tag: one skill keyword (e.g. 'integration', 'proof', 'definition')\n"
        "- difficulty: 1 (easy), 2 (medium), or 3 (hard)\n"
        "- source: 'extracted' (from textbook) or 'generated' (AI-created)\n\n"
        "DO NOT include hints, final_answer, or full_solution — those are generated separately on demand.\n\n"

        "═══ PROBLEM QUALITY RULES ═══\n"
        "- Each problem must test a DISTINCT concept or skill\n"
        "- Mix difficulty levels: ~30% easy, ~50% medium, ~20% hard\n"
        "- Prompts must be SPECIFIC and PRECISE, not vague\n\n"

        "═══ MANDATORY LaTeX NOTATION ═══\n"
        "Use $...$ (inline) or $$...$$ (display) for ALL math, variables, "
        "formulas, Greek letters, operators, etc.\n\n"

        "═══ OUTPUT FORMAT ═══\n"
        "Respond with ONLY a JSON object. No markdown, no code fences.\n"
        "{\n"
        '  "doc_type": "textbook" or "notes",\n'
        '  "topic_name": "...",\n'
        '  "tags": "keyword1,keyword2",\n'
        '  "problems": [\n'
        '    {"title": "...", "prompt": "...", '
        '"skill_tag": "...", "difficulty": 2, "source": "extracted"},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
    )

    user_prompt = (
        "Here is the uploaded document. Analyze it and create practice problems:\n\n"
        "--- DOCUMENT START ---\n"
        + full_text + "\n"
        "--- DOCUMENT END ---\n\n"
        "Target: EXACTLY " + str(num_problems) + " problems total.\n"
        "1. First, determine if this is a TEXTBOOK (has existing exercises/problems) or NOTES\n"
        "2. If textbook: extract existing problems first, then generate more to reach " + str(num_problems) + "\n"
        "3. If notes: generate all " + str(num_problems) + " problems from the content\n"
        "- Mix difficulty levels\n"
        "- Each problem must test a DISTINCT skill\n"
        "- Use $...$ LaTeX for all math\n"
        "- Set 'source' to 'extracted' or 'generated' for each problem"
    )
    if user_instructions:
        user_prompt += (
            "\n\n═══ STUDENT INSTRUCTIONS ═══\n"
            "The student provided these additional instructions. Follow them carefully:\n"
            + user_instructions
        )

    try:
        print(f"[PDF Problems] Sending {len(full_text)} chars to Claude...")
        message = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=6000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()
        print(f"[PDF Problems] Got response ({len(raw)} chars, stop={message.stop_reason})")
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # Attempt to salvage truncated JSON — find last complete problem object
            result = _salvage_problems_json(raw)
            if result is None:
                return jsonify({"error": "AI returned invalid JSON. Please try again."}), 502
    except Exception as exc:
        return jsonify({"error": f"AI request failed: {exc}"}), 502

    problems_data = result.get("problems", [])
    if not isinstance(problems_data, list) or not problems_data:
        return jsonify({"error": "AI did not generate any problems. Try again."}), 502

    doc_type = result.get("doc_type", "notes")
    today_str = date.today().isoformat()
    nr = next_review_date(A_INIT, K_INIT)
    problem_count = 0
    extracted_count = 0
    generated_count = 0

    with get_db() as db:
        # If no topic_id given, create a new topic from the AI's suggested name
        if not topic_id:
            topic_name = new_topic_name or (result.get("topic_name") or "Imported Problems").strip()
            tags = (result.get("tags") or "").strip()
            existing = db.execute("SELECT id FROM topics WHERE name=?", (topic_name,)).fetchone()
            if existing:
                topic_name = topic_name + " (" + today_str + ")"
            db.execute(
                "INSERT INTO topics (name, learned_date, a, k, next_review, tags) VALUES (?,?,?,?,?,?)",
                (topic_name, today_str, A_INIT, K_INIT, nr, tags),
            )
            topic_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        for p in problems_data:
            title = (p.get("title") or "").strip()
            prompt = (p.get("prompt") or "").strip()
            if not title or not prompt:
                continue
            source = (p.get("source") or "generated").strip().lower()
            if source not in ("extracted", "generated"):
                source = "generated"
            db.execute(
                """INSERT INTO problems
                   (topic_id, title, prompt,
                    skill_tag, difficulty, source, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (topic_id, title, prompt,
                 (p.get("skill_tag") or "").strip(),
                 int(p.get("difficulty", 1)),
                 source,
                 today_str, today_str),
            )
            problem_count += 1
            if source == "extracted":
                extracted_count += 1
            else:
                generated_count += 1

    # Get topic name for response
    with get_db() as db:
        trow = db.execute("SELECT name FROM topics WHERE id=?", (topic_id,)).fetchone()
        tname = trow["name"] if trow else "Unknown"

    return jsonify({
        "ok": True,
        "topic_id": topic_id,
        "topic_name": tname,
        "problem_count": problem_count,
        "extracted_count": extracted_count,
        "generated_count": generated_count,
        "doc_type": doc_type,
    })


@app.route("/api/generate-problems-prompt", methods=["POST"])
def generate_problems_prompt():
    """Accept a text prompt and use Claude to generate practice problems."""
    if not ANTHROPIC_CLIENT:
        return jsonify({"error": "AI unavailable"}), 503

    body = request.get_json(force=True, silent=True) or {}
    prompt_text = (body.get("prompt") or "").strip()
    if not prompt_text:
        return jsonify({"error": "Please enter a prompt describing what problems to generate"}), 400

    num_problems = body.get("num_problems", 5)
    if not isinstance(num_problems, int) or num_problems < 1 or num_problems > 20:
        num_problems = 5
    topic_id = body.get("topic_id")
    if topic_id is not None:
        topic_id = int(topic_id) if str(topic_id).isdigit() else None
    new_topic_name = (body.get("new_topic_name") or "").strip()

    system_prompt = (
        "You are a study assistant that creates practice problems based on a student's description.\n\n"

        "═══ PROBLEM STRUCTURE ═══\n"
        "Each problem must have:\n"
        "- title: short descriptive title (5-10 words)\n"
        "- prompt: the full problem statement (specific, precise, self-contained)\n"
        "- skill_tag: one skill keyword (e.g. 'integration', 'proof', 'definition')\n"
        "- difficulty: 1 (easy), 2 (medium), or 3 (hard)\n\n"
        "DO NOT include hints, final_answer, or full_solution — those are generated separately on demand.\n\n"

        "═══ PROBLEM QUALITY RULES ═══\n"
        "- Each problem must test a DISTINCT concept or skill\n"
        "- Mix difficulty levels: ~30% easy, ~50% medium, ~20% hard\n"
        "- Prompts must be SPECIFIC and PRECISE, not vague\n"
        "- Problems should require multi-step reasoning, not just recall\n"
        "- Vary problem styles: Compute, Prove, Derive, Explain, Compare, Apply\n\n"

        "═══ MANDATORY LaTeX NOTATION ═══\n"
        "Use $...$ (inline) or $$...$$ (display) for ALL math, variables, "
        "formulas, Greek letters, operators, etc.\n\n"

        "═══ OUTPUT FORMAT ═══\n"
        "Respond with ONLY a JSON object. No markdown, no code fences.\n"
        "{\n"
        '  "topic_name": "Concise Topic Name",\n'
        '  "tags": "keyword1,keyword2",\n'
        '  "problems": [\n'
        '    {"title": "...", "prompt": "...", '
        '"skill_tag": "...", "difficulty": 2},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
    )

    user_prompt = (
        "The student wants practice problems on the following:\n\n"
        "--- PROMPT START ---\n"
        + prompt_text + "\n"
        "--- PROMPT END ---\n\n"
        "Generate EXACTLY " + str(num_problems) + " problems in the JSON. Remember:\n"
        "- Each problem must test a DISTINCT skill\n"
        "- Mix difficulty levels\n"
        "- Use $...$ LaTeX for all math, variables, and symbols\n"
        "- Make problems specific and multi-step"
    )

    try:
        print(f"[Prompt Problems] Sending prompt to Claude ({len(prompt_text)} chars)...")
        message = ANTHROPIC_CLIENT.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=6000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()
        print(f"[Prompt Problems] Got response ({len(raw)} chars, stop={message.stop_reason})")
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = _salvage_problems_json(raw)
            if result is None:
                return jsonify({"error": "AI returned invalid JSON. Please try again."}), 502
    except Exception as exc:
        return jsonify({"error": f"AI request failed: {exc}"}), 502

    problems_data = result.get("problems", [])
    if not isinstance(problems_data, list) or not problems_data:
        return jsonify({"error": "AI did not generate any problems. Try again."}), 502

    today_str = date.today().isoformat()
    nr = next_review_date(A_INIT, K_INIT)
    problem_count = 0

    with get_db() as db:
        if not topic_id:
            topic_name = new_topic_name or (result.get("topic_name") or "Generated Problems").strip()
            tags = (result.get("tags") or "").strip()
            existing = db.execute("SELECT id FROM topics WHERE name=?", (topic_name,)).fetchone()
            if existing:
                topic_name = topic_name + " (" + today_str + ")"
            db.execute(
                "INSERT INTO topics (name, learned_date, a, k, next_review, tags) VALUES (?,?,?,?,?,?)",
                (topic_name, today_str, A_INIT, K_INIT, nr, tags),
            )
            topic_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        for p in problems_data:
            title = (p.get("title") or "").strip()
            prompt = (p.get("prompt") or "").strip()
            if not title or not prompt:
                continue
            db.execute(
                """INSERT INTO problems
                   (topic_id, title, prompt,
                    skill_tag, difficulty, source, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (topic_id, title, prompt,
                 (p.get("skill_tag") or "").strip(),
                 int(p.get("difficulty", 1)),
                 "generated",
                 today_str, today_str),
            )
            problem_count += 1

    with get_db() as db:
        trow = db.execute("SELECT name FROM topics WHERE id=?", (topic_id,)).fetchone()
        tname = trow["name"] if trow else "Unknown"

    return jsonify({
        "ok": True,
        "topic_id": topic_id,
        "topic_name": tname,
        "problem_count": problem_count,
    })


# ── Notifications ─────────────────────────────────────────────────────────

_notified_today = {"date": "", "ids": set()}

def check_notifications():
    today = date.today().isoformat()
    if _notified_today["date"] != today:
        _notified_today["date"] = today
        _notified_today["ids"] = set()
    with get_db() as db:
        due = db.execute(
            "SELECT id, name FROM topics WHERE next_review <= ?", (today,)
        ).fetchall()
    if not due:
        return
    new_due = [r for r in due if r["id"] not in _notified_today["ids"]]
    if not new_due:
        return
    for r in new_due:
        _notified_today["ids"].add(r["id"])
    names = [r["name"] for r in new_due]
    print(f"\nReviews due: {', '.join(names)}\n")
    try:
        from plyer import notification
        notification.notify(
            title="Study Tracker — Reviews Due",
            message=f"{len(names)} topic(s): {', '.join(names[:3])}",
            timeout=10,
        )
    except Exception:
        pass   # plyer not installed or platform unsupported — no problem


def _reminder_loop():
    """Background thread: check for due topics every 5 minutes."""
    while True:
        try:
            check_notifications()
        except Exception:
            pass
        threading.Event().wait(300)


@app.route("/api/due-count")
def due_count():
    today = date.today().isoformat()
    with get_db() as db:
        date_overdue = db.execute(
            "SELECT COUNT(*) as c FROM topics WHERE next_review < ?", (today,)
        ).fetchone()["c"]
        due_today = db.execute(
            "SELECT COUNT(*) as c FROM topics WHERE next_review = ?", (today,)
        ).fetchone()["c"]
        # Also count topics whose retention has dropped below 40%
        low_ret_extra = 0
        for r in db.execute(
            "SELECT a, k, last_review, learned_date, next_review FROM topics WHERE next_review >= ?",
            (today,),
        ).fetchall():
            ref = r["last_review"] or r["learned_date"]
            days_elapsed = (date.today() - date.fromisoformat(ref)).days
            if retention(r["a"], r["k"], days_elapsed) < 0.40:
                low_ret_extra += 1
                if r["next_review"] == today:
                    due_today -= 1  # move from due to overdue
        overdue = date_overdue + low_ret_extra
        names = [r["name"] for r in db.execute(
            "SELECT name FROM topics WHERE next_review <= ? LIMIT 5", (today,)
        ).fetchall()]
    return jsonify({"overdue": overdue, "due_today": due_today, "total": overdue + due_today, "names": names})


# ── HTML ──────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Study Tracker</title>
<script>(function(){var t=localStorage.getItem('theme');if(t==='dark'||(t!=='light'&&window.matchMedia('(prefers-color-scheme:dark)').matches))document.documentElement.dataset.theme='dark';})()</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/mathlive"></script>
<script>var pdfjsLib; import('https://cdn.jsdelivr.net/npm/pdfjs-dist@4.4.168/build/pdf.min.mjs').then(m=>{pdfjsLib=m;pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdn.jsdelivr.net/npm/pdfjs-dist@4.4.168/build/pdf.worker.min.mjs';});</script>
<script>
/* Build NxM matrix LaTeX for a given environment */
function buildMatrixLatex(r, c, env) {
  var rows = [];
  for (var i = 0; i < r; i++) {
    var cols = [];
    for (var j = 0; j < c; j++) cols.push('\\placeholder{}');
    rows.push(cols.join(' & '));
  }
  return '\\begin{' + env + '} ' + rows.join(' \\\\ ') + ' \\end{' + env + '}';
}
/* Insert custom-sized matrix into the active math-field */
function insertCustomMatrix() {
  var size = prompt('Enter matrix size as rows x cols (e.g. 3x4):');
  if (!size) return;
  var parts = size.toLowerCase().split('x');
  var r = parseInt(parts[0],10), c = parseInt(parts[1],10);
  if (!r || !c || r < 1 || c < 1 || r > 20 || c > 20) { alert('Invalid size. Use format like 3x4 (max 20x20).'); return; }
  var type = prompt('Bracket type?  ( ) = pmatrix,  [ ] = bmatrix,  | | = vmatrix,  { } = Bmatrix\nEnter: p, b, v, or B', 'b');
  var env = type === 'p' ? 'pmatrix' : type === 'v' ? 'vmatrix' : type === 'B' ? 'Bmatrix' : 'bmatrix';
  var rows = [];
  for (var i = 0; i < r; i++) {
    var cols = [];
    for (var j = 0; j < c; j++) cols.push('\\placeholder{}');
    rows.push(cols.join(' & '));
  }
  var latex = '\\begin{' + env + '} ' + rows.join(' \\\\ ') + ' \\end{' + env + '}';
  var mf = document.querySelector('math-field:focus-within') || document.querySelector('math-field:focus');
  if (!mf) { var all = document.querySelectorAll('math-field'); mf = all[all.length-1]; }
  if (mf && mf.executeCommand) {
    mf.executeCommand(['insert', latex]);
  }
}
window.addEventListener('load', function() {
  if (typeof mathVirtualKeyboard === 'undefined') return;
  document.addEventListener('pointerup', function(e) {
    var t = e.target;
    while (t && t !== document) {
      if (t.textContent && t.textContent.trim() === 'N\u00D7M') { setTimeout(insertCustomMatrix, 50); return; }
      t = t.parentElement;
    }
  });
  mathVirtualKeyboard.layouts = [
    'numeric', 'symbols', 'alphabetic', 'greek',
    {label:'\u23A1 \u23A4', tooltip:'Matrices & brackets', rows:[
      [{latex:'\\begin{pmatrix} \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} \\end{pmatrix}', label:'(\u22C5\u22C5)', class:'small'},
       {latex:'\\begin{bmatrix} \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} \\end{bmatrix}', label:'[\u22C5\u22C5]', class:'small'},
       {latex:'\\begin{vmatrix} \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} \\end{vmatrix}', label:'|\u22C5\u22C5|', class:'small'},
       {latex:'\\begin{Bmatrix} \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} \\end{Bmatrix}', label:'{\u22C5\u22C5}', class:'small'}],
      [{latex:'\\begin{pmatrix} \\placeholder{} & \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} & \\placeholder{} \\end{pmatrix}', label:'3\u00D73', class:'small'},
       {class:'action', label:'N\u00D7M', command:['performWithFeedback','insertCustomMatrix()']},
       {latex:'\\vec{\\placeholder{}}', label:'v\u20D7'}, {latex:'\\hat{\\placeholder{}}', label:'v\u0302'}, {latex:'\\dot{\\placeholder{}}', label:'v\u0307'}, {latex:'\\ddot{\\placeholder{}}', label:'v\u0308'}],
      [{latex:'\\det', label:'det'}, {latex:'\\operatorname{tr}', label:'tr'}, {latex:'\\operatorname{rank}', label:'rank'}, {latex:'\\dim', label:'dim'}, {latex:'\\mathbf{I}', label:'\uD835\uDC08'}, {latex:'\\mathbf{0}', label:'\uD835\uDFCE'}],
      [{latex:'\\cdot', label:'\u22C5'}, {latex:'\\times', label:'\u00D7'}, {latex:'\\otimes', label:'\u2297'}, {latex:'\\oplus', label:'\u2295'}, {latex:'^{\\top}', label:'\u22A4'}, {latex:'^{\\dagger}', label:'\u2020'}]
    ]},
    {label:'\u2200', tooltip:'Logic & set theory', rows:[
      [{latex:'\\land', label:'\u2227'}, {latex:'\\lor', label:'\u2228'}, {latex:'\\neg', label:'\u00AC'}, {latex:'\\implies', label:'\u21D2'}, {latex:'\\iff', label:'\u21D4'}, {latex:'\\oplus', label:'\u2295'}],
      [{latex:'\\forall', label:'\u2200'}, {latex:'\\exists', label:'\u2203'}, {latex:'\\nexists', label:'\u2204'}, {latex:'\\top', label:'\u22A4'}, {latex:'\\bot', label:'\u22A5'}, {latex:'\\vdash', label:'\u22A2'}],
      [{latex:'\\in', label:'\u2208'}, {latex:'\\notin', label:'\u2209'}, {latex:'\\subset', label:'\u2282'}, {latex:'\\subseteq', label:'\u2286'}, {latex:'\\supset', label:'\u2283'}, {latex:'\\supseteq', label:'\u2287'}],
      [{latex:'\\cup', label:'\u222A'}, {latex:'\\cap', label:'\u2229'}, {latex:'\\setminus', label:'\u2216'}, {latex:'\\emptyset', label:'\u2205'}, {latex:'\\mathbb{N}', label:'\u2115'}, {latex:'\\mathbb{Z}', label:'\u2124'}, {latex:'\\mathbb{R}', label:'\u211D'}, {latex:'\\mathbb{C}', label:'\u2102'}]
    ]},
    {label:'\u21D2', tooltip:'Arrows & relations', rows:[
      [{latex:'\\leftarrow', label:'\u2190'}, {latex:'\\rightarrow', label:'\u2192'}, {latex:'\\leftrightarrow', label:'\u2194'}, {latex:'\\Leftarrow', label:'\u21D0'}, {latex:'\\Rightarrow', label:'\u21D2'}, {latex:'\\Leftrightarrow', label:'\u21D4'}],
      [{latex:'\\uparrow', label:'\u2191'}, {latex:'\\downarrow', label:'\u2193'}, {latex:'\\mapsto', label:'\u21A6'}, {latex:'\\hookrightarrow', label:'\u21AA'}, {latex:'\\nearrow', label:'\u2197'}, {latex:'\\searrow', label:'\u2198'}],
      [{latex:'\\equiv', label:'\u2261'}, {latex:'\\approx', label:'\u2248'}, {latex:'\\sim', label:'\u223C'}, {latex:'\\cong', label:'\u2245'}, {latex:'\\propto', label:'\u221D'}, {latex:'\\neq', label:'\u2260'}],
      [{latex:'\\leq', label:'\u2264'}, {latex:'\\geq', label:'\u2265'}, {latex:'\\ll', label:'\u226A'}, {latex:'\\gg', label:'\u226B'}, {latex:'\\prec', label:'\u227A'}, {latex:'\\succ', label:'\u227B'}]
    ]},
    {label:'\u222B', tooltip:'Calculus & analysis', rows:[
      [{latex:'\\frac{d}{d\\placeholder{}}', label:'d/dx'}, {latex:'\\frac{\\partial}{\\partial \\placeholder{}}', label:'\u2202/\u2202x'}, {latex:'\\nabla', label:'\u2207'}, {latex:'\\Delta', label:'\u0394'}, {latex:'\\partial', label:'\u2202'}],
      [{latex:'\\int_{\\placeholder{}}^{\\placeholder{}}', label:'\u222B'}, {latex:'\\iint', label:'\u222C'}, {latex:'\\iiint', label:'\u222D'}, {latex:'\\oint', label:'\u222E'}, {latex:'\\lim_{\\placeholder{} \\to \\placeholder{}}', label:'lim'}],
      [{latex:'\\sum_{\\placeholder{}}^{\\placeholder{}}', label:'\u2211'}, {latex:'\\prod_{\\placeholder{}}^{\\placeholder{}}', label:'\u220F'}, {latex:'\\infty', label:'\u221E'}, {latex:'\\to', label:'\u2192'}, {latex:'\\pm', label:'\u00B1'}, {latex:'\\mp', label:'\u2213'}],
      [{latex:'\\sin', label:'sin'}, {latex:'\\cos', label:'cos'}, {latex:'\\tan', label:'tan'}, {latex:'\\ln', label:'ln'}, {latex:'\\log', label:'log'}, {latex:'\\exp', label:'exp'}]
    ]},
    {label:'\u21CC', tooltip:'Chemistry & physics', rows:[
      [{latex:'\\rightleftharpoons', label:'\u21CC'}, {latex:'\\xrightarrow{\\placeholder{}}', label:'\u2192\u0332'}, {latex:'\\xleftarrow{\\placeholder{}}', label:'\u2190\u0332'}, {latex:'\\uparrow', label:'\u2191'}, {latex:'\\downarrow', label:'\u2193'}],
      [{latex:'^{\\placeholder{}}_{\\placeholder{}}\\text{\\placeholder{}}', label:'\u00B9X\u2081'}, {latex:'\\Delta H', label:'\u0394H'}, {latex:'\\Delta G', label:'\u0394G'}, {latex:'\\Delta S', label:'\u0394S'}, {latex:'K_{eq}', label:'K\u2091\u2091'}],
      [{latex:'\\alpha', label:'\u03B1'}, {latex:'\\beta', label:'\u03B2'}, {latex:'\\gamma', label:'\u03B3'}, {latex:'\\lambda', label:'\u03BB'}, {latex:'\\mu', label:'\u03BC'}, {latex:'\\nu', label:'\u03BD'}, {latex:'\\omega', label:'\u03C9'}],
      [{latex:'\\hbar', label:'\u210F'}, {latex:'\\ell', label:'\u2113'}, {latex:'\\varepsilon_0', label:'\u03B5\u2080'}, {latex:'\\mu_0', label:'\u03BC\u2080'}, {latex:'k_B', label:'k\u0042'}, {latex:'\\sigma', label:'\u03C3'}]
    ]}
  ];
});
</script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#FAF8F5;--surface:#fff;--border:#E8E0D8;
  --text:#1a1a1a;--muted:#6b6b6b;--hint:#aaa;
  --teal:#B85C38;--teal-light:#FDF0EB;--teal-mid:#E8B4A0;--accent:#B85C38;
  --amber:#7A4A0A;--amber-light:#FAEEDA;--amber-mid:#FAC775;
  --red:#9B2828;--red-light:#FCEBEB;--red-mid:#F7C1C1;
  --green:#3B6D11;--green-light:#EAF3DE;--green-mid:#C0DD97;
  --blue:#185FA5;--blue-light:#E6F1FB;
  --radius:12px;--shadow-sm:0 1px 3px rgba(0,0,0,.04);--shadow-md:0 4px 16px rgba(0,0,0,.06);
}
[data-theme="dark"]{
  --bg:#181818;--surface:#222;--border:#333;--text:#ececec;--muted:#aaa;--hint:#666;
  --teal:#E8956E;--teal-light:rgba(232,149,110,.12);--teal-mid:rgba(232,149,110,.3);--accent:#E8956E;
  --amber:#FBBF24;--amber-light:rgba(251,191,36,.1);--amber-mid:rgba(251,191,36,.25);
  --red:#F87171;--red-light:rgba(248,113,113,.1);--red-mid:rgba(248,113,113,.25);
  --green:#86EFAC;--green-light:rgba(134,239,172,.1);--green-mid:rgba(134,239,172,.25);
  --blue:#60A5FA;--blue-light:rgba(96,165,250,.1);
  --shadow-sm:0 1px 4px rgba(0,0,0,.2);--shadow-md:0 4px 20px rgba(0,0,0,.25);
}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;
     -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
     transition:background .2s,color .2s}

/* Theme toggle */
.theme-toggle{background:none;border:1px solid var(--border);border-radius:8px;padding:4px 10px;
  cursor:pointer;font-size:15px;color:var(--muted);transition:all .15s;display:flex;align-items:center;gap:4px}
.theme-toggle:hover{border-color:var(--teal-mid);color:var(--text);background:var(--teal-light)}

/* MathLive dark mode + overrides */
[data-theme="dark"] math-field{--hue:15;--_text-font-family:inherit;background:var(--surface);color:var(--text);border-color:var(--border)}
[data-theme="dark"] math-field::part(menu-toggle){color:var(--muted)}
math-field::part(menu-toggle){display:none}
[data-theme="dark"] .katex{color:var(--text)}

/* Global scrollbar */
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}
*{scrollbar-width:thin;scrollbar-color:var(--border) var(--bg)}

header{background:var(--surface);border-bottom:1px solid var(--border);
  padding:0;display:flex;flex-direction:column;position:sticky;top:0;z-index:100;
  box-shadow:var(--shadow-sm)}
.hdr-top{display:flex;align-items:center;justify-content:space-between;padding:.75rem 1.5rem .4rem}
.hdr-bottom{display:flex;align-items:center;gap:8px;padding:0 1.5rem .65rem;flex-wrap:wrap}
header h1{font-size:18px;font-weight:700;letter-spacing:-.02em}
header .formula{font-size:10px;color:var(--hint);font-family:'SF Mono',Menlo,Consolas,monospace;
  background:var(--bg);border:1px solid var(--border);border-radius:20px;padding:3px 12px;line-height:1.6;
  opacity:.65;transition:opacity .2s;white-space:nowrap}
header .formula:hover{opacity:1}
header .subtitle{font-size:11px;color:var(--muted);font-weight:400;letter-spacing:.01em}

.main{max-width:740px;margin:0 auto;padding:1.25rem}

.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:1.25rem}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:1rem 1.1rem;transition:transform .15s,box-shadow .15s;position:relative;overflow:hidden}
.stat:hover{transform:translateY(-2px);box-shadow:var(--shadow-md)}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:var(--radius) var(--radius) 0 0}
.stat:nth-child(1)::before{background:linear-gradient(90deg,var(--red),var(--red-mid))}
.stat:nth-child(2)::before{background:linear-gradient(90deg,var(--amber),var(--amber-mid))}
.stat:nth-child(3)::before{background:linear-gradient(90deg,var(--teal),var(--teal-mid))}
.stat-icon{font-size:20px;margin-bottom:6px}
.stat-lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;font-weight:500}
.stat-num{font-size:28px;font-weight:700;line-height:1}
.c-red{color:var(--red)}.c-amber{color:var(--amber)}.c-teal{color:var(--teal)}

.toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;gap:10px;flex-wrap:wrap}
.filters{display:flex;gap:0;background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:3px;overflow:hidden}
.filter-btn{font-size:12px;padding:6px 14px;border:none;border-radius:8px;background:transparent;
  color:var(--muted);cursor:pointer;transition:all .15s;font-weight:500;white-space:nowrap}
.filter-btn:hover{color:var(--text);background:var(--surface)}
.filter-btn.active{background:var(--teal);color:#fff;box-shadow:0 1px 4px rgba(184,92,56,.2)}

.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:1.1rem 1.25rem;margin-bottom:10px;transition:transform .15s,box-shadow .2s;
  position:relative;overflow:hidden}
.card:hover{box-shadow:var(--shadow-md)}
.card::after{content:'';position:absolute;top:0;left:0;bottom:0;width:4px}
.card.overdue::after{background:var(--red)}
.card.due::after{background:var(--amber)}
.card-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.topic-name{font-weight:600;font-size:15px;line-height:1.35;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.topic-tags{font-size:10px;color:var(--hint);font-weight:400;flex-basis:100%;order:3;margin-top:-4px;line-height:1.6}
.badge{font-size:10px;font-weight:600;padding:3px 10px;border-radius:20px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap;flex-shrink:0}
.b-overdue{background:var(--red-light);color:var(--red)}
.b-due{background:var(--amber-light);color:var(--amber)}
.b-soon{background:var(--teal-light);color:var(--teal)}
.b-upcoming{background:var(--green-light);color:var(--green)}
.meta-row{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px}
.meta{font-size:11.5px;color:var(--muted);display:flex;align-items:center;gap:4px}
.meta strong{color:var(--text);font-weight:600}
.editable-date,.editable-field{cursor:pointer;border-bottom:1px dashed var(--border);padding-bottom:1px;transition:border-color .15s}
.editable-date:hover,.editable-field:hover{border-color:var(--teal);color:var(--teal)}
.field-edit-input{border:1px solid var(--teal);border-radius:4px;padding:2px 6px;font:inherit;font-size:inherit;font-weight:inherit;color:var(--text);background:var(--surface);outline:none;min-width:40px}
.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}
button{font-family:inherit;cursor:pointer;border-radius:8px;font-size:13px;padding:6px 14px;border:1px solid var(--border);background:var(--surface);color:var(--text);transition:all .15s}
button:hover{background:var(--bg);border-color:var(--teal-mid)}
.btn-primary{background:var(--teal);color:#fff;border:none;font-weight:600;padding:8px 18px;
  border-radius:10px;box-shadow:0 2px 8px rgba(184,92,56,.2);transition:all .15s}
.btn-primary:hover{box-shadow:0 4px 14px rgba(184,92,56,.3);transform:translateY(-1px)}
.btn-review{background:var(--teal);color:#fff;border:none;font-weight:600;border-radius:8px;
  padding:6px 16px;box-shadow:0 1px 4px rgba(184,92,56,.15)}
.btn-review:hover{box-shadow:0 3px 10px rgba(184,92,56,.25);transform:translateY(-1px)}
.btn-review-early{background:var(--teal-light);color:var(--teal);border:1px solid var(--teal-mid);font-weight:500}
.btn-review-early:hover{background:var(--teal-mid)}
.btn-done{background:var(--green-light);color:var(--green);border:1px solid var(--green-mid);font-weight:500;cursor:default}
.btn-undo{font-size:11px;color:var(--muted);border-color:var(--border);padding:4px 10px}
.btn-del{font-size:11px;color:var(--red);border-color:transparent;background:transparent;opacity:.6;transition:opacity .15s}
.btn-del:hover{opacity:1;background:var(--red-light)}
.btn-cards{display:inline-flex;align-items:center;gap:5px;font-size:12px;padding:5px 12px;
  border-radius:8px;color:var(--teal);background:var(--teal-light);border:1px solid var(--teal-mid);
  text-decoration:none;font-weight:500;transition:all .15s}
.btn-cards:hover{background:var(--teal-mid);transform:translateY(-1px)}
.cards-badge{background:var(--teal);color:#fff;font-size:10px;padding:1px 6px;border-radius:10px;font-weight:600}
.eq{font-size:10px;color:var(--hint);margin-left:auto;font-family:'SF Mono',Menlo,Consolas,monospace;opacity:.5}
.add-form{display:none;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:1.25rem;margin-bottom:1rem;
  box-shadow:var(--shadow-sm);animation:slideUp .2s ease}
.add-form.open{display:block}
.form-row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
.fg{display:flex;flex-direction:column;gap:4px;min-width:120px}
.fg label{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
input[type="text"],input[type="date"]{font-family:inherit;font-size:14px;padding:8px 12px;
  border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);
  outline:none;transition:border-color .15s,box-shadow .15s}
input[type="text"]:focus,input[type="date"]:focus{border-color:var(--teal);box-shadow:0 0 0 3px var(--teal-light)}

/* Notification bell */
.bell{position:relative;background:none;border:none;padding:4px;cursor:pointer;color:var(--muted);display:flex;align-items:center}
.bell:hover{color:var(--text)}
.bell svg{width:22px;height:22px}
.bell-dot{position:absolute;top:1px;right:1px;width:9px;height:9px;border-radius:50%;background:var(--red);border:2px solid var(--surface);display:none}
.bell-dot.show{display:block}

/* Settings button */
.settings-btn{background:none;border:1px solid var(--border);border-radius:8px;padding:4px;cursor:pointer;color:var(--muted);display:flex;align-items:center;transition:all .15s}
.settings-btn:hover{border-color:var(--teal-mid);color:var(--text);background:var(--teal-light)}

/* Settings Modal */
.settings-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(4px);z-index:10000;align-items:center;justify-content:center}
.settings-overlay.open{display:flex}
.settings-modal{background:var(--surface);border-radius:20px;padding:32px 36px;max-width:480px;width:90%;box-shadow:0 20px 60px rgba(0,0,0,.25);animation:slideUp .25s ease}
.settings-modal h2{font-size:18px;font-weight:600;margin-bottom:20px}
.settings-section{margin-bottom:24px}
.settings-label{font-size:13px;font-weight:600;color:var(--text);margin-bottom:4px;display:block}
.settings-hint{font-size:12px;color:var(--muted);margin:0 0 12px;line-height:1.5}
.settings-hint a{color:var(--teal);text-decoration:none}
.settings-hint a:hover{text-decoration:underline}
.settings-key-row{display:flex;gap:8px;align-items:center}
.settings-input{flex:1;font-family:'SF Mono',Menlo,Consolas,monospace;font-size:13px;padding:10px 14px;border:1px solid var(--border);border-radius:10px;background:var(--bg);color:var(--text);outline:none;transition:border-color .15s,box-shadow .15s}
.settings-input:focus{border-color:var(--teal);box-shadow:0 0 0 3px var(--teal-light)}
.settings-input::placeholder{color:var(--hint)}
.settings-eye{background:none;border:1px solid var(--border);border-radius:8px;padding:8px;cursor:pointer;font-size:16px;line-height:1;color:var(--muted);transition:all .15s}
.settings-eye:hover{border-color:var(--teal-mid);color:var(--text)}
.settings-status{margin-top:10px;font-size:12px;min-height:18px}
.settings-status .connected{color:var(--green)}
.settings-status .disconnected{color:var(--muted)}
.settings-status .no-lib{color:var(--amber)}
.settings-actions{display:flex;gap:10px;justify-content:flex-end}
.settings-btn-cancel{padding:10px 24px;border-radius:10px;font-size:14px;font-weight:500;cursor:pointer;transition:all .15s;background:var(--bg);color:var(--muted);border:1px solid var(--border)}
.settings-btn-cancel:hover{background:var(--border);color:var(--text)}
.settings-btn-save{padding:10px 24px;border-radius:10px;font-size:14px;font-weight:500;cursor:pointer;transition:all .15s;background:linear-gradient(135deg,var(--teal),var(--accent));color:#fff;border:none;box-shadow:0 2px 8px rgba(184,92,56,.3)}
.settings-btn-save:hover{box-shadow:0 4px 16px rgba(184,92,56,.4);transform:translateY(-1px)}

/* Toast notifications */
.toast-wrap{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.toast{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 16px;
  font-size:13px;box-shadow:0 4px 16px rgba(0,0,0,.1);pointer-events:auto;
  animation:toastIn .3s ease forwards;display:flex;align-items:center;gap:8px;max-width:320px}
.toast.out{animation:toastOut .25s ease forwards}
.toast-icon{font-size:16px;flex-shrink:0}
.toast-success{border-left:3px solid var(--teal)}
.toast-info{border-left:3px solid var(--blue)}
.toast-warn{border-left:3px solid #EF9F27}
@keyframes toastIn{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:translateX(0)}}
@keyframes toastOut{from{opacity:1;transform:translateX(0)}to{opacity:0;transform:translateX(30px)}}

/* Header layout */
.hdr-right{display:flex;align-items:center;gap:10px}
.hdr-actions{display:inline-flex;align-items:center;gap:2px;background:var(--bg);
  border:1px solid var(--border);border-radius:10px;padding:3px}
.hdr-actions button,.hdr-actions label{font-size:11px;padding:5px 10px;border:none;border-radius:7px;
  background:transparent;color:var(--muted);cursor:pointer;transition:all .15s;
  display:inline-flex;align-items:center;gap:4px;font-weight:500}
.hdr-actions button:hover,.hdr-actions label:hover{background:var(--surface);color:var(--text);
  box-shadow:0 1px 3px rgba(0,0,0,.06)}

/* Schedule graph */
.sched-section{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  margin-bottom:1.25rem;overflow:hidden}
.sched-header{display:flex;align-items:center;justify-content:space-between;padding:.75rem 1.1rem;
  cursor:pointer;user-select:none}
.sched-header:hover{background:var(--bg)}
.sched-title{font-size:14px;font-weight:500}
.sched-toggle{font-size:11px;color:var(--muted);transition:transform .2s}
.sched-toggle.open{transform:rotate(180deg)}
.sched-body{overflow:hidden;transition:max-height .35s ease;max-height:0}
.sched-body.open{max-height:800px}
.sched-inner{padding:0 1.1rem 1rem}
.sched-graph{overflow-x:auto;padding-bottom:6px}
.sched-graph::-webkit-scrollbar{height:6px}
.sched-graph::-webkit-scrollbar-track{background:var(--bg);border-radius:3px}
.sched-graph::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;transition:background .2s}

/* Tag bar */
#tag-bar .filter-btn{font-size:11px;padding:3px 10px;border-radius:20px}
.sched-graph::-webkit-scrollbar-thumb:hover{background:var(--muted)}
@supports not selector(::-webkit-scrollbar){
  .sched-graph{scrollbar-width:thin;scrollbar-color:var(--border) var(--bg)}
}
.sched-empty{text-align:center;padding:1.5rem;color:var(--hint);font-size:13px}

.btn-cards{font-size:12px;color:var(--blue);border-color:var(--border);padding:3px 10px;text-decoration:none;display:inline-flex;align-items:center;gap:4px}
.btn-cards:hover{color:var(--text);border-color:var(--text);background:var(--bg)}
.cards-badge{font-size:11px;color:var(--hint)}

/* Study session overlay */
.ss-overlay{position:fixed;inset:0;z-index:10000;background:var(--bg);display:flex;flex-direction:column;
  animation:ssFadeIn .25s ease forwards;overflow-y:auto}
/* MathLive virtual keyboard must float above the study session overlay */
.ML__keyboard{z-index:100000 !important}
@keyframes ssFadeIn{from{opacity:0}to{opacity:1}}
.ss-header{background:var(--surface);border-bottom:1px solid var(--border);padding:.8rem 1.5rem;
  display:flex;align-items:center;justify-content:space-between}
.ss-header h2{font-size:16px;font-weight:500}
.ss-progress{font-size:12px;color:var(--muted)}
.ss-close{background:none;border:none;font-size:20px;color:var(--muted);cursor:pointer;padding:4px 8px}
.ss-close:hover{color:var(--text)}
.ss-back{background:none;border:1px solid var(--border);border-radius:6px;font-size:14px;color:var(--muted);
  cursor:pointer;padding:4px 10px;transition:all .15s;flex-shrink:0}
.ss-back:hover{color:var(--text);background:var(--bg);border-color:var(--teal-mid)}
.ss-body{max-width:600px;width:100%;margin:0 auto;padding:2rem 1.5rem;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ss-overlay.vk-open .ss-body{justify-content:flex-start;padding-top:1.5rem;padding-bottom:340px}
.ss-card-type{font-size:11px;color:var(--hint);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px;
  background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:3px 10px;display:inline-block}
.ss-question{font-size:22px;font-weight:600;text-align:center;line-height:1.5;margin-bottom:1.5rem;max-width:520px;
  background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:1.5rem 2rem;
  box-shadow:0 2px 12px rgba(0,0,0,.04);width:100%}
.ss-answer-area{width:100%;margin-bottom:1.5rem}
.ss-answer-area textarea{width:100%;min-height:100px;padding:14px 16px;border:2px solid var(--border);border-radius:12px;
  font-family:inherit;font-size:15px;background:var(--surface);color:var(--text);outline:none;resize:vertical;
  line-height:1.5;transition:border-color .2s,box-shadow .2s}
.ss-answer-area textarea:focus{border-color:var(--teal);box-shadow:0 0 0 3px rgba(184,92,56,.12)}
.ss-answer-area textarea::placeholder{color:var(--hint)}
.ss-answer-area .ss-hint{font-size:12px;color:var(--hint);margin-top:6px;text-align:center}
.math-helper{display:flex;align-items:center;gap:6px;border:1px solid var(--border);border-radius:8px;padding:3px 4px;margin-top:4px}
.math-helper math-field{flex:1;min-width:120px;font-size:14px;border:none;outline:none;background:transparent;color:var(--text);min-height:32px}
.math-helper .mh-insert{padding:3px 10px;font-size:11px;font-weight:600;border-radius:6px;border:1px solid var(--teal-mid);
  background:var(--teal-light);color:var(--teal);cursor:pointer;white-space:nowrap;transition:all .15s}
.math-helper .mh-insert:hover{background:var(--teal-mid);color:#fff}
.ss-reveal-btn{background:var(--teal);color:#fff;border:none;padding:12px 28px;border-radius:12px;font-size:15px;
  font-weight:600;cursor:pointer;transition:background .15s,transform .1s,box-shadow .15s}
.ss-reveal-btn:hover{background:var(--teal);transform:translateY(-1px);box-shadow:0 4px 12px rgba(15,110,86,.25)}
.ss-answer-box{background:var(--surface);border:2px solid var(--green-mid);border-radius:14px;padding:1.4rem 1.8rem;
  width:100%;margin-bottom:1.5rem;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.04)}
.ss-answer-label{font-size:11px;color:var(--green);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;font-weight:600}
.ss-answer-text{font-size:17px;line-height:1.6;font-weight:500}
.ss-your-answer{background:var(--bg);border-radius:10px;padding:10px 14px;margin-top:12px;font-size:13px;
  color:var(--muted);text-align:left;border:1px solid var(--border)}
.ss-your-answer strong{color:var(--text);font-weight:600}

/* Multiple choice options */
.mc-options{width:100%;display:flex;flex-direction:column;gap:10px;margin-bottom:1.5rem}
.mc-opt{width:100%;padding:14px 18px;border:2px solid var(--border);border-radius:12px;background:var(--surface);
  color:var(--text);font-size:15px;font-weight:500;text-align:left;cursor:pointer;
  transition:border-color .15s,background .15s,transform .1s,box-shadow .15s;line-height:1.4;
  display:flex;align-items:center;gap:12px}
.mc-opt:hover{border-color:var(--teal-mid);background:var(--teal-light);transform:translateY(-1px);
  box-shadow:0 2px 8px rgba(0,0,0,.06)}
.mc-opt .mc-letter{width:28px;height:28px;border-radius:50%;background:var(--bg);border:2px solid var(--border);
  display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;
  transition:background .15s,border-color .15s,color .15s}
.mc-opt:hover .mc-letter{border-color:var(--teal-mid);color:var(--teal)}
.mc-opt.selected{border-color:var(--teal);background:var(--teal-light)}
.mc-opt.selected .mc-letter{background:var(--teal);border-color:var(--teal);color:#fff}
.mc-opt.correct{border-color:var(--green-mid);background:var(--green-light)}
.mc-opt.correct .mc-letter{background:var(--green);border-color:var(--green);color:#fff}
.mc-opt.wrong{border-color:var(--red-mid);background:var(--red-light)}
.mc-opt.wrong .mc-letter{background:var(--red);border-color:var(--red);color:#fff}
.mc-opt.dimmed{opacity:.5;cursor:default}
.mc-opt.dimmed:hover{transform:none;box-shadow:none}

/* Rating buttons */
.ss-rate{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.ss-rate button{padding:12px 28px;border-radius:12px;font-size:14px;font-weight:600;cursor:pointer;border:2px solid transparent;
  transition:transform .15s,box-shadow .2s}
.ss-rate button:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.12)}
.ss-rate .rate-fail{background:var(--red-light);color:var(--red);border-color:var(--red-mid)}
.ss-rate .rate-fail:hover{background:#f9d4d4}
.ss-rate .rate-partial{background:var(--amber-light);color:var(--amber);border-color:var(--amber-mid)}
.ss-rate .rate-partial:hover{background:#fce5c3}
.ss-rate .rate-complete{background:var(--green-light);color:var(--green);border-color:var(--green-mid)}
.ss-rate .rate-complete:hover{background:#d9edc4}

/* Session summary */
.ss-summary{text-align:center}
.ss-summary h3{font-size:20px;font-weight:500;margin-bottom:1rem}

/* Session progress bar */
.ss-progress-bar{height:3px;background:var(--border);width:100%}
.ss-progress-fill{height:100%;background:var(--teal);border-radius:0 3px 3px 0;transition:width .3s ease}

/* Session card breakdown */
.ss-breakdown{width:100%;max-width:400px;margin:0 auto 1.2rem;text-align:left}
.ss-breakdown-title{font-size:11px;color:var(--hint);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px}
.ss-card-row{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border);font-size:13px}
.ss-card-row:last-child{border-bottom:none}
.ss-card-q{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.ss-card-badge{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:500}
.ss-card-badge.complete{background:var(--green-light);color:var(--green)}
.ss-card-badge.partial{background:var(--amber-light);color:var(--amber)}
.ss-card-badge.failed{background:var(--red-light);color:var(--red)}
.ss-summary-stats{display:flex;gap:16px;justify-content:center;margin-bottom:1.5rem}
.ss-summary-stat{padding:12px 20px;border-radius:10px;border:1px solid var(--border);background:var(--surface)}
.ss-summary-stat .ss-stat-num{font-size:24px;font-weight:500}
.ss-summary-stat .ss-stat-lbl{font-size:11px;color:var(--muted);text-transform:uppercase}
.ss-summary .ss-done-btn{background:var(--teal);color:#fff;border:none;padding:10px 28px;border-radius:10px;font-size:15px;font-weight:500;cursor:pointer}
.ss-summary .ss-done-btn:hover{background:var(--teal)}

/* AI Evaluation Panel */
.ai-panel{width:100%;margin:1rem 0;border:1px solid var(--border);border-radius:12px;background:var(--surface);
  padding:1rem 1.2rem;text-align:left;animation:ssFadeIn .3s ease}
.ai-panel-header{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:13px;font-weight:500}
.ai-panel-header .ai-icon{font-size:16px}
.ai-verdict{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.ai-verdict .ai-rating-badge{font-size:12px;padding:3px 12px;border-radius:10px;font-weight:600}
.ai-verdict .ai-rating-badge.complete{background:var(--green-light);color:var(--green);border:1px solid var(--green-mid)}
.ai-verdict .ai-rating-badge.partial{background:var(--amber-light);color:var(--amber);border:1px solid var(--amber-mid)}
.ai-verdict .ai-rating-badge.failed{background:var(--red-light);color:var(--red);border:1px solid var(--red-mid)}
.ai-explanation{font-size:13px;color:var(--muted);line-height:1.5;margin-bottom:8px}
.ai-missing{font-size:12px;color:var(--red);line-height:1.4;margin-bottom:10px;padding:6px 10px;
  background:var(--red-light);border-radius:8px;border:1px solid var(--red-mid)}
.ai-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ai-accept{font-size:12px;padding:5px 14px;border-radius:8px;background:var(--teal);color:#fff;border:1px solid var(--teal);
  font-weight:500;cursor:pointer}
.ai-accept:hover{background:var(--teal)}
.ai-override-label{font-size:11px;color:var(--hint);margin-left:4px}
.ai-override-btn{font-size:11px;padding:3px 10px;border-radius:6px;cursor:pointer;border:1px solid var(--border);
  background:var(--bg);color:var(--muted)}
.ai-override-btn:hover{border-color:var(--text);color:var(--text)}
.ai-override-btn.selected{border-color:var(--teal-mid);background:var(--teal-light);color:var(--teal)}
.ai-loading{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);padding:8px 0}
.ai-spinner{width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--teal);border-radius:50%;
  animation:aiSpin .6s linear infinite}
@keyframes aiSpin{to{transform:rotate(360deg)}}

/* Streak */
.streak-bar{display:flex;gap:16px;align-items:center;margin-bottom:14px;
  background:linear-gradient(135deg,var(--surface),var(--teal-light));
  border:1px solid var(--teal-mid);border-radius:var(--radius);padding:12px 18px;
  box-shadow:0 2px 8px rgba(184,92,56,.06)}
.streak-item{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted)}
.streak-item .streak-num{font-size:20px;font-weight:700;color:var(--text)}
.streak-item .streak-fire{font-size:20px}

/* Retention history sparkline */
.hist-wrap{overflow:hidden;max-height:0;transition:max-height .4s ease;margin-top:6px}
.hist-wrap.open{max-height:180px}
.btn-hist{font-size:11px;padding:3px 8px;color:var(--muted);border-color:var(--border)}
.btn-hist:hover{background:var(--bg)}

.math-preview{min-height:36px;padding:8px 14px;background:var(--bg);border:1px solid var(--border);
  border-radius:10px;margin-top:6px;font-size:16px;line-height:1.6;overflow-x:auto;
  transition:border-color .2s;display:none}
.math-preview:not(:empty){border-color:var(--teal-mid)}
.math-preview .katex{font-size:1.15em !important}
.math-preview-hint{color:var(--hint);font-size:12px;font-style:italic}
.math-preview-text{color:var(--text);font-size:15px}

/* KaTeX overrides for inline display */
.katex{font-size:1em !important}
.rendered-math .katex{font-size:1.1em !important}

/* ── PDF Import Button ── */
.btn-pdf-import{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
  padding:6px 14px;border:none;border-radius:10px;cursor:pointer;color:#fff;
  background:linear-gradient(135deg,var(--teal),#D4764E);
  box-shadow:0 2px 8px rgba(184,92,56,.25);transition:all .2s ease}
.btn-pdf-import:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(184,92,56,.35)}
.btn-pdf-import:active{transform:translateY(0);box-shadow:0 1px 4px rgba(184,92,56,.2)}
.btn-pdf-import svg{opacity:.9}

/* ── PDF Import Modal ── */
.pdf-modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(4px);
  display:none;align-items:center;justify-content:center;z-index:10000;animation:fadeIn .2s ease}
.pdf-modal-overlay.open{display:flex}
.pdf-modal{background:var(--surface);border-radius:20px;padding:32px 36px;max-width:680px;width:90%;
  box-shadow:0 20px 60px rgba(0,0,0,.3);animation:slideUp .25s ease;max-height:90vh;overflow-y:auto}
.pdf-modal h2{font-size:18px;font-weight:600;margin-bottom:4px;display:flex;align-items:center;gap:10px}
.pdf-modal .pdf-subtitle{font-size:13px;color:var(--muted);margin-bottom:20px}
.pdf-modal .pdf-file-info{display:flex;align-items:center;gap:12px;padding:14px 16px;
  background:var(--bg);border-radius:12px;border:1px solid var(--border);margin-bottom:20px}
.pdf-modal .pdf-file-icon{width:42px;height:42px;border-radius:10px;
  background:linear-gradient(135deg,#E74C3C,#C0392B);display:flex;align-items:center;
  justify-content:center;color:#fff;font-weight:700;font-size:11px;flex-shrink:0}
.pdf-modal .pdf-file-details{flex:1;min-width:0}
.pdf-modal .pdf-file-name{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pdf-modal .pdf-file-meta{font-size:12px;color:var(--muted);margin-top:2px}
.pdf-modal .pdf-slider-section{margin-bottom:24px}
.pdf-modal .pdf-slider-label{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.pdf-modal .pdf-slider-label span{font-size:13px;color:var(--muted)}
.pdf-modal .pdf-slider-label strong{font-size:22px;font-weight:700;color:var(--teal);min-width:36px;text-align:right}
.pdf-modal .pdf-slider-row{display:flex;align-items:center;gap:12px}
.pdf-modal .pdf-slider-row span{font-size:11px;color:var(--hint);min-width:16px;text-align:center}
.pdf-modal input[type="range"]{flex:1;-webkit-appearance:none;appearance:none;height:6px;
  border-radius:3px;background:var(--border);outline:none;cursor:pointer}
.pdf-modal input[type="range"]::-webkit-slider-thumb{-webkit-appearance:none;width:20px;height:20px;
  border-radius:50%;background:var(--teal);border:3px solid var(--surface);
  box-shadow:0 2px 6px rgba(0,0,0,.2);cursor:pointer;transition:transform .15s}
.pdf-modal input[type="range"]::-webkit-slider-thumb:hover{transform:scale(1.15)}
.pdf-modal input[type="range"]::-moz-range-thumb{width:20px;height:20px;
  border-radius:50%;background:var(--teal);border:3px solid var(--surface);
  box-shadow:0 2px 6px rgba(0,0,0,.2);cursor:pointer}
.pdf-modal .pdf-rec{display:inline-flex;align-items:center;gap:4px;font-size:11px;
  color:var(--teal);background:var(--teal-light);padding:3px 10px;border-radius:20px;margin-left:8px}
.pdf-modal .pdf-actions{display:flex;gap:10px;justify-content:flex-end}
.pdf-modal .pdf-btn{padding:10px 24px;border-radius:10px;font-size:14px;font-weight:600;
  border:none;cursor:pointer;transition:all .15s}
.pdf-modal .pdf-btn-cancel{background:var(--bg);color:var(--muted);border:1px solid var(--border)}
.pdf-modal .pdf-btn-cancel:hover{background:var(--border);color:var(--text)}
.pdf-modal .pdf-btn-generate{background:linear-gradient(135deg,var(--teal),#D4764E);color:#fff;
  box-shadow:0 2px 10px rgba(184,92,56,.3)}
.pdf-modal .pdf-btn-generate:hover{box-shadow:0 4px 16px rgba(184,92,56,.4);transform:translateY(-1px)}
.pdf-modal .pdf-btn-generate:disabled{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}

/* Generator modal tabs */
.gen-tabs{display:flex;gap:0;margin-bottom:20px;border-bottom:2px solid var(--border)}
.gen-tab{flex:1;padding:10px 16px;font-size:13px;font-weight:600;color:var(--muted);background:none;
  border:none;cursor:pointer;transition:all .15s;border-bottom:2px solid transparent;margin-bottom:-2px;
  display:flex;align-items:center;justify-content:center;gap:6px}
.gen-tab:hover{color:var(--text);background:var(--bg)}
.gen-tab.active{color:var(--teal);border-bottom-color:var(--teal)}
.gen-tab-panel{display:none}
.gen-tab-panel.active{display:block}
.prompt-textarea{width:100%;min-height:120px;padding:14px 16px;border:2px solid var(--border);border-radius:12px;
  font-family:inherit;font-size:14px;background:var(--surface);color:var(--text);outline:none;resize:vertical;
  line-height:1.5;transition:border-color .2s,box-shadow .2s;box-sizing:border-box;margin-bottom:16px}
.prompt-textarea:focus{border-color:var(--teal);box-shadow:0 0 0 3px rgba(184,92,56,.12)}
.prompt-textarea::placeholder{color:var(--hint)}
.prompt-topic-input{width:100%;padding:10px 14px;border:1px solid var(--border);border-radius:10px;
  font-family:inherit;font-size:13px;background:var(--surface);color:var(--text);outline:none;
  transition:border-color .15s;box-sizing:border-box;margin-bottom:16px}
.prompt-topic-input:focus{border-color:var(--teal)}

/* ── Dual Range Slider ── */
.dual-range{position:relative;height:36px;margin:8px 0}
.dual-range .dr-track{position:absolute;top:50%;left:0;right:0;height:6px;transform:translateY(-50%);background:var(--border);border-radius:3px}
.dual-range .dr-fill{position:absolute;top:50%;height:6px;transform:translateY(-50%);background:linear-gradient(90deg,var(--teal),#D4764E);border-radius:3px;pointer-events:none}
.dual-range input[type="range"]{position:absolute;top:0;left:0;width:100%;height:100%;margin:0;padding:0;-webkit-appearance:none;appearance:none;background:none;pointer-events:none;z-index:2}
.dual-range input[type="range"]::-webkit-slider-thumb{-webkit-appearance:none;width:22px;height:22px;border-radius:50%;background:var(--surface);border:3px solid var(--teal);box-shadow:0 1px 4px rgba(0,0,0,.18);cursor:pointer;pointer-events:auto;transition:transform .15s,box-shadow .15s}
.dual-range input[type="range"]::-webkit-slider-thumb:hover{transform:scale(1.15);box-shadow:0 2px 8px rgba(184,92,56,.35)}
.dual-range input[type="range"]::-moz-range-thumb{width:22px;height:22px;border-radius:50%;background:var(--surface);border:3px solid var(--teal);box-shadow:0 1px 4px rgba(0,0,0,.18);cursor:pointer;pointer-events:auto}
.dual-range input[type="range"]::-moz-range-thumb:hover{transform:scale(1.15)}
.dr-labels{display:flex;justify-content:space-between;align-items:center;margin-top:4px;font-size:11px;color:var(--muted)}
.dr-labels .dr-val{font-weight:600;color:var(--teal);font-size:13px;min-width:28px;text-align:center}
.dr-labels .dr-input{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:2px 4px;width:52px;font-family:inherit;font-weight:600;color:var(--teal);font-size:13px;text-align:center;outline:none;-moz-appearance:textfield}
.dr-labels .dr-input::-webkit-outer-spin-button,.dr-labels .dr-input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.dr-labels .dr-input:focus{border-color:var(--teal);box-shadow:0 0 0 2px rgba(184,92,56,.15)}
.dr-labels .dr-sep{color:var(--hint);font-size:12px}
.dr-info{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.dr-info svg{flex-shrink:0}
.dr-info .dr-title{font-size:12px;font-weight:600;color:var(--text)}
.dr-info .dr-total{font-size:11px;color:var(--muted);margin-left:auto}
.dr-tip{font-size:11px;color:var(--hint);margin-top:6px;text-align:center}

/* ── PDF Preview ── */
.pdf-preview{margin-bottom:16px;border-radius:10px;border:1px solid var(--border);overflow:hidden;background:var(--bg)}
.pdf-preview-header{font-size:12px;font-weight:600;color:var(--muted);padding:10px 12px 4px;display:flex;align-items:center;gap:6px}
.pdf-preview-strip{overflow-y:auto;max-height:50vh;padding:8px 12px 12px;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.pdf-preview-strip::-webkit-scrollbar{width:6px}
.pdf-preview-strip::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.pdf-page-slot{width:100%;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.15);background:var(--surface);display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden;transition:opacity .2s,outline-color .2s;margin-bottom:8px}
.pdf-page-slot canvas{width:100%;height:auto;display:block;border-radius:6px}
.pdf-page-num{font-size:13px;font-weight:600;color:var(--hint);pointer-events:none;position:absolute}

@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes slideUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}

/* ── Enhanced Forgetting Curve Graph ── */
.curve-wrap{margin-top:10px;overflow:hidden;transition:max-height .3s ease,opacity .25s ease;max-height:300px;opacity:1}
.curve-wrap.closed{max-height:0;opacity:0}
.curve-toggle{display:flex;align-items:center;gap:6px;margin-top:10px;cursor:pointer;user-select:none;
  font-size:12px;color:var(--muted);font-weight:500;padding:4px 0;transition:color .15s}
.curve-toggle:hover{color:var(--teal)}
.curve-toggle svg{transition:transform .2s;transform:rotate(180deg)}
.curve-toggle.closed svg{transform:rotate(0deg)}

/* ── Enhanced Retention Bar ── */
.ret-row{display:flex;align-items:center;gap:10px;margin-top:12px}
.ret-label{font-size:11px;color:var(--muted);min-width:82px;font-weight:500}
.ret-bg{flex:1;height:8px;background:var(--bg);border-radius:4px;overflow:hidden;
  box-shadow:inset 0 1px 3px rgba(0,0,0,.06)}
.ret-fill{height:100%;border-radius:4px;transition:width .6s cubic-bezier(.25,.8,.25,1);position:relative}
.ret-pct{font-size:13px;font-weight:600;min-width:42px;text-align:right}
.empty{text-align:center;padding:3rem 1rem;color:var(--hint);font-size:14px}
.empty svg{margin-bottom:12px;opacity:.4}

/* ── Practice Problems Tab ── */
.pp-tab-bar{display:flex;gap:0;margin-bottom:16px;background:var(--surface);border-radius:var(--radius);
  border:1px solid var(--border);overflow:hidden;box-shadow:var(--shadow-sm)}
.pp-tab{flex:1;padding:10px 16px;text-align:center;font-size:13px;font-weight:600;cursor:pointer;
  border:none;background:transparent;color:var(--muted);transition:all .2s;border-right:1px solid var(--border)}
.pp-tab:last-child{border-right:none}
.pp-tab.active{background:var(--teal);color:#fff}
.pp-tab:not(.active):hover{background:var(--teal-light);color:var(--teal)}

.pp-panel{display:none}
.pp-panel.active{display:block}

/* Problem list */
.pp-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.pp-toolbar .btn-primary{font-size:12px;padding:6px 14px}

.pp-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px;margin-bottom:10px;box-shadow:var(--shadow-sm);transition:box-shadow .2s, transform .15s}
.pp-card:hover{box-shadow:var(--shadow-md);transform:translateY(-1px)}
.pp-card-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:8px}
.pp-card-title{font-weight:600;font-size:14px;color:var(--text);flex:1}
.pp-card-meta{display:flex;gap:8px;flex-wrap:wrap;font-size:11px;color:var(--muted);margin-bottom:10px}
.pp-card-meta span{display:flex;align-items:center;gap:3px}
.pp-diff{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600;letter-spacing:.02em}
.pp-diff-1{background:var(--green-light);color:var(--green)}
.pp-diff-2{background:var(--amber-light);color:var(--amber)}
.pp-diff-3{background:var(--red-light);color:var(--red)}
.pp-skill{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600;
  background:var(--blue-light);color:var(--blue)}
.pp-rating-badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600}
.pp-rating-badge.complete{background:var(--green-light);color:var(--green)}
.pp-rating-badge.partial{background:var(--amber-light);color:var(--amber)}
.pp-rating-badge.failed{background:var(--red-light);color:var(--red)}
.pp-card-actions{display:flex;gap:6px;flex-wrap:wrap;padding-top:10px;border-top:1px solid var(--border)}
.pp-card-actions button{font-size:11px;padding:4px 12px;border-radius:6px;border:1px solid var(--border);
  background:var(--surface);color:var(--text);cursor:pointer;font-weight:500;transition:all .15s}
.pp-card-actions button:hover{background:var(--teal-light);border-color:var(--teal);color:var(--teal)}
.pp-card-actions .btn-start{background:var(--teal);color:#fff;border-color:var(--teal)}
.pp-card-actions .btn-start:hover{opacity:.9;transform:translateY(-1px)}
.pp-card-actions .btn-del{color:var(--red);border-color:var(--red-light)}
.pp-card-actions .btn-del:hover{background:var(--red-light)}
.pp-card-actions .btn-regen{color:var(--muted);border-color:transparent;background:transparent;font-size:14px;padding:4px 8px;transition:all .2s}
.pp-card-actions .btn-regen:hover{color:var(--teal);transform:rotate(90deg)}
.pp-card-actions .btn-regen.spinning{animation:spin 1s linear infinite;pointer-events:none;opacity:.5}

/* Problem solve overlay */
.pp-overlay{position:fixed;inset:0;z-index:10000;background:var(--bg);display:flex;flex-direction:column;overflow:auto}
.pp-header{display:flex;align-items:center;gap:12px;padding:14px 20px;background:var(--surface);
  border-bottom:1px solid var(--border);box-shadow:var(--shadow-sm);position:sticky;top:0;z-index:10}
.pp-header h2{font-size:16px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pp-close{background:none;border:none;font-size:22px;cursor:pointer;color:var(--muted);padding:4px 8px;
  border-radius:6px;transition:all .15s}
.pp-close:hover{background:var(--red-light);color:var(--red)}
.pp-body{max-width:800px;width:100%;margin:0 auto;padding:24px 20px}
.pp-prompt{font-size:15px;line-height:1.7;padding:20px;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);margin-bottom:20px;box-shadow:var(--shadow-sm)}
.pp-prompt .rendered-math{font-size:15px}

.pp-hints-section{margin-bottom:20px}
.pp-hint-btn{background:var(--amber-light);color:var(--amber);border:1px solid var(--amber-mid);
  padding:6px 16px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s}
.pp-hint-btn:hover{transform:translateY(-1px);box-shadow:0 2px 8px rgba(122,74,10,.15)}
.pp-hint-btn:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
.pp-hint{padding:12px 16px;background:var(--amber-light);border-left:3px solid var(--amber-mid);
  border-radius:0 8px 8px 0;margin-top:8px;font-size:13px;color:var(--text);line-height:1.5;
  animation:slideUp .2s ease}

.pp-solution-section{margin-bottom:20px}
.pp-sol-btn{background:var(--teal-light);color:var(--teal);border:1px solid var(--teal-mid);
  padding:6px 16px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s}
.pp-sol-btn:hover{transform:translateY(-1px);box-shadow:0 2px 8px rgba(184,92,56,.15)}
.pp-solution{padding:16px;background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--teal);
  border-radius:0 var(--radius) var(--radius) 0;margin-top:10px;font-size:13px;line-height:1.7;
  animation:slideUp .2s ease}

.pp-answer-section{margin-bottom:20px}
.pp-answer-section textarea{width:100%;min-height:120px;resize:vertical;font-family:inherit;font-size:14px;
  background:var(--surface);color:var(--text);border:2px solid var(--border);border-radius:var(--radius);
  padding:14px;line-height:1.6;outline:none;transition:border-color .2s}
.pp-answer-section textarea:focus{border-color:var(--teal)}
.pp-answer-section .math-helper{margin-top:8px}

.pp-submit-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:16px}
.pp-submit-btn{padding:10px 24px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;
  border:none;transition:all .15s}
.pp-submit-btn.ai{background:var(--teal);color:#fff}
.pp-submit-btn.ai:hover{opacity:.9;transform:translateY(-1px)}
.pp-submit-btn.ai:disabled{opacity:.5;cursor:default;transform:none}
.pp-submit-btn.manual{background:var(--surface);border:1px solid var(--border);color:var(--text)}
.pp-submit-btn.manual:hover{border-color:var(--teal);color:var(--teal)}
.pp-submit-btn.manual.active{background:var(--teal-light);border-color:var(--teal);color:var(--teal)}

.pp-ai-result{padding:16px;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);margin-top:16px;animation:slideUp .2s ease}
.pp-ai-result .ai-rating-badge{display:inline-block;padding:4px 12px;border-radius:8px;
  font-weight:600;font-size:12px;margin-bottom:8px}
.pp-ai-result .ai-rating-badge.complete{background:var(--green-light);color:var(--green)}
.pp-ai-result .ai-rating-badge.partial{background:var(--amber-light);color:var(--amber)}
.pp-ai-result .ai-rating-badge.failed{background:var(--red-light);color:var(--red)}
.pp-ai-result .ai-explanation{font-size:13px;line-height:1.5;color:var(--text);margin-top:6px}

.pp-manual-rating{display:flex;gap:8px;margin-top:12px}
.pp-manual-rating button{flex:1;padding:8px;border-radius:8px;border:1px solid var(--border);
  background:var(--surface);font-size:12px;font-weight:600;cursor:pointer;transition:all .15s}
.pp-manual-rating button:hover{transform:translateY(-1px)}
.pp-manual-rating .r-complete{color:var(--green);border-color:var(--green-mid)}
.pp-manual-rating .r-complete:hover,.pp-manual-rating .r-complete.sel{background:var(--green-light)}
.pp-manual-rating .r-partial{color:var(--amber);border-color:var(--amber-mid)}
.pp-manual-rating .r-partial:hover,.pp-manual-rating .r-partial.sel{background:var(--amber-light)}
.pp-manual-rating .r-failed{color:var(--red);border-color:var(--red-mid)}
.pp-manual-rating .r-failed:hover,.pp-manual-rating .r-failed.sel{background:var(--red-light)}

.pp-form{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px;margin-bottom:16px;animation:slideUp .2s ease;box-shadow:var(--shadow-sm)}
.pp-form .form-row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.pp-form .fg{flex:1;min-width:200px}
.pp-form label{display:block;font-size:11px;font-weight:600;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}
.pp-form input,.pp-form textarea,.pp-form select{width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;
  font-family:inherit;font-size:13px;background:var(--surface);color:var(--text);outline:none;transition:border-color .15s}
.pp-form input:focus,.pp-form textarea:focus,.pp-form select:focus{border-color:var(--teal)}
.pp-form textarea{min-height:80px;resize:vertical;line-height:1.5}

/* PDF Problems modal — reuses .pdf-modal-overlay / .pdf-modal classes */
</style>
</head>
<body>

<!-- Card Generator Modal -->
<div class="pdf-modal-overlay" id="pdf-modal">
  <div class="pdf-modal">
    <h2>
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--teal)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
      </svg>
      Card Generator
    </h2>
    <div class="pdf-subtitle">AI generates study flashcards from a prompt or PDF</div>
    <div class="gen-tabs">
      <button class="gen-tab active" onclick="switchGenTab('prompt')" id="gen-tab-prompt">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        Prompt
      </button>
      <button class="gen-tab" onclick="switchGenTab('pdf')" id="gen-tab-pdf">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        PDF Upload
      </button>
    </div>

    <!-- Prompt Tab -->
    <div class="gen-tab-panel active" id="gen-panel-prompt">
      <textarea class="prompt-textarea" id="gen-prompt-text" placeholder="Describe what you want to study...&#10;&#10;Examples:&#10;- Key concepts of photosynthesis for AP Biology&#10;- Linear algebra: eigenvalues and eigenvectors&#10;- French Revolution causes and effects&#10;- Python decorators and closures"></textarea>
      <input class="prompt-topic-input" id="gen-prompt-topic" placeholder="Topic name (optional — AI will auto-name if blank)">
      <div class="pdf-slider-section" id="prompt-slider-section">
        <div class="pdf-slider-label">
          <span>Number of cards</span>
          <strong id="prompt-card-count">15</strong>
        </div>
        <div class="pdf-slider-row">
          <span>5</span>
          <input type="range" id="prompt-slider" min="5" max="30" value="15" oninput="document.getElementById('prompt-card-count').textContent=this.value">
          <span>30</span>
        </div>
      </div>
      <div class="pdf-actions">
        <button class="pdf-btn pdf-btn-cancel" onclick="closePdfModal()">Cancel</button>
        <button class="pdf-btn pdf-btn-generate" id="prompt-generate-btn" onclick="generateFromPrompt()">Generate Cards</button>
      </div>
    </div>

    <!-- PDF Tab -->
    <div class="gen-tab-panel" id="gen-panel-pdf">
    <div class="pdf-file-info" id="pdf-file-info" style="display:none">
      <div class="pdf-file-icon">PDF</div>
      <div class="pdf-file-details">
        <div class="pdf-file-name" id="pdf-file-name"></div>
        <div class="pdf-file-meta" id="pdf-file-meta"></div>
      </div>
    </div>
    <div id="pdf-dropzone" style="border:2px dashed var(--border);border-radius:14px;padding:32px 20px;text-align:center;cursor:pointer;transition:all .2s;margin-bottom:20px">
      <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--hint)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:8px">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
      <div style="font-size:14px;font-weight:500;color:var(--text)">Drop PDF here or click to browse</div>
      <div style="font-size:12px;color:var(--hint);margin-top:4px">Supports .pdf files</div>
      <input type="file" accept=".pdf" id="pdf-file-input" style="display:none">
    </div>
    <div id="pdf-page-range" style="display:none;margin-bottom:16px;padding:12px;background:var(--bg);border-radius:8px;border:1px solid var(--border)">
      <div class="dr-info">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--teal)" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
        <span class="dr-title">Page range</span>
        <span class="dr-total" id="pdf-total-pages"></span>
      </div>
      <div class="dual-range" id="pdf-dual-range">
        <div class="dr-track"></div>
        <div class="dr-fill" id="pdf-dr-fill"></div>
        <input type="range" id="pdf-page-start" min="1" max="100" value="1">
        <input type="range" id="pdf-page-end" min="1" max="100" value="100">
      </div>
      <div class="dr-labels">
        <input type="number" class="dr-val dr-input" id="pdf-dr-start-label" value="1" min="1" max="100">
        <span class="dr-sep">&mdash;</span>
        <input type="number" class="dr-val dr-input" id="pdf-dr-end-label" value="100" min="1" max="100">
      </div>
      <div class="dr-tip">Drag handles to select a chapter or section</div>
    </div>
    <textarea class="prompt-textarea" id="pdf-prompt-text" placeholder="Optional: guide the AI on what to focus on...&#10;&#10;Examples:&#10;- Focus on definitions and key theorems&#10;- Emphasize practical applications&#10;- Create harder questions on sections 3-5&#10;- Skip the introduction, focus on proofs" style="display:none;margin-bottom:12px;min-height:72px"></textarea>
    <div class="pdf-slider-section" id="pdf-slider-section" style="display:none">
      <div class="pdf-slider-label">
        <span>Number of cards <span class="pdf-rec" id="pdf-rec-badge">&#x2728; Recommended</span></span>
        <strong id="pdf-card-count">15</strong>
      </div>
      <div class="pdf-slider-row">
        <span>5</span>
        <input type="range" id="pdf-slider" min="5" max="30" value="15" oninput="updatePdfSlider(this.value)">
        <span>30</span>
      </div>
    </div>
    <div id="pdf-preview" class="pdf-preview" style="display:none">
      <div class="pdf-preview-header">Preview</div>
      <div class="pdf-preview-strip" id="pdf-preview-strip"></div>
    </div>
    <div class="pdf-actions">
      <button class="pdf-btn pdf-btn-cancel" onclick="closePdfModal()">Cancel</button>
      <button class="pdf-btn pdf-btn-generate" id="pdf-generate-btn" disabled onclick="generateFromPdf()">Generate Cards</button>
    </div>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div class="settings-overlay" id="settings-overlay" onclick="if(event.target===this)closeSettingsModal()">
  <div class="settings-modal">
    <h2>&#x2699; Settings</h2>
    <div class="settings-section">
      <label class="settings-label">Anthropic API Key</label>
      <p class="settings-hint">Enables AI features: card generation, PDF import, hints, and problem solving. Get a key at <a href="https://console.anthropic.com/" target="_blank" rel="noopener">console.anthropic.com</a></p>
      <div class="settings-key-row">
        <input type="password" id="settings-api-key" class="settings-input" placeholder="sk-ant-..." autocomplete="off" spellcheck="false">
        <button class="settings-eye" onclick="toggleKeyVisibility()" id="settings-eye-btn" title="Show/hide key">&#x1F441;</button>
      </div>
      <div class="settings-status" id="settings-status"></div>
    </div>
    <div class="settings-actions">
      <button class="settings-btn-cancel" onclick="closeSettingsModal()">Cancel</button>
      <button class="settings-btn-save" onclick="saveApiKey()">Save</button>
    </div>
  </div>
</div>

<header>
  <div class="hdr-top">
    <div style="display:flex;align-items:center;gap:12px">
      <img src="/logo.png" alt="Study Tracker" style="height:36px;border-radius:8px">
      <div>
        <h1>Study Tracker</h1>
        <div class="subtitle">Spaced repetition &middot; Ebbinghaus curve</div>
      </div>
    </div>
    <div class="hdr-right">
      <div class="formula">R(t) = a + (1&minus;a)&middot;e<sup>&minus;kt</sup> &middot; review at &lt;80%</div>
      <button class="theme-toggle" onclick="toggleTheme()" id="theme-btn" title="Toggle light/dark mode">☀</button>
      <button class="settings-btn" onclick="openSettingsModal()" title="Settings">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="20" height="20">
          <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
        </svg>
      </button>
      <button class="bell" onclick="requestNotifPerm()" title="Enable browser notifications">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>
        </svg>
        <span class="bell-dot" id="bell-dot"></span>
      </button>
    </div>
  </div>
  <div class="hdr-bottom">
    <div class="hdr-actions">
      <button class="btn-pdf-import" onclick="openPdfModal()" title="AI generates flashcards from a prompt or PDF" style="border:none;border-radius:7px;padding:5px 10px;font-size:11px">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
        </svg>
        Card Generator
      </button>
      <button onclick="exportData()" title="Export all data as JSON">&#x2B06; Export</button>
      <label title="Import data from JSON backup">
        &#x2B07; Import
        <input type="file" accept=".json" style="display:none" onchange="importData(event)">
      </label>
      <a href="/statistics" title="View animated study statistics" style="font-size:11px;padding:5px 10px;border:none;border-radius:7px;background:transparent;color:var(--muted);cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:4px;font-weight:500;text-decoration:none"
         onmouseover="this.style.background='var(--surface)';this.style.color='var(--text)'"
         onmouseout="this.style.background='transparent';this.style.color='var(--muted)'">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>
        </svg>
        Statistics
      </a>
    </div>
  </div>
</header>
<div class="toast-wrap" id="toast-wrap"></div>

<div class="main">
  <div class="pp-tab-bar">
    <button class="pp-tab active" onclick="switchMainTab('topics')" id="tab-topics">&#x1F4DA; Topics &amp; Review</button>
    <button class="pp-tab" onclick="switchMainTab('problems')" id="tab-problems">&#x1F9E9; Practice Problems</button>
  </div>

  <div class="pp-panel active" id="panel-topics">
  <div class="stats">
    <div class="stat"><div class="stat-icon">⚠️</div><div class="stat-lbl">Overdue</div><div class="stat-num c-red" id="c-ov">—</div></div>
    <div class="stat"><div class="stat-icon">⏰</div><div class="stat-lbl">Due today</div><div class="stat-num c-amber" id="c-du">—</div></div>
    <div class="stat"><div class="stat-icon">✅</div><div class="stat-lbl">Upcoming</div><div class="stat-num c-teal" id="c-up">—</div></div>
  </div>

  <div class="streak-bar" id="streak-bar" style="display:none">
    <div class="streak-item"><span class="streak-fire">&#x1F525;</span><span class="streak-num" id="s-cur">0</span> day streak</div>
    <div class="streak-item">Best: <span class="streak-num" id="s-best">0</span></div>
    <div class="streak-item">Total: <span class="streak-num" id="s-total">0</span> days</div>
    <div class="streak-item">&#x1F0CF; <span class="streak-num" id="s-cards">0</span> cards</div>
  </div>

  <div class="sched-section">
    <div class="sched-header" onclick="toggleSchedule()">
      <span class="sched-title">&#x1F4C5; Review Schedule</span>
      <span class="sched-toggle" id="sched-arrow">&#9660;</span>
    </div>
    <div class="sched-body" id="sched-body">
      <div class="sched-inner">
        <div class="sched-graph" id="sched-graph"><div class="sched-empty">Loading…</div></div>
      </div>
    </div>
  </div>

  <div class="toolbar">
    <button class="btn-primary" onclick="toggleAdd()">+ Add topic</button>
    <div class="filters">
      <button class="filter-btn active" id="f-all"      onclick="setFilter('all')">All</button>
      <button class="filter-btn"        id="f-overdue"  onclick="setFilter('overdue')">Overdue</button>
      <button class="filter-btn"        id="f-due"      onclick="setFilter('due')">Due</button>
      <button class="filter-btn"        id="f-upcoming" onclick="setFilter('upcoming')">Upcoming</button>
    </div>
  </div>

  <div id="tag-bar" style="display:none;margin-bottom:8px;gap:6px;flex-wrap:wrap;align-items:center">
  </div>

  <div class="add-form" id="add-form">
    <div class="form-row">
      <div class="fg" style="flex:2">
        <label>Topic name</label>
        <input type="text" id="inp-name" placeholder="e.g. Memory Chain Model"
               onkeydown="if(event.key==='Enter')addTopic()">
      </div>
      <div class="fg">
        <label>Tags (comma-separated)</label>
        <input type="text" id="inp-tags" placeholder="e.g. psych, memory">
      </div>
      <div class="fg">
        <label>Learned on</label>
        <input type="date" id="inp-date">
      </div>
      <button class="btn-primary" onclick="addTopic()" style="align-self:flex-end;white-space:nowrap">Add</button>
      <button onclick="toggleAdd()" style="align-self:flex-end">Cancel</button>
    </div>
  </div>

  <div id="list"></div>
  </div><!-- /panel-topics -->

  <div class="pp-panel" id="panel-problems">
    <div class="pp-toolbar">
      <select id="pp-topic-select" onchange="ppLoadProblems()" style="padding:6px 12px;border-radius:8px;border:1px solid var(--border);font-size:13px;background:var(--surface);color:var(--text);min-width:180px">
        <option value="">— Select topic —</option>
      </select>
      <button class="btn-primary" onclick="ppToggleForm()" id="pp-add-btn" style="display:none">+ Add Problem</button>
      <span id="pp-count-badge" style="display:none;font-size:12px;color:var(--muted);font-weight:500;margin-left:auto"></span>
      <button class="btn-pdf-import" onclick="ppOpenGenModal()" id="pp-pdf-btn" style="display:none;border:none;border-radius:7px;padding:5px 10px;font-size:11px">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><polyline points="9 15 12 12 15 15"/>
        </svg>
        Generate Problems
      </button>
    </div>
    <div id="pp-form-wrap" style="display:none"></div>
    <div id="pp-list"></div>
  </div><!-- /panel-problems -->
</div>

<!-- Problem Generator Modal -->
<div class="pdf-modal-overlay" id="pp-gen-modal">
  <div class="pdf-modal">
    <h2>
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--teal)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
      </svg>
      Problem Generator
    </h2>
    <div class="pdf-subtitle">AI generates practice problems from a prompt or PDF</div>
    <div class="gen-tabs">
      <button class="gen-tab active" onclick="ppSwitchGenTab('prompt')" id="pp-gen-tab-prompt">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        Prompt
      </button>
      <button class="gen-tab" onclick="ppSwitchGenTab('pdf')" id="pp-gen-tab-pdf">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        PDF Upload
      </button>
    </div>

    <!-- Prompt Tab -->
    <div class="gen-tab-panel active" id="pp-gen-panel-prompt">
      <textarea class="prompt-textarea" id="pp-gen-prompt-text" placeholder="Describe what you want practice problems for...&#10;&#10;Examples:&#10;- Integration by parts and substitution problems&#10;- Linear algebra: eigenvalue proofs and computations&#10;- Organic chemistry reaction mechanisms&#10;- Thermodynamics: heat transfer calculations"></textarea>
      <div style="margin-bottom:14px">
        <label style="font-size:12px;font-weight:500;display:block;margin-bottom:4px">Target topic</label>
        <select id="pp-prompt-topic-select" style="width:100%;padding:7px 12px;border-radius:8px;border:1px solid var(--border);font-size:13px;background:var(--surface);color:var(--text)">
          <option value="">— Select topic —</option>
          <option value="__new__">+ Create new topic</option>
        </select>
        <input type="text" id="pp-prompt-new-topic-name" class="prompt-topic-input" placeholder="New topic name (optional — AI will auto-name if blank)" style="display:none;margin-top:6px">
      </div>
      <div class="pdf-slider-section">
        <div class="pdf-slider-label">
          <span>Number of problems</span>
          <strong id="pp-prompt-count">5</strong>
        </div>
        <div class="pdf-slider-row">
          <span>1</span>
          <input type="range" id="pp-prompt-slider" min="1" max="20" value="5" oninput="document.getElementById('pp-prompt-count').textContent=this.value">
          <span>20</span>
        </div>
      </div>
      <div class="pdf-actions">
        <button class="pdf-btn pdf-btn-cancel" onclick="ppCloseGenModal()">Cancel</button>
        <button class="pdf-btn pdf-btn-generate" id="pp-prompt-generate-btn" onclick="ppGenerateFromPrompt()">Generate Problems</button>
      </div>
    </div>

    <!-- PDF Tab -->
    <div class="gen-tab-panel" id="pp-gen-panel-pdf">
    <div style="margin-bottom:14px">
      <label style="font-size:12px;font-weight:500;display:block;margin-bottom:4px">Target topic</label>
      <select id="pp-pdf-topic-select" style="width:100%;padding:7px 12px;border-radius:8px;border:1px solid var(--border);font-size:13px;background:var(--surface);color:var(--text)">
        <option value="">— Select topic —</option>
        <option value="__new__">+ Create new topic from PDF</option>
      </select>
      <input type="text" id="pp-pdf-new-topic-name" class="prompt-topic-input" placeholder="New topic name (leave blank to auto-name)" style="display:none;margin-top:6px">
    </div>
    <div class="pdf-file-info" id="pp-pdf-file-info" style="display:none">
      <div class="pdf-file-icon">PDF</div>
      <div class="pdf-file-details">
        <div class="pdf-file-name" id="pp-pdf-file-name"></div>
        <div class="pdf-file-meta" id="pp-pdf-file-meta"></div>
      </div>
    </div>
    <div id="pp-pdf-dropzone" style="border:2px dashed var(--border);border-radius:14px;padding:32px 20px;text-align:center;cursor:pointer;transition:all .2s;margin-bottom:20px">
      <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--hint)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:8px">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
      <div style="font-size:14px;font-weight:500;color:var(--text)">Drop PDF here or click to browse</div>
      <div style="font-size:12px;color:var(--hint);margin-top:4px">Supports .pdf files</div>
      <input type="file" accept=".pdf" id="pp-pdf-file-input" style="display:none">
    </div>
    <div id="pp-pdf-page-range" style="display:none;margin-bottom:16px;padding:12px;background:var(--bg);border-radius:8px;border:1px solid var(--border)">
      <div class="dr-info">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--teal)" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
        <span class="dr-title">Page range</span>
        <span class="dr-total" id="pp-pdf-total-pages"></span>
      </div>
      <div class="dual-range" id="pp-pdf-dual-range">
        <div class="dr-track"></div>
        <div class="dr-fill" id="pp-pdf-dr-fill"></div>
        <input type="range" id="pp-pdf-page-start" min="1" max="100" value="1">
        <input type="range" id="pp-pdf-page-end" min="1" max="100" value="100">
      </div>
      <div class="dr-labels">
        <input type="number" class="dr-val dr-input" id="pp-pdf-dr-start-label" value="1" min="1" max="100">
        <span class="dr-sep">&mdash;</span>
        <input type="number" class="dr-val dr-input" id="pp-pdf-dr-end-label" value="100" min="1" max="100">
      </div>
      <div class="dr-tip">Drag handles to select a chapter or section</div>
    </div>
    <textarea class="prompt-textarea" id="pp-pdf-prompt-text" placeholder="Optional: guide the AI on what to focus on...&#10;&#10;Examples:&#10;- Focus on definitions and key theorems&#10;- Create harder computation problems&#10;- Emphasize proof-based exercises&#10;- Skip the introduction, focus on applications" style="display:none;margin-bottom:12px;min-height:72px"></textarea>
    <div class="pdf-slider-section" id="pp-pdf-slider-section" style="display:none">
      <div class="pdf-slider-label">
        <span>Number of problems <span class="pdf-rec" id="pp-pdf-rec-badge">&#x2728; Recommended</span></span>
        <strong id="pp-pdf-count">5</strong>
      </div>
      <div class="pdf-slider-row">
        <span>1</span>
        <input type="range" id="pp-pdf-slider" min="1" max="20" value="5" oninput="ppUpdatePdfSlider(this.value)">
        <span>20</span>
      </div>
    </div>
    <div id="pp-pdf-preview" class="pdf-preview" style="display:none">
      <div class="pdf-preview-header">Preview</div>
      <div class="pdf-preview-strip" id="pp-pdf-preview-strip"></div>
    </div>
    <div class="pdf-actions">
      <button class="pdf-btn pdf-btn-cancel" onclick="ppCloseGenModal()">Cancel</button>
      <button class="pdf-btn pdf-btn-generate" id="pp-pdf-generate-btn" disabled onclick="ppGenerateFromPdf()">Generate Problems</button>
    </div>
    </div>
  </div>
</div>

<script>
/* ── Theme Toggle ────────────────────────────────── */
function toggleTheme(){
  const html=document.documentElement;
  const isDark=html.dataset.theme==='dark';
  html.dataset.theme=isDark?'':'dark';
  localStorage.setItem('theme',isDark?'light':'dark');
  document.getElementById('theme-btn').textContent=isDark?'☼':'☾';
}
(function(){var b=document.getElementById('theme-btn');if(b)b.textContent=document.documentElement.dataset.theme==='dark'?'☾':'☼';})();

/* ── Settings Modal ─────────────────────────────── */
function openSettingsModal() {
  document.getElementById('settings-overlay').classList.add('open');
  fetch('/api/settings').then(r => r.json()).then(d => {
    const inp = document.getElementById('settings-api-key');
    const st = document.getElementById('settings-status');
    inp.value = '';
    inp.placeholder = d.api_key_masked || 'sk-ant-...';
    if (!d.has_anthropic_lib) {
      st.innerHTML = '<span class="no-lib">&#x26A0; anthropic package not installed &mdash; run: pip install anthropic</span>';
    } else if (d.ai_connected) {
      st.innerHTML = '<span class="connected">&#x2714; Connected</span>';
    } else {
      st.innerHTML = '<span class="disconnected">No API key set &mdash; AI features disabled</span>';
    }
  });
}
function closeSettingsModal() {
  document.getElementById('settings-overlay').classList.remove('open');
}
function toggleKeyVisibility() {
  const inp = document.getElementById('settings-api-key');
  inp.type = inp.type === 'password' ? 'text' : 'password';
}
function saveApiKey() {
  const inp = document.getElementById('settings-api-key');
  const key = inp.value.trim();
  const st = document.getElementById('settings-status');
  if (!key && !confirm('Clear the API key? AI features will be disabled.')) return;
  st.innerHTML = '<span style="color:var(--muted)">Saving&hellip;</span>';
  fetch('/api/settings/api-key', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  }).then(r => r.json()).then(d => {
    if (d.ai_connected) {
      st.innerHTML = '<span class="connected">&#x2714; Connected &mdash; saved!</span>';
      toast('API key saved — AI features enabled');
    } else if (key) {
      st.innerHTML = '<span class="disconnected">Key saved (could not verify connection)</span>';
      toast('API key saved', 'info');
    } else {
      st.innerHTML = '<span class="disconnected">API key cleared</span>';
      toast('API key cleared', 'info');
    }
    inp.value = '';
    inp.placeholder = d.api_key_masked || 'sk-ant-...';
  }).catch(() => {
    st.innerHTML = '<span style="color:var(--red)">Failed to save</span>';
  });
}

/* ── Dual Range Slider ──────────────────────────── */
var DR_MAX_SPAN = 40; /* max selectable page range — matches backend ~80k char limit */
function initDualRange(prefix, totalPages, defaultStart, defaultEnd, onChange) {
  const startInput = document.getElementById(prefix + '-page-start');
  const endInput = document.getElementById(prefix + '-page-end');
  const fill = document.getElementById(prefix + '-dr-fill');
  const startLabel = document.getElementById(prefix + '-dr-start-label');
  const endLabel = document.getElementById(prefix + '-dr-end-label');
  if (!startInput || !endInput) return;

  if (defaultEnd - defaultStart + 1 > DR_MAX_SPAN) defaultEnd = defaultStart + DR_MAX_SPAN - 1;
  if (defaultEnd > totalPages) { defaultEnd = totalPages; defaultStart = Math.max(1, totalPages - DR_MAX_SPAN + 1); }

  startInput.min = 1; startInput.max = totalPages;
  endInput.min = 1; endInput.max = totalPages;
  startInput.value = defaultStart;
  endInput.value = defaultEnd;

  /* Show max-range tip */
  var tipEl = document.querySelector('#' + prefix + '-page-range .dr-tip');
  if (tipEl) tipEl.textContent = totalPages <= DR_MAX_SPAN
    ? 'Drag handles to select a chapter or section'
    : 'Max ' + DR_MAX_SPAN + ' pages at a time \u2014 drag to pick a section';

  function update() {
    let s = parseInt(startInput.value);
    let e = parseInt(endInput.value);
    if (s < 1) { s = 1; startInput.value = 1; }
    if (e > totalPages) { e = totalPages; endInput.value = totalPages; }
    if (s > e) {
      if (this === startInput) { e = s; endInput.value = e; }
      else { s = e; startInput.value = s; }
    }
    /* Enforce max span */
    if (e - s + 1 > DR_MAX_SPAN) {
      if (this === startInput) { e = s + DR_MAX_SPAN - 1; if (e > totalPages) { e = totalPages; s = e - DR_MAX_SPAN + 1; startInput.value = s; } endInput.value = e; }
      else { s = e - DR_MAX_SPAN + 1; if (s < 1) { s = 1; e = DR_MAX_SPAN; endInput.value = e; } startInput.value = s; }
    }
    const ratio1 = totalPages > 1 ? (s - 1) / (totalPages - 1) : 0;
    const ratio2 = totalPages > 1 ? (e - 1) / (totalPages - 1) : 1;
    var thumbHalf = 11;
    var trackW = startInput.getBoundingClientRect().width;
    var usable = trackW - thumbHalf * 2;
    var px1 = thumbHalf + ratio1 * usable;
    var px2 = thumbHalf + ratio2 * usable;
    fill.style.left = px1 + 'px';
    fill.style.width = Math.max(0, px2 - px1) + 'px';
    startLabel.value = s;
    endLabel.value = e;
    if (onChange) onChange(s, e);
  }
  startInput.addEventListener('input', update);
  endInput.addEventListener('input', update);

  /* Let users type page numbers — sync range sliders on every keystroke */
  function onLabelChange(isStart) {
    return function() {
      var v = parseInt(this.value);
      if (isNaN(v) || v < 1) v = 1;
      if (v > totalPages) v = totalPages;
      if (isStart) {
        startInput.value = v;
      } else {
        endInput.value = v;
      }
      update.call(isStart ? startInput : endInput);
    };
  }
  startLabel.addEventListener('input', onLabelChange(true));
  endLabel.addEventListener('input', onLabelChange(false));
  startLabel.addEventListener('blur', function() {
    this.value = startInput.value;
  });
  endLabel.addEventListener('blur', function() {
    this.value = endInput.value;
  });
  startLabel.addEventListener('keydown', function(ev){ if(ev.key==='Enter'){ev.preventDefault();this.blur();} });
  endLabel.addEventListener('keydown', function(ev){ if(ev.key==='Enter'){ev.preventDefault();this.blur();} });

  startLabel.min = 1; startLabel.max = totalPages;
  endLabel.min = 1; endLabel.max = totalPages;

  update();
}

/* ── PDF Preview ──────────────────────────────────── */
var _pdfPreviewCache = {};  /* prefix -> { doc, file, items[], observer } */

async function renderPdfPreview(prefix, file) {
  if (!window.pdfjsLib) return;
  var stripEl = document.getElementById(prefix + '-preview-strip');
  var wrapEl = document.getElementById(prefix + '-preview');
  if (!stripEl || !wrapEl) return;

  var cache = _pdfPreviewCache[prefix];
  if (cache && cache.file === file && cache.items.length) {
    wrapEl.style.display = '';
    return;
  }

  /* Clean up previous observer */
  if (cache && cache.observer) cache.observer.disconnect();

  var url = URL.createObjectURL(file);
  var doc = await pdfjsLib.getDocument(url).promise;
  cache = { doc: doc, file: file, items: [] };
  _pdfPreviewCache[prefix] = cache;

  stripEl.innerHTML = '';
  wrapEl.style.display = '';

  /* Get first page to determine aspect ratio for placeholders */
  var firstPage = await doc.getPage(1);
  var vp = firstPage.getViewport({ scale: 1 });
  var pctHeight = (vp.height / vp.width * 100).toFixed(2);

  /* Create lightweight placeholders for ALL pages */
  for (var i = 1; i <= doc.numPages; i++) {
    var wrap = document.createElement('div');
    wrap.className = 'pdf-page-slot';
    wrap.dataset.page = i;
    wrap.style.paddingBottom = pctHeight + '%';
    wrap.innerHTML = '<span class="pdf-page-num">' + i + '</span>';
    stripEl.appendChild(wrap);
    cache.items.push({ el: wrap, rendered: false });
  }

  /* Lazy-render via IntersectionObserver — only render when scrolled into view */
  cache.observer = new IntersectionObserver(function(entries) {
    entries.forEach(function(entry) {
      if (!entry.isIntersecting) return;
      var slot = entry.target;
      var pg = parseInt(slot.dataset.page);
      var item = cache.items[pg - 1];
      if (item.rendered) return;
      item.rendered = true;
      doc.getPage(pg).then(function(page) {
        var scaled = page.getViewport({ scale: 1.5 });
        var canvas = document.createElement('canvas');
        canvas.width = scaled.width;
        canvas.height = scaled.height;
        page.render({ canvasContext: canvas.getContext('2d'), viewport: scaled }).promise.then(function() {
          slot.innerHTML = '';
          slot.style.paddingBottom = '0';
          slot.appendChild(canvas);
        });
      });
    });
  }, { root: stripEl, rootMargin: '200px 0px' });

  for (var j = 0; j < cache.items.length; j++) {
    cache.observer.observe(cache.items[j].el);
  }

  /* Apply initial selection highlight */
  var startEl = document.getElementById(prefix + '-page-start');
  var endEl = document.getElementById(prefix + '-page-end');
  if (startEl && endEl) updatePdfPreviewHighlight(prefix, parseInt(startEl.value), parseInt(endEl.value));
}

function updatePdfPreviewHighlight(prefix, pageStart, pageEnd) {
  var cache = _pdfPreviewCache[prefix];
  if (!cache || !cache.items.length) return;
  for (var i = 0; i < cache.items.length; i++) {
    var pg = i + 1;
    var selected = pg >= pageStart && pg <= pageEnd;
    cache.items[i].el.style.opacity = selected ? '1' : '0.3';
    cache.items[i].el.style.outline = selected ? '2px solid var(--teal)' : 'none';
    cache.items[i].el.style.outlineOffset = selected ? '2px' : '0';
  }
  var firstSel = cache.items[Math.max(0, pageStart - 1)];
  if (firstSel) firstSel.el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/* ── Math helpers ────────────────────────────────── */

/* Render LaTeX in an element using KaTeX auto-render */
function renderMathIn(el) {
  if (typeof renderMathInElement === 'function') {
    renderMathInElement(el, {
      delimiters: [
        {left: '$$', right: '$$', display: true},
        {left: '$', right: '$', display: false},
        {left: '\\(', right: '\\)', display: false},
        {left: '\\[', right: '\\]', display: true},
      ],
      throwOnError: false,
    });
  }
}

/* Render text with math support — returns HTML string */
function renderMathText(text) {
  const span = document.createElement('span');
  span.textContent = text;
  const escaped = span.innerHTML;
  const tmp = document.createElement('span');
  tmp.className = 'rendered-math';
  tmp.innerHTML = escaped;
  renderMathIn(tmp);
  return tmp.innerHTML;
}

let all = [], filter = 'all', lastDueCount = 0, closedCurves = new Set(), tagFilter = '';

const today = () => new Date().toISOString().split('T')[0];

/* ── Toast system ─────────────────────────────────── */
function toast(msg, type='success') {
  const wrap = document.getElementById('toast-wrap');
  const icons = {success:'\u2705', info:'\u2139\ufe0f', warn:'\u26a0\ufe0f'};
  const el = document.createElement('div');
  el.className = 'toast toast-' + type;
  el.innerHTML = `<span class="toast-icon">${icons[type]||''}</span><span>${msg}</span>`;
  wrap.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, 3000);
}

/* ── Browser notifications ────────────────────────── */
function requestNotifPerm() {
  if (!('Notification' in window)) { toast('Browser notifications not supported', 'warn'); return; }
  if (Notification.permission === 'granted') { toast('Notifications already enabled', 'info'); return; }
  Notification.requestPermission().then(p => {
    if (p === 'granted') toast('Notifications enabled!', 'success');
    else toast('Notification permission denied', 'warn');
  });
}

async function checkBrowserNotifications() {
  try {
    const res = await fetch('/api/due-count');
    const data = await res.json();
    const dot = document.getElementById('bell-dot');
    dot.classList.toggle('show', data.total > 0);
    if (data.total > 0 && data.total !== lastDueCount && Notification.permission === 'granted') {
      new Notification('Study Tracker', {
        body: data.total + ' topic(s) due: ' + data.names.slice(0,3).join(', '),
        icon: 'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>\ud83d\udcda</text></svg>'
      });
    }
    lastDueCount = data.total;
  } catch(e) {}
}

/* ── Data loading ─────────────────────────────────── */
async function load() {
  const res = await fetch('/api/topics');
  all = await res.json();
  render();
  checkBrowserNotifications();
}

function toggleAdd() {
  const f = document.getElementById('add-form');
  const isOpen = f.classList.contains('open');
  if (isOpen) {
    f.classList.remove('open');
  } else {
    f.classList.add('open');
    document.getElementById('inp-date').value = today();
    setTimeout(() => document.getElementById('inp-name').focus(), 100);
  }
}

async function addTopic() {
  const name = document.getElementById('inp-name').value.trim();
  const ld   = document.getElementById('inp-date').value || today();
  const tags = (document.getElementById('inp-tags').value || '').trim();
  if (!name) { toast('Please enter a topic name', 'warn'); return; }
  await fetch('/api/topics', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, learned_date:ld, tags}) });
  document.getElementById('inp-name').value = '';
  document.getElementById('inp-tags').value = '';
  document.getElementById('add-form').classList.remove('open');
  toast('"' + name + '" added \u2014 first review scheduled', 'success');
  await load();
  if (schedOpen) loadSchedule();
}

async function markReviewed(id, rating='complete') {
  const t = all.find(x => x.id === id);
  const res = await fetch('/api/topics/'+id+'/review', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rating})
  });
  const data = await res.json();
  if (res.status === 409) {
    toast((t ? t.name : 'Topic') + ' was already reviewed today. Spaced repetition requires sleep between reviews.', 'warn');
    return data;
  }
  toast((t ? t.name : 'Topic') + ' reviewed (' + rating + ')! Next review: ' + (data.next_review || ''), 'success');
  await load();
  if (schedOpen) loadSchedule();
  return data;
}

async function undoReview(id) {
  const t = all.find(x => x.id === id);
  const res = await fetch('/api/topics/'+id+'/undo-review', {method:'POST'});
  const data = await res.json();
  if (!res.ok) { toast(data.error || 'Cannot undo', 'warn'); return; }
  toast((t ? t.name : 'Topic') + ' review undone', 'info');
  await load();
  if (schedOpen) loadSchedule();
}

async function del(id) {
  if (!confirm('Delete this topic?')) return;
  const t = all.find(x => x.id === id);
  await fetch('/api/topics/'+id, {method:'DELETE'});
  toast('"' + (t ? t.name : 'Topic') + '" deleted', 'info');
  await load();
  if (schedOpen) loadSchedule();
}

function editDate(tid, field, currentVal) {
  var el = event.target.closest('.editable-date') || event.target;
  if (el.querySelector('.date-edit-input')) return;
  var label = el.textContent.replace(/\s*\u270E\s*$/, '').trim();
  var inp = document.createElement('input');
  inp.type = 'date';
  inp.className = 'date-edit-input';
  inp.value = currentVal;
  el.textContent = '';
  el.appendChild(inp);
  inp.focus();
  function save() {
    var v = inp.value;
    if (v && v !== currentVal) {
      var body = {}; body[field] = v;
      fetch('/api/topics/' + tid, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      }).then(function(){ toast('Updated', 'success'); load().then(function(){ if (schedOpen) loadSchedule(); }); });
    } else {
      el.textContent = label;
    }
  }
  inp.addEventListener('change', save);
  inp.addEventListener('blur', function() { setTimeout(function(){ if(el.contains(inp)) el.textContent = label; }, 200); });
}

function editField(tid, field, currentVal, inputType) {
  var el = event.target.closest('.editable-field') || event.target;
  if (el.querySelector('.field-edit-input')) return;
  var label = el.textContent.trim();
  var inp = document.createElement('input');
  inp.type = inputType || 'text';
  inp.className = 'field-edit-input';
  inp.value = currentVal;
  if (inputType === 'number') { inp.min = '0'; inp.style.width = '60px'; }
  else { inp.style.width = Math.max(100, el.offsetWidth + 20) + 'px'; }
  el.textContent = '';
  el.appendChild(inp);
  inp.focus();
  inp.select();
  function save() {
    var v = inp.value.trim();
    if (inputType === 'number') v = parseInt(v) || 0;
    if (v !== '' && String(v) !== String(currentVal)) {
      var body = {}; body[field] = v;
      fetch('/api/topics/' + tid, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      }).then(function(){ toast('Updated', 'success'); load().then(function(){ if (schedOpen) loadSchedule(); }); });
    } else {
      el.textContent = label;
    }
  }
  inp.addEventListener('keydown', function(ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); save(); }
    if (ev.key === 'Escape') { el.textContent = label; }
  });
  inp.addEventListener('blur', function() { setTimeout(function(){ if(el.contains(inp)) save(); }, 150); });
}

/* ── Collapsible curve ───────────────────────────── */
function toggleCurve(tid) {
  if (closedCurves.has(tid)) closedCurves.delete(tid);
  else closedCurves.add(tid);
  const wrap = document.getElementById('curve-'+tid);
  const toggle = wrap ? wrap.previousElementSibling : null;
  if (wrap) wrap.classList.toggle('closed', closedCurves.has(tid));
  if (toggle) toggle.classList.toggle('closed', closedCurves.has(tid));
}

async function toggleHistory(tid) {
  const wrap = document.getElementById('hist-'+tid);
  if (!wrap) return;
  if (wrap.classList.contains('open')) {
    wrap.classList.remove('open');
    return;
  }
  if (!wrap.dataset.loaded) {
    try {
      const res = await fetch('/api/topics/'+tid+'/history');
      const data = await res.json();
      wrap.innerHTML = renderHistoryChart(data);
      wrap.dataset.loaded = '1';
    } catch(e) {
      wrap.innerHTML = '<div style="font-size:12px;color:var(--hint);padding:6px">No history data yet.</div>';
      wrap.dataset.loaded = '1';
    }
  }
  wrap.classList.add('open');
}

function renderHistoryChart(data) {
  const dates = data.history_dates || [];
  const snaps = data.curve_snapshots || [];
  if (dates.length < 2 && snaps.length < 2) {
    return '<div style="font-size:12px;color:var(--hint);padding:6px">Not enough review data for a chart yet.</div>';
  }
  const W = 380, H = 110, pl = 40, pr = 14, pt = 20, pb = 24;
  const pw = W - pl - pr, ph = H - pt - pb;
  const startDate = new Date(dates[0] + 'T00:00:00');
  const endDate = new Date(dates[dates.length-1] + 'T00:00:00');
  const spanDays = Math.max(1, (endDate - startDate) / 86400000);
  const xOf = d => pl + ((new Date(d+'T00:00:00') - startDate) / 86400000 / spanDays) * pw;
  const yOf = r => pt + (1 - Math.max(0, Math.min(1, r))) * ph;
  const hid = 'h' + Math.random().toString(36).slice(2,8);

  let svg = '';

  /* Defs */
  svg += '<defs>';
  svg += '<linearGradient id="'+hid+'-fill" x1="0" y1="0" x2="0" y2="1">';
  svg += '<stop offset="0%" stop-color="var(--teal)" stop-opacity=".12"/>';
  svg += '<stop offset="100%" stop-color="var(--teal)" stop-opacity=".01"/>';
  svg += '</linearGradient>';
  svg += '<filter id="'+hid+'-glow"><feGaussianBlur stdDeviation="2" result="blur"/>';
  svg += '<feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>';
  svg += '</defs>';

  /* 80% threshold zone */
  const threshY = yOf(0.8);
  svg += '<rect x="'+pl+'" y="'+threshY.toFixed(1)+'" width="'+pw+'" height="'+(H-pb-threshY).toFixed(1)+'" fill="var(--red)" opacity=".03" rx="2"/>';

  /* Grid lines */
  [{v:1,l:'100%'},{v:0.8,l:'80%'},{v:0.5,l:'50%'}].forEach(function(g) {
    var y = yOf(g.v);
    var op = g.v === 0.8 ? '0.5' : '0.25';
    svg += '<line x1="'+pl+'" y1="'+y.toFixed(1)+'" x2="'+(W-pr)+'" y2="'+y.toFixed(1)+'" stroke="var(--border)" stroke-width="'+(g.v===0.8?'1':'0.5')+'"'+(g.v===0.8?' stroke-dasharray="5,3"':'')+' opacity="'+op+'"/>';
    svg += '<text x="'+(pl-6)+'" y="'+(y+3).toFixed(1)+'" font-size="8" fill="var(--hint)" text-anchor="end" font-weight="'+(g.v===0.8?'600':'400')+'" opacity="'+(g.v===0.8?'1':'.6')+'">'+g.l+'</text>';
  });

  const rColors = {complete:'var(--green)', partial:'#EF9F27', failed:'var(--red)'};

  if (snaps.length >= 2) {
    var pathPts = [];
    snaps.forEach(function(s, i) {
      var x = xOf(s.date);
      if (i > 0) {
        var prev = snaps[i-1];
        var prevA = prev.a || 0.2, prevK = prev.k || 0.3;
        var daysBetween = (new Date(s.date+'T00:00:00') - new Date(prev.date+'T00:00:00')) / 86400000;
        var retBefore = prevA + (1 - prevA) * Math.exp(-prevK * daysBetween);
        pathPts.push({x: x, y: yOf(retBefore)});
      }
      var retAfter = s.rating === 'failed' ? (s.a || 0.2) + (1 - (s.a || 0.2)) * 0.5 : 1.0;
      pathPts.push({x: x, y: yOf(retAfter)});
    });

    /* Fill area under curve */
    if (pathPts.length > 0) {
      var fillD = pathPts.map(function(p, i) { return (i===0?'M':'L') + p.x.toFixed(1) + ',' + p.y.toFixed(1); }).join('');
      fillD += ' L'+pathPts[pathPts.length-1].x.toFixed(1)+','+(H-pb).toFixed(1)+' L'+pathPts[0].x.toFixed(1)+','+(H-pb).toFixed(1)+' Z';
      svg += '<path d="' + fillD + '" fill="url(#'+hid+'-fill)"/>';
    }

    /* Draw the connected path */
    if (pathPts.length > 0) {
      var pathD = pathPts.map(function(p, i) { return (i===0?'M':'L') + p.x.toFixed(1) + ',' + p.y.toFixed(1); }).join('');
      svg += '<path d="' + pathD + '" fill="none" stroke="var(--teal)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>';
    }

    /* Draw dots at review points */
    snaps.forEach(function(s) {
      var x = xOf(s.date);
      var clr = rColors[s.rating] || 'var(--teal)';
      svg += '<circle cx="'+x.toFixed(1)+'" cy="'+yOf(1.0).toFixed(1)+'" r="4.5" fill="var(--surface)" stroke="'+clr+'" stroke-width="2" filter="url(#'+hid+'-glow)"><title>'+s.date+': '+s.rating+'</title></circle>';
      svg += '<circle cx="'+x.toFixed(1)+'" cy="'+yOf(1.0).toFixed(1)+'" r="1.5" fill="'+clr+'"/>';
    });
  } else {
    dates.forEach(function(d) {
      var x = xOf(d);
      svg += '<circle cx="'+x.toFixed(1)+'" cy="'+(pt+4).toFixed(1)+'" r="4" fill="var(--surface)" stroke="var(--teal)" stroke-width="2"><title>'+d+'</title></circle>';
    });
  }

  /* Date labels */
  svg += '<text x="'+pl+'" y="'+(H-5)+'" font-size="8" fill="var(--hint)" font-weight="500">'+dates[0]+'</text>';
  svg += '<text x="'+(W-pr)+'" y="'+(H-5)+'" font-size="8" fill="var(--hint)" text-anchor="end" font-weight="500">'+dates[dates.length-1]+'</text>';

  /* Legend */
  var legendX = pl;
  var legendY = pt - 8;
  [{c:'var(--green)',l:'Complete'},{c:'#EF9F27',l:'Partial'},{c:'var(--red)',l:'Failed'}].forEach(function(item) {
    svg += '<circle cx="'+legendX+'" cy="'+legendY+'" r="3" fill="'+item.c+'"/>';
    svg += '<text x="'+(legendX+6)+'" y="'+(legendY+3)+'" font-size="7" fill="var(--hint)">'+item.l+'</text>';
    legendX += 50;
  });

  return '<div style="padding:6px 0"><div style="font-size:11px;color:var(--muted);margin-bottom:4px;font-weight:500">' +
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>' +
    'Review History <span style="font-weight:400;color:var(--hint)">&middot; '+dates.length+' sessions</span></div>' +
    '<svg width="100%" viewBox="0 0 '+W+' '+H+'" style="display:block">'+svg+'</svg></div>';
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

/* ── Study session ────────────────────────────────── */
let ssState = null;

async function startStudySession(tid, practice = false) {
  const t = all.find(x => x.id === tid);
  if (!t) return;
  if (t.reviewed_today && !practice) {
    toast(t.name + ' was already reviewed today.', 'warn');
    return;
  }
  try {
    const res = await fetch('/api/topics/'+tid+'/cards');
    const cards = await res.json();
    if (!cards.length) {
      toast('Add some flashcards first before starting a review session.', 'warn');
      toggleCardEditor(tid);
      return;
    }
    /* Sort: lower box first (harder), then higher fail_count first, then shuffle within groups */
    cards.sort((a, b) => {
      if (a.box !== b.box) return a.box - b.box;
      if (a.fail_count !== b.fail_count) return b.fail_count - a.fail_count;
      return Math.random() - 0.5;
    });
    ssState = {
      topicId: tid,
      topicName: t.name,
      cards: cards,
      current: 0,
      phase: 'question',  /* question | answer | summary */
      ratings: [],         /* per-card rating */
      userAnswers: [],     /* user's typed answers or selected choice */
      mcChoices: [],       /* per-card shuffled MC options (for qa cards) */
      mcSelected: [],      /* per-card selected MC index (-1 = none) */
      practice: practice   /* practice mode — don't update curve */
    };
    /* Pre-generate multiple choice options for each qa card */
    ssState.cards.forEach((card, i) => {
      if (card.card_type === 'qa' && card.answer) {
        const storedWo = (card.wrong_options && Array.isArray(card.wrong_options)) ? card.wrong_options.filter(w => w && w.trim()) : [];
        const wrongs = [];
        const seen = new Set([card.answer.toLowerCase().trim()]);

        /* First use stored wrong options */
        for (const w of storedWo) {
          const key = w.toLowerCase().trim();
          if (!seen.has(key) && wrongs.length < 3) {
            wrongs.push(w);
            seen.add(key);
          }
        }

        /* Fill remaining from other cards if needed */
        if (wrongs.length < 3) {
          const allAnswers = cards.filter((c, j) => j !== i && c.answer && c.card_type === 'qa').map(c => c.answer);
          const shuffled = allAnswers.sort(() => Math.random() - 0.5);
          for (const a of shuffled) {
            const key = a.toLowerCase().trim();
            if (!seen.has(key) && wrongs.length < 3) {
              wrongs.push(a);
              seen.add(key);
            }
          }
        }

        /* If still not enough, add generic fillers */
        const fillers = ['None of the above', 'All of the above', 'Not enough information'];
        for (const f of fillers) {
          if (wrongs.length >= 3) break;
          if (!seen.has(f.toLowerCase())) { wrongs.push(f); seen.add(f.toLowerCase()); }
        }
        /* Build choices array and shuffle */
        const choices = [{text: card.answer, correct: true}];
        wrongs.forEach(w => choices.push({text: w, correct: false}));
        for (let j = choices.length - 1; j > 0; j--) {
          const k = Math.floor(Math.random() * (j + 1));
          [choices[j], choices[k]] = [choices[k], choices[j]];
        }
        ssState.mcChoices[i] = choices;
      } else {
        ssState.mcChoices[i] = null;
      }
      ssState.mcSelected[i] = -1;
    });
    renderSession();
  } catch(e) {
    toast('Failed to load cards', 'warn');
  }
}

/* Weighted overall rating: complete=1, partial=0.5, failed=0 */
function calcOverall(ratings) {
  if (!ratings.length) return 'complete';
  const sm = {complete:1, partial:0.5, failed:0};
  const avg = ratings.reduce((s,r) => s + (sm[r]||1), 0) / ratings.length;
  if (avg >= 0.8) return 'complete';
  if (avg >= 0.4) return 'partial';
  return 'failed';
}

function renderSession() {
  if (!ssState) return;
  let existing = document.getElementById('ss-overlay');
  if (!existing) {
    existing = document.createElement('div');
    existing.id = 'ss-overlay';
    existing.className = 'ss-overlay';
    document.body.appendChild(existing);
  }
  const s = ssState;

  if (s.phase === 'summary') {
    const complete = s.ratings.filter(r => r === 'complete').length;
    const partial = s.ratings.filter(r => r === 'partial').length;
    const failed = s.ratings.filter(r => r === 'failed').length;
    const overall = calcOverall(s.ratings);
    const ratingLabels = {complete:'Complete Recall', partial:'Partial Recall', failed:'Failed Recall'};
    const ratingColors = {complete:'var(--green)', partial:'var(--amber)', failed:'var(--red)'};
    const scoreMap = {complete:1, partial:0.5, failed:0};
    const avg = s.ratings.length ? s.ratings.reduce((sum,r) => sum + (scoreMap[r]||1), 0) / s.ratings.length : 1;
    const pct = Math.round(avg * 100);
    existing.innerHTML = `
      <div class="ss-header">
        <h2>${escHtml(s.topicName)} \u2014 ${s.practice ? 'Practice' : 'Session'} Complete</h2>
        ${s.practice ? '<span style="font-size:11px;background:var(--blue-light);color:var(--blue);padding:2px 10px;border-radius:8px;font-weight:600">\u{1F501} Practice Mode</span>' : ''}
        <button class="ss-close" onclick="closeSession()">&times;</button>
      </div>
      <div class="ss-body">
        <div class="ss-summary">
          <h3>\u2705 Review Complete!</h3>
          <div class="ss-summary-stats">
            <div class="ss-summary-stat" style="border-color:var(--green-mid)">
              <div class="ss-stat-num" style="color:var(--green)">${complete}</div>
              <div class="ss-stat-lbl">Complete</div>
            </div>
            <div class="ss-summary-stat" style="border-color:var(--amber-mid)">
              <div class="ss-stat-num" style="color:var(--amber)">${partial}</div>
              <div class="ss-stat-lbl">Partial</div>
            </div>
            <div class="ss-summary-stat" style="border-color:var(--red-mid)">
              <div class="ss-stat-num" style="color:var(--red)">${failed}</div>
              <div class="ss-stat-lbl">Failed</div>
            </div>
          </div>
          <p style="margin-bottom:1rem;color:var(--muted);font-size:13px">
            Score: <strong>${pct}%</strong> \u2014
            Overall: <strong style="color:${ratingColors[overall]}">${ratingLabels[overall]}</strong>
          </p>
          <div class="ss-breakdown">
            <div class="ss-breakdown-title">Card Results</div>
            ${s.cards.map((c, i) => {
              const r = s.ratings[i] || 'complete';
              const rlbl = {complete:'\u2713', partial:'\u00BD', failed:'\u2717'};
              return '<div class="ss-card-row"><span class="ss-card-q">' + escHtml(c.question) + '</span><span class="ss-card-badge ' + r + '">' + rlbl[r] + ' ' + r.charAt(0).toUpperCase() + r.slice(1) + '</span></div>';
            }).join('')}
          </div>
          <button class="ss-done-btn" onclick="finishSession('${overall}')">${s.practice ? 'Close' : 'Save &amp; Close'}</button>
        </div>
      </div>`;
    return;
  }

  const card = s.cards[s.current];
  const typeLabels = {qa:'Q & A', recall:'Free Recall'};
  const progress = (s.current + 1) + ' / ' + s.cards.length;
  const pctDone = Math.round(((s.current) / s.cards.length) * 100);
  const progressBarHtml = '<div class="ss-progress-bar"><div class="ss-progress-fill" style="width:' + pctDone + '%"></div></div>';

  if (s.phase === 'question') {
    const choices = s.mcChoices[s.current];
    const isMultiChoice = choices && choices.length > 1;
    const placeholder = card.card_type === 'recall' ? 'Write everything you can recall...' :
                        'Type your answer (optional)...';
    const letters = ['A','B','C','D','E','F'];
    let bodyHtml = `<div class="ss-card-type">${typeLabels[card.card_type] || card.card_type}</div>
        <div class="ss-question rendered-math">${renderMathText(card.question)}</div>`;

    if (isMultiChoice) {
      bodyHtml += '<div class="mc-options">';
      choices.forEach((opt, idx) => {
        bodyHtml += `<button class="mc-opt" onclick="selectMcOption(${idx})" data-idx="${idx}">
          <span class="mc-letter">${letters[idx] || idx}</span>
          <span class="rendered-math">${renderMathText(opt.text)}</span>
        </button>`;
      });
      bodyHtml += '</div>';
    } else {
      bodyHtml += `<div class="ss-answer-area">
            <textarea id="ss-answer" rows="3"
              oninput="livePreview('ss-answer','ss-answer-preview')"
              style="width:100%;min-height:100px;resize:vertical;
              font-family:inherit;font-size:15px;background:var(--surface);color:var(--text);outline:none;
              border:2px solid var(--teal);border-radius:10px;padding:12px;
              line-height:1.5;"
              placeholder="${placeholder}"></textarea>
            <div id="ss-answer-preview" class="math-preview rendered-math"></div>
            <div class="math-helper" style="margin-top:6px">
              <math-field id="mf-ss-helper" virtual-keyboard-mode="onfocus" style="flex:1;min-height:38px;font-size:14px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:8px"></math-field>
              <button type="button" class="mh-insert" onclick="insertMath('mf-ss-helper','ss-answer')">&#x2795; Insert Math</button>
            </div>
          </div>
          <button class="ss-reveal-btn" onclick="revealAnswer()">Reveal Answer <span style="font-size:11px;opacity:.6;margin-left:4px">[Enter]</span></button>`;
    }

    existing.innerHTML = `
      <div class="ss-header">
        ${s.current > 0 ? '<button class="ss-back" onclick="goBackCard()" title="Go back to previous card">&larr;</button>' : ''}
        <h2>${escHtml(s.topicName)}</h2>
        <span class="ss-progress">${progress}</span>
        <button class="ss-close" onclick="closeSession()">&times;</button>
      </div>
      ${progressBarHtml}
      <div class="ss-body">${bodyHtml}</div>`;
    if (!isMultiChoice) {
      setTimeout(() => { const ta = document.getElementById('ss-answer'); if (ta) ta.focus(); }, 100);
    }
  } else if (s.phase === 'answer') {
    const userAns = s.userAnswers[s.current] || '';
    const choices = s.mcChoices[s.current];
    const isMc = choices && choices.length > 1;
    const mcIdx = s.mcSelected[s.current];
    const showAi = !isMc && !!userAns;
    const letters = ['A','B','C','D','E','F'];

    let answerBodyHtml = `<div class="ss-card-type">${typeLabels[card.card_type] || card.card_type}</div>
        <div class="ss-question rendered-math">${renderMathText(card.question)}</div>`;

    if (isMc) {
      /* Show MC options with correct/wrong highlighting */
      answerBodyHtml += '<div class="mc-options">';
      choices.forEach((opt, idx) => {
        let cls = 'mc-opt dimmed';
        if (opt.correct) cls = 'mc-opt correct';
        else if (idx === mcIdx) cls = 'mc-opt wrong';
        answerBodyHtml += `<button class="${cls}" style="cursor:default;pointer-events:none">
          <span class="mc-letter">${letters[idx] || idx}</span>
          <span class="rendered-math">${renderMathText(opt.text)}</span>
        </button>`;
      });
      answerBodyHtml += '</div>';
      answerBodyHtml += `<p style="margin:.5rem 0 .8rem;color:var(--muted);font-size:13px">How well did you know this?</p>
        <div class="ss-rate">
          <button class="rate-fail" onclick="rateCard('failed')">&cross; Failed <span style="font-size:10px;opacity:.5">[1]</span></button>
          <button class="rate-partial" onclick="rateCard('partial')">&frac12; Partial <span style="font-size:10px;opacity:.5">[2]</span></button>
          <button class="rate-complete" onclick="rateCard('complete')">&check; Complete <span style="font-size:10px;opacity:.5">[3]</span></button>
        </div>`;
    } else {
      /* Original free-response answer phase */
      if (card.answer) {
        answerBodyHtml += `<div class="ss-answer-box">
            <div class="ss-answer-label">Answer</div>
            <div class="ss-answer-text rendered-math">${renderMathText(card.answer)}</div>
          </div>`;
      }
      if (userAns) {
        answerBodyHtml += `<div class="ss-your-answer">
            <strong>Your answer:</strong> <span class="rendered-math">${renderMathText(userAns)}</span>
          </div>`;
      }
      if (showAi) {
        answerBodyHtml += '<div class="ai-panel" id="ai-panel"><div class="ai-loading"><div class="ai-spinner"></div>AI is evaluating your answer...</div></div>';
      }
      answerBodyHtml += `<p style="margin:1rem 0 .8rem;color:var(--muted);font-size:13px">How well did you recall this?</p>
        <div class="ss-rate">
          <button class="rate-fail" onclick="rateCard('failed')">&cross; Failed <span style="font-size:10px;opacity:.5">[1]</span></button>
          <button class="rate-partial" onclick="rateCard('partial')">&frac12; Partial <span style="font-size:10px;opacity:.5">[2]</span></button>
          <button class="rate-complete" onclick="rateCard('complete')">&check; Complete <span style="font-size:10px;opacity:.5">[3]</span></button>
        </div>`;
    }

    existing.innerHTML = `
      <div class="ss-header">
        <button class="ss-back" onclick="goBackCard()" title="Redo this card from scratch">&larr;</button>
        <h2>${escHtml(s.topicName)}</h2>
        ${s.practice ? '<span style="font-size:10px;background:var(--blue-light);color:var(--blue);padding:2px 8px;border-radius:6px;font-weight:600">\u{1F501} Practice</span>' : ''}
        <span class="ss-progress">${progress}</span>
        <button class="ss-close" onclick="closeSession()">&times;</button>
      </div>
      ${progressBarHtml}
      <div class="ss-body">${answerBodyHtml}</div>`;
  }
}

function revealAnswer() {
  if (!ssState) return;
  const ta = document.getElementById('ss-answer');
  ssState.userAnswers[ssState.current] = ta ? ta.value.trim() : '';
  ssState.phase = 'answer';
  ssState.aiEval = null; /* reset AI eval for this card */
  renderSession();
  /* Auto-trigger AI evaluation if user typed an answer */
  const userAns = ssState.userAnswers[ssState.current];
  if (userAns) {
    triggerAiEval();
  }
}

function insertMath(mfId, targetId) {
  const mf = document.getElementById(mfId);
  const ta = document.getElementById(targetId);
  if (!mf || !ta) return;
  const latex = mf.value.trim();
  if (!latex) return;
  const start = ta.selectionStart || ta.value.length;
  const end = ta.selectionEnd || ta.value.length;
  const before = ta.value.substring(0, start);
  const after = ta.value.substring(end);
  ta.value = before + '$' + latex + '$' + after;
  mf.value = '';
  ta.focus();
  const newPos = start + latex.length + 2;
  ta.setSelectionRange(newPos, newPos);
  ta.dispatchEvent(new Event('input'));
}

/* Live preview: render LaTeX from textarea into a preview div */
function livePreview(srcId, prevId) {
  const src = document.getElementById(srcId);
  const prev = document.getElementById(prevId);
  if (!src || !prev) return;
  const val = src.value.trim();
  if (!val || !val.includes('$')) { prev.style.display = 'none'; prev.innerHTML = ''; return; }
  prev.style.display = 'block';
  prev.textContent = val;
  renderMathIn(prev);
}

/* Shift content up when MathLive virtual keyboard opens in study session */
document.addEventListener('focusin', function(e) {
  if (!e.target || e.target.tagName !== 'MATH-FIELD') return;
  var overlay = document.querySelector('.ss-overlay');
  if (!overlay) return;
  overlay.classList.add('vk-open');
  var helper = overlay.querySelector('.math-helper');
  if (helper) setTimeout(function(){ helper.scrollIntoView({behavior:'smooth', block:'center'}); }, 200);
});
document.addEventListener('focusout', function(e) {
  if (!e.target || e.target.tagName !== 'MATH-FIELD') return;
  setTimeout(function() {
    var active = document.activeElement;
    if (active && active.tagName === 'MATH-FIELD') return;
    var overlay = document.querySelector('.ss-overlay');
    if (overlay) overlay.classList.remove('vk-open');
  }, 150);
});

function selectMcOption(idx) {
  if (!ssState || ssState.phase !== 'question') return;
  const s = ssState;
  const choices = s.mcChoices[s.current];
  if (!choices) return;
  s.mcSelected[s.current] = idx;
  const selected = choices[idx];
  s.userAnswers[s.current] = selected.text;

  /* Highlight selected, then reveal after brief delay */
  const btns = document.querySelectorAll('.mc-opt');
  btns.forEach((btn, i) => {
    btn.classList.remove('selected');
    btn.onclick = null;
    btn.style.cursor = 'default';
    if (i === idx) btn.classList.add('selected');
  });

  setTimeout(() => {
    /* Show correct/wrong states */
    btns.forEach((btn, i) => {
      btn.classList.add('dimmed');
      if (choices[i].correct) {
        btn.classList.remove('dimmed');
        btn.classList.add('correct');
      }
      if (i === idx && !choices[i].correct) {
        btn.classList.remove('dimmed');
        btn.classList.add('wrong');
      }
    });
    /* Auto-rate based on selection and move to answer phase after a moment */
    const wasCorrect = selected.correct;
    setTimeout(() => {
      if (wasCorrect) {
        /* Correct MC answer — auto-rate complete, skip answer phase */
        rateCard('complete');
      } else {
        /* Wrong MC answer — auto-rate as failed */
          rateCard('failed');
      }
    }, 1200);
  }, 400);
}

async function triggerAiEval() {
  if (!ssState) return;
  const s = ssState;
  const card = s.cards[s.current];
  const userAns = s.userAnswers[s.current];
  if (!userAns) return;
  /* Show loading state */
  const panel = document.getElementById('ai-panel');
  if (panel) panel.innerHTML = '<div class="ai-loading"><div class="ai-spinner"></div>AI is evaluating your answer...</div>';
  try {
    const res = await fetch('/api/evaluate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        question: card.question,
        correct_answer: card.answer || '',
        user_answer: userAns,
        topic_id: s.topicId,
        card_id: card.id,
        topic_name: s.topicName
      })
    });
    const data = await res.json();
    if (data.error) {
      if (panel) panel.innerHTML = '<div class="ai-loading" style="color:var(--red)">AI unavailable: ' + escHtml(data.error) + '</div>';
      return;
    }
    ssState.aiEval = data;
    renderAiPanel();
  } catch(e) {
    if (panel) panel.innerHTML = '<div class="ai-loading" style="color:var(--red)">AI evaluation failed</div>';
  }
}

function renderAiPanel() {
  const panel = document.getElementById('ai-panel');
  if (!panel || !ssState || !ssState.aiEval) return;
  const ev = ssState.aiEval;
  const rLabels = {complete:'Complete', partial:'Partial', failed:'Failed'};
  const rIcons = {complete:'\u2713', partial:'\u00BD', failed:'\u2717'};
  let html = '<div class="ai-panel-header"><span class="ai-icon">\ud83e\udd16</span> AI Evaluation</div>';
  html += '<div class="ai-verdict"><span class="ai-rating-badge ' + ev.rating + '">' + rIcons[ev.rating] + ' ' + rLabels[ev.rating] + '</span></div>';
  if (ev.explanation) html += '<div class="ai-explanation">' + escHtml(ev.explanation) + '</div>';
  if (ev.key_missing) html += '<div class="ai-missing"><strong>Missing:</strong> ' + escHtml(ev.key_missing) + '</div>';
  panel.innerHTML = html;
}

function acceptAiRating() {
  if (!ssState || !ssState.aiEval) return;
  rateCard(ssState.aiEval.rating);
}

async function overrideAiRating(newRating) {
  if (!ssState || !ssState.aiEval) return;
  const s = ssState;
  const card = s.cards[s.current];
  const ev = s.aiEval;
  /* Save the correction for future training */
  try {
    await fetch('/api/evaluate/override', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        topic_id: s.topicId,
        card_id: card.id,
        question: card.question,
        correct_answer: card.answer || '',
        user_answer: s.userAnswers[s.current] || '',
        ai_rating: ev.rating,
        user_override: newRating,
        explanation: ev.explanation || ''
      })
    });
    toast('Override saved \u2014 AI will learn from this', 'info');
  } catch(e) { /* silent */ }
  rateCard(newRating);
}

function rateCard(rating) {
  if (!ssState) return;
  /* If AI evaluated and user chose a different rating, save the correction */
  if (ssState.aiEval && ssState.aiEval.rating !== rating) {
    const s = ssState;
    const card = s.cards[s.current];
    const ev = s.aiEval;
    fetch('/api/evaluate/override', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        topic_id: s.topicId, card_id: card.id,
        question: card.question, correct_answer: card.answer || '',
        user_answer: s.userAnswers[s.current] || '',
        ai_rating: ev.rating, user_override: rating,
        explanation: ev.explanation || ''
      })
    }).catch(() => {});
  }
  ssState.ratings[ssState.current] = rating;
  ssState.current++;
  if (ssState.current >= ssState.cards.length) {
    ssState.phase = 'summary';
  } else {
    ssState.phase = 'question';
  }
  renderSession();
}

/* Hidden undo — go back to previous card to redo answer (e.g. accidental Enter) */
function goBackCard() {
  if (!ssState || ssState.current <= 0) return;
  ssState.current--;
  ssState.ratings[ssState.current] = undefined;
  ssState.userAnswers[ssState.current] = '';
  ssState.mcSelected[ssState.current] = -1;
  ssState.phase = 'question';
  ssState.aiEval = null;
  renderSession();
}

async function finishSession(overall) {
  if (!ssState) return;
  const tid = ssState.topicId;
  const isPractice = ssState.practice;
  /* Build per-card ratings array */
  const card_ratings = ssState.cards.map((c, i) => ({
    card_id: c.id,
    rating: ssState.ratings[i] || 'complete'
  }));
  if (isPractice) {
    /* Practice mode — don't update curve or card stats */
    const t = all.find(x => x.id === tid);
    toast((t ? t.name : 'Topic') + ' practice complete (' + overall + '). Curve unchanged.', 'success');
    closeSession();
    return;
  }
  try {
    const res = await fetch('/api/topics/'+tid+'/session', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({card_ratings})
    });
    const data = await res.json();
    if (res.status === 409) {
      const t = all.find(x => x.id === tid);
      toast((t ? t.name : 'Topic') + ' was already reviewed today.', 'warn');
    } else if (res.ok) {
      const t = all.find(x => x.id === tid);
      toast((t ? t.name : 'Topic') + ' reviewed (' + data.rating + ')! Next: ' + (data.next_review || ''), 'success');
      await load();
      if (schedOpen) loadSchedule();
      /* Auto-vary cards after 2+ consecutive quizzes on the same topic */
      if (data.consecutive_reviews && data.consecutive_reviews >= 2) {
        try {
          const vRes = await fetch('/api/topics/'+tid+'/vary', {method:'POST'});
          const vData = await vRes.json();
          if (vRes.ok && vData.varied) {
            toast('\uD83D\uDD04 Cards varied for your next review! (' + vData.varied + ' cards)', 'success');
          }
        } catch(ve) { /* variation is best-effort */ }
      }
    }
  } catch(e) {
    toast('Failed to save session', 'warn');
  }
  closeSession();
}

function closeSession() {
  ssState = null;
  const el = document.getElementById('ss-overlay');
  if (el) el.remove();
}

/* Keyboard shortcuts for study session */
document.addEventListener('keydown', e => {
  /* Escape closes practice problem overlay if open */
  if (e.key === 'Escape' && ppSolveState) {
    e.preventDefault();
    ppCloseSolve();
    return;
  }
  if (!ssState) return;
  /* Detect typing: check activeElement, its shadowRoot host, and composedPath for math-field */
  const active = document.activeElement;
  const isTyping = active && (
    active.tagName === 'TEXTAREA' ||
    active.tagName === 'INPUT' ||
    active.tagName === 'MATH-FIELD' ||
    (active.shadowRoot && active.matches && active.matches('math-field'))
  );
  /* If user is inside any editable field, let the field handle all keys */
  if (isTyping) {
    /* Only intercept Enter (reveal answer) — let everything else pass through */
    if (ssState.phase === 'question' && e.code === 'Enter' && !e.shiftKey) {
      const choices = ssState.mcChoices[ssState.current];
      if (!choices || choices.length <= 1) {
        e.preventDefault();
        revealAnswer();
      }
    }
    return;
  }
  /* Not typing — handle shortcuts */
  if (ssState.phase === 'question') {
    const choices = ssState.mcChoices[ssState.current];
    if (choices && choices.length > 1) {
      /* MC mode: A/B/C/D or 1/2/3/4 to select */
      const keyMap = {a:0, b:1, c:2, d:3, '1':0, '2':1, '3':2, '4':3};
      const idx = keyMap[e.key.toLowerCase()];
      if (idx !== undefined && idx < choices.length) {
        e.preventDefault();
        selectMcOption(idx);
      }
    }
  } else if (ssState.phase === 'answer') {
    if (e.key === '1') { e.preventDefault(); rateCard('failed'); }
    else if (e.key === '2') { e.preventDefault(); rateCard('partial'); }
    else if (e.key === '3') { e.preventDefault(); rateCard('complete'); }
  } else if (ssState.phase === 'summary' && (e.code === 'Enter' || e.code === 'Space')) {
    e.preventDefault();
    finishSession(calcOverall(ssState.ratings));
  }
  if (e.key === 'Escape') {
    e.preventDefault();
    if (ssState.current > 0 && ssState.phase !== 'summary') {
      if (confirm('End this study session? Your progress so far will be lost.')) closeSession();
    } else {
      closeSession();
    }
  }
  /* Backspace = go back to previous card (hidden undo) */
  if (e.key === 'Backspace' && ssState.phase !== 'summary') {
    e.preventDefault();
    goBackCard();
  }
});

function setFilter(f) {
  filter = f;
  document.querySelectorAll('.filters .filter-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('f-'+f).classList.add('active');
  render();
}

function setTagFilter(tag) {
  tagFilter = tag;
  render();
}

function retColor(pct) {
  if (pct >= 80) return '#3B8C2A';
  if (pct >= 55) return '#EF9F27';
  return '#E24B4A';
}

/* Mini SVG curve showing decay from last review with current position marked */
function curveSVG(a, k, daysElapsed, nextInterval) {
  const W = 380, H = 120, pl = 40, pr = 16, pt = 24, pb = 24;
  const maxD = Math.max(nextInterval * 1.8, daysElapsed + 5, 14);
  const pw = W - pl - pr, ph = H - pt - pb;
  const xOf = d => pl + (d / maxD) * pw;
  const yOf = r => pt + (1 - r) * ph;
  const uid = 'c' + Math.random().toString(36).slice(2,8);

  /* curve path + fill */
  let pathD = '';
  for (let i = 0; i <= 200; i++) {
    const d = (i / 200) * maxD;
    const r = a + (1 - a) * Math.exp(-k * d);
    pathD += (i === 0 ? 'M' : 'L') + xOf(d).toFixed(1) + ',' + yOf(r).toFixed(1);
  }
  const fillPath = pathD + ` L${(W-pr).toFixed(1)},${yOf(0).toFixed(1)} L${pl},${yOf(0).toFixed(1)} Z`;

  const curRet = a + (1-a)*Math.exp(-k*daysElapsed);
  const curX  = xOf(Math.min(daysElapsed, maxD));
  const curY  = yOf(curRet);
  const nextX = xOf(Math.min(nextInterval, maxD));
  const threshY = yOf(0.8);

  let svg = '';

  /* ── Defs: gradients, filters, clip ── */
  svg += `<defs>`;
  /* Main curve gradient fill */
  svg += `<linearGradient id="${uid}-fill" x1="0" y1="0" x2="0" y2="1">`;
  svg += `<stop offset="0%" stop-color="var(--teal)" stop-opacity=".18"/>`;
  svg += `<stop offset="60%" stop-color="var(--teal)" stop-opacity=".06"/>`;
  svg += `<stop offset="100%" stop-color="var(--teal)" stop-opacity="0"/>`;
  svg += `</linearGradient>`;
  /* Danger zone below 80% - subtle red tint */
  svg += `<linearGradient id="${uid}-danger" x1="0" y1="0" x2="0" y2="1">`;
  svg += `<stop offset="0%" stop-color="var(--red)" stop-opacity=".06"/>`;
  svg += `<stop offset="100%" stop-color="var(--red)" stop-opacity=".02"/>`;
  svg += `</linearGradient>`;
  /* Glow for current dot */
  svg += `<filter id="${uid}-glow"><feGaussianBlur stdDeviation="3" result="blur"/>`;
  svg += `<feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>`;
  /* Curve line gradient (green->amber->red) */
  svg += `<linearGradient id="${uid}-line" x1="0" y1="0" x2="1" y2="0">`;
  svg += `<stop offset="0%" stop-color="var(--green)"/>`;
  svg += `<stop offset="40%" stop-color="var(--teal)"/>`;
  svg += `<stop offset="75%" stop-color="#EF9F27"/>`;
  svg += `<stop offset="100%" stop-color="var(--red)"/>`;
  svg += `</linearGradient>`;
  svg += `</defs>`;

  /* ── Danger zone (below 80% threshold) ── */
  svg += `<rect x="${pl}" y="${threshY.toFixed(1)}" width="${pw}" height="${(H-pb-threshY).toFixed(1)}" fill="url(#${uid}-danger)" rx="2"/>`;

  /* ── Grid lines ── */
  [{v:1,l:'100%',dash:false},{v:.8,l:'80%',dash:true},{v:.5,l:'50%',dash:false},{v:.2,l:'20%',dash:false}].forEach(({v,l,dash}) => {
    const y = yOf(v);
    const op = v === 0.8 ? '0.6' : '0.3';
    svg += `<line x1="${pl}" y1="${y.toFixed(1)}" x2="${W-pr}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-width="${v===.8?'1':'0.5'}" ${dash?'stroke-dasharray="6,4"':''} opacity="${op}"/>`;
    svg += `<text x="${pl-6}" y="${(y+3).toFixed(1)}" font-size="9" fill="var(--hint)" text-anchor="end" font-weight="${v===.8?'600':'400'}" opacity="${v===.8?'1':'.7'}">${l}</text>`;
  });

  /* ── 80% threshold label ── */
  svg += `<text x="${W-pr-2}" y="${(threshY-4).toFixed(1)}" font-size="8" fill="var(--red)" text-anchor="end" opacity=".5">review threshold</text>`;

  /* ── X-axis labels ── */
  svg += `<text x="${pl}" y="${H-5}" font-size="9" fill="var(--hint)" text-anchor="start" font-weight="500">0d</text>`;
  svg += `<text x="${(W-pr)}" y="${H-5}" font-size="9" fill="var(--hint)" text-anchor="end" font-weight="500">${Math.round(maxD)}d</text>`;
  /* Middle tick */
  const midD = Math.round(maxD / 2);
  const midX = xOf(midD);
  svg += `<line x1="${midX.toFixed(1)}" y1="${(H-pb).toFixed(1)}" x2="${midX.toFixed(1)}" y2="${(H-pb+3).toFixed(1)}" stroke="var(--border)" stroke-width="0.5"/>`;
  svg += `<text x="${midX.toFixed(1)}" y="${H-5}" font-size="8" fill="var(--hint)" text-anchor="middle">${midD}d</text>`;

  /* ── Next-review marker ── */
  svg += `<line x1="${nextX.toFixed(1)}" y1="${pt}" x2="${nextX.toFixed(1)}" y2="${(H-pb).toFixed(1)}" stroke="#EF9F27" stroke-width="1.5" stroke-dasharray="4,3" opacity=".5"/>`;
  const nrAnchor = nextX < pl + 40 ? 'start' : nextX > W - pr - 40 ? 'end' : 'middle';
  svg += `<rect x="${(nextX - (nrAnchor==='middle'?28:nrAnchor==='end'?52:0)).toFixed(1)}" y="${(pt-18).toFixed(1)}" width="56" height="16" rx="8" fill="#EF9F27" opacity=".12"/>`;
  svg += `<text x="${nextX.toFixed(1)}" y="${(pt-7).toFixed(1)}" font-size="9" fill="#EF9F27" text-anchor="${nrAnchor}" font-weight="700">${nextInterval}d next</text>`;

  /* ── Curve fill ── */
  svg += `<path d="${fillPath}" fill="url(#${uid}-fill)"/>`;

  /* ── Curve line (gradient colored) ── */
  svg += `<path d="${pathD}" fill="none" stroke="url(#${uid}-line)" stroke-width="2.5" stroke-linecap="round"/>`;

  /* ── Current elapsed marker line ── */
  if (daysElapsed > 0) {
    svg += `<line x1="${curX.toFixed(1)}" y1="${(curY+6).toFixed(1)}" x2="${curX.toFixed(1)}" y2="${(H-pb).toFixed(1)}" stroke="var(--teal)" stroke-width="0.5" stroke-dasharray="2,2" opacity=".4"/>`;
    svg += `<text x="${curX.toFixed(1)}" y="${H-5}" font-size="8" fill="var(--teal)" text-anchor="middle" font-weight="600">${daysElapsed}d</text>`;
  }

  /* ── Current-position dot with glow ── */
  const dotColor = curRet >= 0.8 ? 'var(--green)' : curRet >= 0.5 ? '#EF9F27' : 'var(--red)';
  svg += `<circle cx="${curX.toFixed(1)}" cy="${curY.toFixed(1)}" r="6" fill="${dotColor}" opacity=".2" filter="url(#${uid}-glow)"/>`;
  svg += `<circle cx="${curX.toFixed(1)}" cy="${curY.toFixed(1)}" r="5" fill="var(--surface)" stroke="${dotColor}" stroke-width="2.5"/>`;
  svg += `<circle cx="${curX.toFixed(1)}" cy="${curY.toFixed(1)}" r="2" fill="${dotColor}"/>`;

  /* ── Current retention label near dot ── */
  const retPct = Math.round(curRet * 100);
  const labelX = curX + (curX > W - 60 ? -8 : 8);
  const labelAnchor = curX > W - 60 ? 'end' : 'start';
  svg += `<text x="${labelX.toFixed(1)}" y="${(curY - 8).toFixed(1)}" font-size="10" fill="${dotColor}" text-anchor="${labelAnchor}" font-weight="700">${retPct}%</text>`;

  return `<svg width="100%" viewBox="0 0 ${W} ${H}" style="display:block;margin-top:4px">${svg}</svg>`;
}

function fmtDate(s) {
  return s ? new Date(s+'T12:00:00').toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'}) : '—';
}

function render() {
  /* Build tag filter bar */
  const allTags = new Set();
  all.forEach(t => (t.tags || []).forEach(tg => allTags.add(tg)));
  const tagBar = document.getElementById('tag-bar');
  if (allTags.size > 0) {
    tagBar.style.display = 'flex';
    let tagBtns = `<span style="font-size:11px;color:var(--hint);text-transform:uppercase;letter-spacing:.04em">Tags:</span>`;
    tagBtns += `<button class="filter-btn ${tagFilter===''?'active':''}" onclick="setTagFilter('')" style="font-size:11px;padding:3px 10px">\ud83c\udff7 All</button>`;
    [...allTags].sort().forEach(tg => {
      tagBtns += `<button class="filter-btn ${tagFilter===tg?'active':''}" onclick="setTagFilter('${tg.replace(/'/g,"\\'")}')" style="font-size:11px;padding:3px 10px">${escHtml(tg)}</button>`;
    });
    tagBar.innerHTML = tagBtns;
  } else {
    tagBar.style.display = 'none';
  }

  let topics = [...all];
  if (tagFilter) topics = topics.filter(t => (t.tags || []).includes(tagFilter));
  if (filter === 'overdue')  topics = topics.filter(t => t.status === 'overdue');
  else if (filter === 'due') topics = topics.filter(t => ['overdue','due'].includes(t.status));
  else if (filter === 'upcoming') topics = topics.filter(t => ['soon','upcoming'].includes(t.status));

  let ov=0, du=0, up=0;
  all.forEach(t => { if(t.status==='overdue')ov++; else if(t.status==='due')du++; else if(['soon','upcoming'].includes(t.status))up++; });
  document.getElementById('c-ov').textContent = ov;
  document.getElementById('c-du').textContent = du;
  document.getElementById('c-up').textContent = up;

  if (!topics.length) {
    const emptyMsg = all.length ? 'No topics match this filter.' : 'Add your first topic to start tracking.';
    const emptyIcon = all.length ? '' : `<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--hint)" stroke-width="1.5" stroke-linecap="round"><path d="M12 6v6l4 2"/><circle cx="12" cy="12" r="10"/></svg>`;
    document.getElementById('list').innerHTML = `<div class="empty">${emptyIcon}<div>${emptyMsg}</div></div>`;
    return;
  }

  document.getElementById('list').innerHTML = topics.map(t => {
    const isDue = ['overdue','due'].includes(t.status) || t.retention <= 80;
    const bc = {overdue:'b-overdue',due:'b-due',soon:'b-soon',upcoming:'b-upcoming'}[t.status]||'b-upcoming';
    const rc = retColor(t.retention);
    const hasCards = t.card_count > 0;
    let actionBtns;
    if (t.reviewed_today) {
      actionBtns = `<button class="btn-done">\u2714 Reviewed today</button>${hasCards ? `<button class="btn-review-early" onclick="startStudySession(${t.id}, true)">\u{1F501} Redo Quiz</button>` : ''}`;
    } else if (hasCards && isDue) {
      actionBtns = `<button class="btn-review" onclick="startStudySession(${t.id})">\u270F Start Review</button>`;
    } else if (hasCards) {
      actionBtns = `<button class="btn-review-early" onclick="startStudySession(${t.id})">\u270F Review Early</button>`;
    } else if (isDue) {
      actionBtns = `<button class="btn-review" onclick="markReviewed(${t.id})">\u2714 Quick Review</button>`;
    } else {
      actionBtns = `<button class="btn-review-early" onclick="markReviewed(${t.id})">\u2714 Review Early</button>`;
    }
    const curveId = 'curve-'+t.id;
    const isClosed = closedCurves.has(t.id);
    return `<div class="card ${isDue?t.status:''}">
      <div class="card-top">
        <div class="topic-name editable-field" onclick="editField(${t.id},'name','${escAttr(t.name)}','text')" title="Click to edit">${t.name}</div>
        ${(t.tags && t.tags.length) ? `<div class="topic-tags">${t.tags.map(tg=>'#'+tg).join(' ')}</div>` : ''}
        <span class="badge ${bc}">${t.status_text}</span>
      </div>
      <div class="meta-row">
        <span class="meta">Learned <strong class="editable-date" onclick="editDate(${t.id},'learned_date','${t.learned_date}')" title="Click to edit">${fmtDate(t.learned_date)}</strong></span>
        <span class="meta">Last review <strong>${t.last_review ? `<span class="editable-date" onclick="editDate(${t.id},'last_review','${t.last_review}')" title="Click to edit">${fmtDate(t.last_review)}</span>` : `<span class="editable-date" onclick="editDate(${t.id},'last_review','${new Date().toISOString().slice(0,10)}')" title="Click to set">never</span>`}</strong></span>
        <span class="meta">Next <strong class="editable-date" onclick="editDate(${t.id},'next_review','${t.next_review}')" title="Click to edit">${fmtDate(t.next_review)}</strong></span>
        <span class="meta">Reviews <strong class="editable-field" onclick="editField(${t.id},'review_count','${t.review_count}','number')" title="Click to edit">${t.review_count}</strong></span>
        <span class="meta">Next interval <strong>${t.next_interval}d</strong></span>
      </div>
      <div class="ret-row">
        <span class="ret-label">Retention</span>
        <div class="ret-bg"><div class="ret-fill" style="width:${Math.max(2,t.retention)}%;background:linear-gradient(90deg,${rc},${rc}dd);box-shadow:0 0 8px ${rc}33"></div></div>
        <span class="ret-pct" style="color:${rc}">${t.retention}%</span>
      </div>
      <div class="curve-toggle ${isClosed?'closed':''}" onclick="toggleCurve(${t.id})">
        <svg width="12" height="12" viewBox="0 0 10 10"><path d="M2 3.5L5 7L8 3.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        <span>Forgetting curve</span>
      </div>
      <div class="curve-wrap ${isClosed?'closed':''}" id="${curveId}">${curveSVG(t.a, t.k, t.days_elapsed, t.next_interval)}</div>
      <div class="hist-wrap" id="hist-${t.id}"></div>
      <div class="actions">
        ${actionBtns}
        <a class="btn-cards" href="/cards/${t.id}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="16" height="16" rx="2"/><path d="M6 0h12a2 2 0 0 1 2 2v16"/></svg> Cards <span class="cards-badge">${t.card_count}</span></a>
        <button class="btn-cards" onclick="goToProblems(${t.id})">\ud83e\udde9 Problems <span class="cards-badge">${t.problem_count || 0}</span></button>
        ${t.review_count > 0 ? `<button class="btn-hist" onclick="toggleHistory(${t.id})">\ud83d\udcca History</button>` : ''}
        <button class="btn-del" onclick="del(${t.id})">Delete</button>
        <span class="eq">a=${t.a} \u00b7 k=${t.k}</span>
      </div>
    </div>`;
  }).join('');
}

/* ── Schedule graph ──────────────────────────────── */
let schedOpen = false;
function toggleSchedule() {
  schedOpen = !schedOpen;
  document.getElementById('sched-body').classList.toggle('open', schedOpen);
  document.getElementById('sched-arrow').classList.toggle('open', schedOpen);
  if (schedOpen) loadSchedule();
}

async function loadSchedule() {
  try {
    const res = await fetch('/api/schedule');
    const data = await res.json();
    renderSchedule(data);
  } catch(e) {
    document.getElementById('sched-graph').innerHTML = '<div class="sched-empty">Failed to load schedule.</div>';
  }
}

function renderSchedule(data) {
  const el = document.getElementById('sched-graph');
  if (!data.length) { el.innerHTML = '<div class="sched-empty">Add topics to see the review schedule.</div>'; return; }

  const todayStr = today();
  const todayMs = new Date(todayStr + 'T00:00:00').getTime();
  const dayMs = 86400000;

  /* find date range */
  let allDates = [];
  data.forEach(t => t.reviews.forEach(d => allDates.push(d)));
  allDates.sort();
  const minDate = todayStr < allDates[0] ? todayStr : allDates[0];
  const maxDate = allDates[allDates.length - 1];
  const startMs = new Date(minDate + 'T00:00:00').getTime();
  const endMs   = new Date(maxDate + 'T00:00:00').getTime();
  const spanDays = Math.max(1, Math.round((endMs - startMs) / dayMs));

  const rowH = 28, padTop = 30, padBot = 24, padL = 120, padR = 20;
  const W = Math.max(600, padL + spanDays * 4 + padR);
  const H = padTop + data.length * rowH + padBot;
  const graphW = W - padL - padR;
  const xOf = d => padL + ((new Date(d+'T00:00:00').getTime() - startMs) / dayMs) / spanDays * graphW;

  /* color palette */
  const colors = ['#B85C38','#3B82F6','#EF9F27','#E24B4A','#8B5CF6','#EC4899','#14B8A6','#F97316'];

  let svg = '';

  /* month markers */
  const seenMonths = new Set();
  for (let d = new Date(minDate+'T00:00:00'); d.getTime() <= endMs + dayMs * 30; d.setDate(d.getDate() + 1)) {
    const key = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0');
    if (!seenMonths.has(key) && d.getDate() <= 7) {
      seenMonths.add(key);
      const iso = d.toISOString().split('T')[0];
      const x = xOf(iso);
      if (x >= padL && x <= W - padR) {
        svg += `<line x1="${x}" y1="${padTop - 4}" x2="${x}" y2="${H - padBot}" stroke="var(--border)" stroke-width="0.5"/>`;
        const lbl = d.toLocaleDateString(undefined,{month:'short',year:'2-digit'});
        svg += `<text x="${x+3}" y="${padTop - 8}" font-size="9" fill="var(--hint)">${lbl}</text>`;
      }
    }
  }

  /* today line */
  const todayX = xOf(todayStr);
  if (todayX >= padL && todayX <= W - padR) {
    svg += `<line x1="${todayX}" y1="${padTop - 4}" x2="${todayX}" y2="${H - padBot}" stroke="var(--red)" stroke-width="1" stroke-dasharray="3,2" opacity=".6"/>`;
    svg += `<text x="${todayX}" y="${H - padBot + 14}" font-size="9" fill="var(--red)" text-anchor="middle">today</text>`;
  }

  /* rows */
  data.forEach((t, i) => {
    const y = padTop + i * rowH + rowH / 2;
    const c = colors[i % colors.length];

    /* topic label */
    const label = t.name.length > 16 ? t.name.slice(0,15) + '\u2026' : t.name;
    svg += `<text x="${padL - 8}" y="${y + 4}" font-size="11" fill="var(--text)" text-anchor="end" font-weight="500">${label}</text>`;

    /* row stripe */
    if (i % 2 === 0) svg += `<rect x="${padL}" y="${y - rowH/2}" width="${graphW}" height="${rowH}" fill="var(--bg)" opacity=".4"/>`;

    /* connecting line */
    if (t.reviews.length > 1) {
      const x1 = xOf(t.reviews[0]), x2 = xOf(t.reviews[t.reviews.length-1]);
      svg += `<line x1="${x1}" y1="${y}" x2="${x2}" y2="${y}" stroke="${c}" stroke-width="1.5" opacity=".3"/>`;
    }

    /* review dots */
    t.reviews.forEach((d, j) => {
      const x = xOf(d);
      const isPast = d < todayStr;
      const isToday = d === todayStr;
      const r = isToday ? 5 : 4;
      const fill = isPast ? 'var(--hint)' : c;
      const stroke = isToday ? 'var(--red)' : 'none';
      const sw = isToday ? 1.5 : 0;
      svg += `<circle cx="${x}" cy="${y}" r="${r}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}">`;
      const fmtD = new Date(d+'T12:00:00').toLocaleDateString(undefined,{month:'short',day:'numeric'});
      svg += `<title>${t.name} \u2014 Review ${j+1}: ${fmtD}${isToday?' (today)':isPast?' (past)':''}</title></circle>`;
    });
  });

  el.innerHTML = `<svg width="100%" viewBox="0 0 ${W} ${H}" style="display:block;min-width:${W}px">${svg}</svg>`;
}

/* ── Streak Stats ────────────────────────────────── */
async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();
    if (data.total_review_days > 0) {
      document.getElementById('streak-bar').style.display = '';
      document.getElementById('s-cur').textContent = data.current_streak;
      document.getElementById('s-best').textContent = data.longest_streak;
      document.getElementById('s-total').textContent = data.total_review_days;
      document.getElementById('s-cards').textContent = data.total_cards || 0;
    } else {
      document.getElementById('streak-bar').style.display = 'none';
    }
  } catch(e) { /* silent */ }
}

/* ── Card Generator ──────────────────────────────── */
/* ── Card Generator Modal Flow ───────────────────── */
let pdfSelectedFile = null;
let pdfRecommended = 15;

function switchGenTab(tab) {
  document.querySelectorAll('.gen-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.gen-tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('gen-tab-' + tab).classList.add('active');
  document.getElementById('gen-panel-' + tab).classList.add('active');
}

function openPdfModal() {
  /* Reset prompt tab */
  document.getElementById('gen-prompt-text').value = '';
  document.getElementById('gen-prompt-topic').value = '';
  document.getElementById('prompt-slider').value = 15;
  document.getElementById('prompt-card-count').textContent = '15';
  document.getElementById('prompt-generate-btn').disabled = false;
  document.getElementById('prompt-generate-btn').textContent = 'Generate Cards';
  /* Reset PDF tab */
  pdfSelectedFile = null;
  document.getElementById('pdf-file-info').style.display = 'none';
  document.getElementById('pdf-slider-section').style.display = 'none';
  document.getElementById('pdf-prompt-text').style.display = 'none';
  document.getElementById('pdf-prompt-text').value = '';
  document.getElementById('pdf-page-range').style.display = 'none';
  document.getElementById('pdf-preview').style.display = 'none';
  document.getElementById('pdf-generate-btn').disabled = true;
  document.getElementById('pdf-dropzone').style.display = '';
  /* Default to prompt tab */
  switchGenTab('prompt');
  document.getElementById('pdf-modal').classList.add('open');
}

function closePdfModal() {
  document.getElementById('pdf-modal').classList.remove('open');
  pdfSelectedFile = null;
  const inp = document.getElementById('pdf-file-input');
  if (inp) inp.value = '';
}

function updatePdfSlider(val) {
  document.getElementById('pdf-card-count').textContent = val;
  const badge = document.getElementById('pdf-rec-badge');
  const diff = Math.abs(parseInt(val) - pdfRecommended);
  badge.style.display = diff <= 2 ? '' : 'none';
}

(function() {
  /* Dropzone interaction */
  const dz = document.getElementById('pdf-dropzone');
  const inp = document.getElementById('pdf-file-input');
  if (!dz || !inp) return;
  dz.addEventListener('click', function(e) { if (e.target !== inp) inp.click(); });
  dz.addEventListener('dragover', function(e) { e.preventDefault(); dz.style.borderColor = 'var(--teal)'; dz.style.background = 'var(--teal-light)'; });
  dz.addEventListener('dragleave', function() { dz.style.borderColor = 'var(--border)'; dz.style.background = ''; });
  dz.addEventListener('drop', function(e) {
    e.preventDefault(); dz.style.borderColor = 'var(--border)'; dz.style.background = '';
    if (e.dataTransfer.files.length) handlePdfFile(e.dataTransfer.files[0]);
  });
  inp.addEventListener('change', function() { if (inp.files[0]) handlePdfFile(inp.files[0]); });
})();

async function handlePdfFile(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    toast('Please select a PDF file', 'warn'); return;
  }
  pdfSelectedFile = file;
  document.getElementById('pdf-file-name').textContent = file.name;
  document.getElementById('pdf-file-meta').textContent = (file.size / 1024).toFixed(0) + ' KB';
  document.getElementById('pdf-file-info').style.display = 'flex';
  document.getElementById('pdf-dropzone').style.display = 'none';
  document.getElementById('pdf-generate-btn').disabled = true;
  document.getElementById('pdf-generate-btn').textContent = 'Analyzing...';

  /* Call estimate endpoint */
  const formData = new FormData();
  formData.append('pdf', file);
  try {
    const res = await fetch('/api/estimate-pdf', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      toast(data.error || 'Failed to analyze PDF', 'warn');
      /* Reset to dropzone so user can retry */
      document.getElementById('pdf-file-info').style.display = 'none';
      document.getElementById('pdf-dropzone').style.display = '';
      document.getElementById('pdf-generate-btn').disabled = true;
      document.getElementById('pdf-generate-btn').textContent = 'Generate Cards';
      pdfSelectedFile = null;
      return;
    }
    document.getElementById('pdf-file-meta').textContent =
      data.page_count + ' page' + (data.page_count !== 1 ? 's' : '') +
      ' \u00B7 ' + (data.char_count / 1000).toFixed(1) + 'k characters';
    /* Show page range controls */
    const totalPages = data.page_count;
    document.getElementById('pdf-total-pages').textContent = totalPages + ' pages total';
    var defEnd = Math.min(totalPages, 30);
    initDualRange('pdf', totalPages, 1, defEnd, function(s, e) {
      updatePdfPreviewHighlight('pdf', s, e);
    });
    document.getElementById('pdf-page-range').style.display = '';
    renderPdfPreview('pdf', file);
    pdfRecommended = data.recommended_cards;
    const slider = document.getElementById('pdf-slider');
    slider.value = pdfRecommended;
    updatePdfSlider(pdfRecommended);
    document.getElementById('pdf-rec-badge').style.display = '';
    document.getElementById('pdf-slider-section').style.display = '';
    document.getElementById('pdf-prompt-text').style.display = '';
    document.getElementById('pdf-generate-btn').disabled = false;
    document.getElementById('pdf-generate-btn').textContent = 'Generate Cards';
  } catch(e) {
    toast('Failed to analyze PDF: ' + e.message, 'warn');
    document.getElementById('pdf-file-info').style.display = 'none';
    document.getElementById('pdf-dropzone').style.display = '';
    document.getElementById('pdf-generate-btn').disabled = true;
    pdfSelectedFile = null;
  }
}

async function generateFromPdf() {
  if (!pdfSelectedFile) return;
  const numCards = parseInt(document.getElementById('pdf-slider').value) || 15;
  const btn = document.getElementById('pdf-generate-btn');
  btn.disabled = true;
  btn.innerHTML = '<span style="display:inline-block;animation:spin 1s linear infinite">&#x2699;</span> Generating ' + numCards + ' cards...';

  const formData = new FormData();
  formData.append('pdf', pdfSelectedFile);
  formData.append('num_cards', numCards);
  const pageStart = parseInt(document.getElementById('pdf-page-start').value) || 1;
  const pageEnd = parseInt(document.getElementById('pdf-page-end').value) || 9999;
  formData.append('page_start', pageStart);
  formData.append('page_end', pageEnd);
  const pdfPrompt = document.getElementById('pdf-prompt-text').value.trim();
  if (pdfPrompt) formData.append('prompt', pdfPrompt);

  try {
    const res = await fetch('/api/import-pdf', { method:'POST', body:formData });
    const result = await res.json();
    closePdfModal();
    if (res.ok && result.ok) {
      toast('Created "' + result.topic_name + '" with ' + result.card_count + ' cards!', 'success');
      await load();
      loadStats();
      if (confirm('Topic "' + result.topic_name + '" created with ' + result.card_count + ' AI-generated cards!\n\nOpen the card editor to review them?')) {
        window.location.href = '/cards/' + result.topic_id;
      }
    } else {
      toast(result.error || 'PDF import failed', 'warn');
    }
  } catch(e) {
    closePdfModal();
    toast('PDF import failed: ' + e.message, 'warn');
  }
}

async function generateFromPrompt() {
  const promptText = document.getElementById('gen-prompt-text').value.trim();
  if (!promptText) { toast('Please enter a prompt describing what to study', 'warn'); return; }
  const topicName = document.getElementById('gen-prompt-topic').value.trim();
  const numCards = parseInt(document.getElementById('prompt-slider').value) || 15;
  const btn = document.getElementById('prompt-generate-btn');
  btn.disabled = true;
  btn.innerHTML = '<span style="display:inline-block;animation:spin 1s linear infinite">&#x2699;</span> Generating ' + numCards + ' cards...';

  try {
    const res = await fetch('/api/generate-cards-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ prompt: promptText, topic_name: topicName, num_cards: numCards })
    });
    const result = await res.json();
    closePdfModal();
    if (res.ok && result.ok) {
      toast('Created "' + result.topic_name + '" with ' + result.card_count + ' cards!', 'success');
      await load();
      loadStats();
      if (confirm('Topic "' + result.topic_name + '" created with ' + result.card_count + ' AI-generated cards!\n\nOpen the card editor to review them?')) {
        window.location.href = '/cards/' + result.topic_id;
      }
    } else {
      toast(result.error || 'Card generation failed', 'warn');
    }
  } catch(e) {
    closePdfModal();
    toast('Card generation failed: ' + e.message, 'warn');
  }
}

/* ── Export / Import ──────────────────────────────── */
async function exportData() {
  try {
    const res = await fetch('/api/export');
    const data = await res.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'study-tracker-backup-' + today() + '.json';
    a.click();
    URL.revokeObjectURL(url);
    toast('Data exported', 'success');
  } catch(e) { toast('Export failed', 'warn'); }
}

async function importData(event) {
  const file = event.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    const res = await fetch('/api/import', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(data)
    });
    const result = await res.json();
    if (res.ok) {
      toast('Imported ' + result.imported_topics + ' topic(s) and ' + result.imported_cards + ' card(s)', 'success');
      await load();
      loadStats();
    } else {
      toast(result.error || 'Import failed', 'warn');
    }
  } catch(e) { toast('Invalid file format', 'warn'); }
  event.target.value = '';
}

/* ═══ Practice Problems Tab ═══ */

let ppProblems = [], ppCurrentTopic = null, ppEditingId = null;

function switchMainTab(tab) {
  document.querySelectorAll('.pp-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.pp-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.getElementById('panel-' + tab).classList.add('active');
  if (tab === 'problems') ppPopulateTopics();
}

function goToProblems(tid) {
  switchMainTab('problems');
  const sel = document.getElementById('pp-topic-select');
  sel.value = tid;
  ppLoadProblems();
}

function ppPopulateTopics() {
  const sel = document.getElementById('pp-topic-select');
  const cur = sel.value;
  let opts = '<option value="">\u2014 Select topic \u2014</option>';
  all.forEach(t => { opts += '<option value="' + t.id + '">' + escHtml(t.name) + '</option>'; });
  sel.innerHTML = opts;
  if (cur) sel.value = cur;
}

async function ppLoadProblems() {
  const tid = parseInt(document.getElementById('pp-topic-select').value);
  ppCurrentTopic = tid || null;
  document.getElementById('pp-add-btn').style.display = tid ? '' : 'none';
  document.getElementById('pp-pdf-btn').style.display = tid ? '' : 'none';
  document.getElementById('pp-form-wrap').style.display = 'none';
  ppEditingId = null;
  if (!tid) {
    ppProblems = [];
    document.getElementById('pp-list').innerHTML = '<div class="empty">Select a topic to view practice problems.</div>';
    document.getElementById('pp-count-badge').style.display = 'none';
    return;
  }
  try {
    const res = await fetch('/api/topics/' + tid + '/problems');
    ppProblems = await res.json();
  } catch(e) {
    ppProblems = [];
    toast('Failed to load problems', 'warn');
  }
  ppRender();
}

function ppRender() {
  const el = document.getElementById('pp-list');
  const badge = document.getElementById('pp-count-badge');
  badge.textContent = ppProblems.length + ' problem' + (ppProblems.length !== 1 ? 's' : '');
  badge.style.display = ppProblems.length ? '' : 'none';
  if (!ppProblems.length) {
    el.innerHTML = '<div class="empty">No practice problems yet. Add one or import from PDF!</div>';
    return;
  }
  const diffLabels = {1:'Easy', 2:'Medium', 3:'Hard'};
  el.innerHTML = ppProblems.map(p => {
    const rBadge = p.last_rating ? '<span class="pp-rating-badge ' + p.last_rating + '">' + p.last_rating.charAt(0).toUpperCase() + p.last_rating.slice(1) + '</span>' : '';
    return '<div class="pp-card">' +
      '<div class="pp-card-top">' +
        '<div class="pp-card-title rendered-math">' + renderMathText(p.title) + '</div>' +
        rBadge +
      '</div>' +
      '<div class="pp-card-meta">' +
        '<span class="pp-diff pp-diff-' + p.difficulty + '">' + (diffLabels[p.difficulty] || 'Medium') + '</span>' +
        (p.skill_tag ? '<span class="pp-skill">' + escHtml(p.skill_tag) + '</span>' : '') +
        (p.source === 'extracted' ? '<span class="pp-skill" style="background:var(--teal-light);color:var(--teal)">\uD83D\uDCD6 Textbook</span>' : '') +
        '<span>\u270F ' + p.attempt_count + ' attempt' + (p.attempt_count !== 1 ? 's' : '') + '</span>' +
      '</div>' +
      '<div class="pp-card-actions">' +
        '<button class="btn-start" onclick="ppStartProblem(' + p.id + ')">\u25B6 Solve</button>' +
        '<button onclick="ppEditProblem(' + p.id + ')">\u270F Edit</button>' +
        '<button class="btn-regen" onclick="ppRegenerateProblem(' + p.id + ')" title="Regenerate problem">\u2699</button>' +
        '<button class="btn-del" onclick="ppDeleteProblem(' + p.id + ')">Delete</button>' +
      '</div>' +
    '</div>';
  }).join('');
}

function ppToggleForm(editData) {
  const wrap = document.getElementById('pp-form-wrap');
  if (wrap.style.display !== 'none' && !editData) {
    wrap.style.display = 'none';
    ppEditingId = null;
    return;
  }
  ppEditingId = editData ? editData.id : null;
  wrap.style.display = '';
  wrap.innerHTML = '<div class="pp-form">' +
    '<div class="form-row">' +
      '<div class="fg" style="flex:2"><label>Title</label><input type="text" id="pp-inp-title" placeholder="e.g. Solve the integral" value="' + escAttr(editData ? editData.title : '') + '"></div>' +
      '<div class="fg" style="flex:0 0 120px"><label>Difficulty</label><select id="pp-inp-diff"><option value="1"' + (editData && editData.difficulty===1?' selected':'') + '>Easy</option><option value="2"' + (!editData || editData.difficulty===2?' selected':'') + '>Medium</option><option value="3"' + (editData && editData.difficulty===3?' selected':'') + '>Hard</option></select></div>' +
      '<div class="fg" style="flex:0 0 140px"><label>Skill tag</label><input type="text" id="pp-inp-skill" placeholder="e.g. integration" value="' + escAttr(editData ? editData.skill_tag : '') + '"></div>' +
    '</div>' +
    '<div class="fg"><label>Problem prompt</label><textarea id="pp-inp-prompt" rows="4" placeholder="Write the full problem statement...">' + escHtml(editData ? editData.prompt : '') + '</textarea>' +
      '<div id="pp-prev-prompt" class="math-preview rendered-math"></div>' +
      '<div class="math-helper" style="margin-top:6px"><math-field id="mf-pp-prompt" virtual-keyboard-mode="onfocus" style="flex:1;min-height:38px;font-size:14px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:8px"></math-field><button type="button" class="mh-insert" onclick="insertMath(\'mf-pp-prompt\',\'pp-inp-prompt\')">&#x2795; Insert</button></div>' +
    '</div>' +
    '<div class="form-row" style="margin-top:10px">' +
      '<div class="fg"><label>Final answer</label><input type="text" id="pp-inp-answer" placeholder="Concise final answer" value="' + escAttr(editData ? editData.final_answer : '') + '"></div>' +
    '</div>' +
    '<div class="fg" style="margin-top:10px"><label>Full solution</label><textarea id="pp-inp-solution" rows="4" placeholder="Step-by-step worked solution...">' + escHtml(editData ? editData.full_solution : '') + '</textarea>' +
      '<div id="pp-prev-solution" class="math-preview rendered-math"></div>' +
      '<div class="math-helper" style="margin-top:6px"><math-field id="mf-pp-solution" virtual-keyboard-mode="onfocus" style="flex:1;min-height:38px;font-size:14px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:8px"></math-field><button type="button" class="mh-insert" onclick="insertMath(\'mf-pp-solution\',\'pp-inp-solution\')">&#x2795; Insert</button></div>' +
    '</div>' +
    '<div class="fg" style="margin-top:10px"><label>Hints (one per line)</label><textarea id="pp-inp-hints" rows="3" placeholder="Hint 1\nHint 2\nHint 3">' + escHtml(editData && editData.hints ? editData.hints.join('\n') : '') + '</textarea>' +
      '<div id="pp-prev-hints" class="math-preview rendered-math"></div>' +
      '<div class="math-helper" style="margin-top:6px"><math-field id="mf-pp-hints" virtual-keyboard-mode="onfocus" style="flex:1;min-height:38px;font-size:14px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:8px"></math-field><button type="button" class="mh-insert" onclick="insertMath(\'mf-pp-hints\',\'pp-inp-hints\')">&#x2795; Insert</button></div>' +
    '</div>' +
    '<div style="display:flex;gap:8px;margin-top:14px">' +
      '<button class="btn-primary" onclick="ppSaveProblem()">' + (ppEditingId ? 'Update' : 'Add') + ' Problem</button>' +
      '<button onclick="ppToggleForm()">Cancel</button>' +
    '</div>' +
  '</div>';
  /* Wire up live preview for form textareas */
  ['prompt','solution','hints'].forEach(function(f) {
    const ta = document.getElementById('pp-inp-' + f);
    if (ta) {
      ta.addEventListener('input', function() { livePreview('pp-inp-' + f, 'pp-prev-' + f); });
      livePreview('pp-inp-' + f, 'pp-prev-' + f);
    }
  });
}

function escAttr(s) { return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

async function ppSaveProblem() {
  if (!ppCurrentTopic) return;
  const title = document.getElementById('pp-inp-title').value.trim();
  const prompt = document.getElementById('pp-inp-prompt').value.trim();
  if (!title || !prompt) { toast('Title and prompt required', 'warn'); return; }
  const body = {
    title: title,
    prompt: prompt,
    final_answer: document.getElementById('pp-inp-answer').value.trim(),
    full_solution: document.getElementById('pp-inp-solution').value.trim(),
    skill_tag: document.getElementById('pp-inp-skill').value.trim(),
    difficulty: parseInt(document.getElementById('pp-inp-diff').value) || 2,
    hints: document.getElementById('pp-inp-hints').value.split('\n').map(h => h.trim()).filter(Boolean),
  };
  try {
    const url = ppEditingId ? '/api/problems/' + ppEditingId : '/api/topics/' + ppCurrentTopic + '/problems';
    const method = ppEditingId ? 'PUT' : 'POST';
    const res = await fetch(url, { method: method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const data = await res.json();
    if (res.ok) {
      toast(ppEditingId ? 'Problem updated' : 'Problem added', 'success');
      document.getElementById('pp-form-wrap').style.display = 'none';
      ppEditingId = null;
      await ppLoadProblems();
    } else {
      toast(data.error || 'Failed to save', 'warn');
    }
  } catch(e) { toast('Failed to save problem', 'warn'); }
}

function ppEditProblem(pid) {
  const p = ppProblems.find(x => x.id === pid);
  if (!p) return;
  ppToggleForm(p);
}

async function ppDeleteProblem(pid) {
  if (!confirm('Delete this problem and all its attempts?')) return;
  try {
    await fetch('/api/problems/' + pid, { method: 'DELETE' });
    toast('Problem deleted', 'success');
    await ppLoadProblems();
  } catch(e) { toast('Failed to delete', 'warn'); }
}

async function ppRegenerateProblem(pid) {
  if (!confirm('Regenerate this problem? The current prompt and any attempts will be replaced.')) return;
  const btn = document.querySelector('.btn-regen[onclick*="' + pid + '"]');
  if (btn) btn.classList.add('spinning');
  try {
    const res = await fetch('/api/problems/' + pid + '/regenerate', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
    });
    const data = await res.json();
    if (res.ok) {
      toast('Problem regenerated', 'success');
      await ppLoadProblems();
    } else {
      toast(data.error || 'Failed to regenerate', 'warn');
    }
  } catch(e) {
    toast('Failed to regenerate problem', 'warn');
  }
  if (btn) btn.classList.remove('spinning');
}

/* ═══ Problem Solving Overlay ═══ */

let ppSolveState = null;

async function ppStartProblem(pid) {
  const p = ppProblems.find(x => x.id === pid);
  if (!p) return;
  let attempts = [];
  try {
    const res = await fetch('/api/problems/' + pid + '/attempts');
    if (res.ok) attempts = await res.json();
  } catch(e) {}
  ppSolveState = {
    problem: p,
    hintsRevealed: 0,
    hintsLoaded: [],
    hintLoading: false,
    solutionRevealed: false,
    solutionLoading: false,
    userAnswer: '',
    aiResult: null,
    rating: '',
    submitted: false,
    attempts: attempts,
    showHistory: false,
  };
  ppRenderSolve();
}

function ppCloseSolve() {
  ppSolveState = null;
  const el = document.getElementById('pp-overlay');
  if (el) el.remove();
}

function ppToggleHistory() {
  if (!ppSolveState) return;
  ppSolveState.showHistory = !ppSolveState.showHistory;
  ppRenderSolve();
}

function ppAttemptHistoryHtml(s) {
  if (!s.attempts || !s.attempts.length) return '';
  const rColors = {complete:'var(--green)', partial:'var(--amber)', failed:'var(--red)'};
  let html = '<div style="margin-top:20px;border-top:1px solid var(--border);padding-top:14px">' +
    '<button onclick="ppToggleHistory()" style="background:none;border:none;cursor:pointer;font-size:13px;font-weight:600;color:var(--muted);display:flex;align-items:center;gap:6px;padding:0">' +
      (s.showHistory ? '\u25BC' : '\u25B6') + ' Past Attempts (' + s.attempts.length + ')' +
    '</button>';
  if (s.showHistory) {
    html += '<div style="margin-top:10px;display:flex;flex-direction:column;gap:8px">';
    s.attempts.slice().reverse().forEach(function(a, i) {
      const d = new Date(a.created_at);
      const dateStr = d.toLocaleDateString(undefined, {month:'short',day:'numeric',year:'numeric'});
      html += '<div style="padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:8px;font-size:13px">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
          '<span style="font-weight:600;color:var(--text)">' + dateStr + '</span>' +
          (a.rating ? '<span style="font-weight:600;color:' + (rColors[a.rating] || 'var(--muted)') + '">' + a.rating.charAt(0).toUpperCase() + a.rating.slice(1) + '</span>' : '') +
        '</div>' +
        (a.user_answer ? '<div style="color:var(--muted);margin-bottom:4px" class="rendered-math">' + renderMathText(a.user_answer.length > 200 ? a.user_answer.substring(0,200) + '...' : a.user_answer) + '</div>' : '') +
        (a.ai_explanation ? '<div style="font-size:12px;color:var(--hint);font-style:italic">\uD83E\uDD16 ' + escHtml(a.ai_explanation.length > 150 ? a.ai_explanation.substring(0,150) + '...' : a.ai_explanation) + '</div>' : '') +
        '<div style="font-size:11px;color:var(--muted);margin-top:3px">' +
          (a.hints_used ? a.hints_used + ' hint' + (a.hints_used !== 1 ? 's' : '') + ' used' : 'No hints') +
          (a.solution_viewed ? ' \u00B7 Solution viewed' : '') +
        '</div>' +
      '</div>';
    });
    html += '</div>';
  }
  html += '</div>';
  return html;
}

function ppRenderSolve() {
  if (!ppSolveState) return;
  let el = document.getElementById('pp-overlay');
  if (!el) {
    el = document.createElement('div');
    el.id = 'pp-overlay';
    el.className = 'pp-overlay';
    document.body.appendChild(el);
  }
  const s = ppSolveState;
  const p = s.problem;
  const diffLabels = {1:'Easy', 2:'Medium', 3:'Hard'};

  let hintsHtml = '<div class="pp-hints-section">';
  for (let i = 0; i < s.hintsLoaded.length; i++) {
    hintsHtml += '<div class="pp-hint rendered-math"><strong>Hint ' + (i+1) + ':</strong> ' + renderMathText(s.hintsLoaded[i]) + '</div>';
  }
  if (s.hintLoading) {
    hintsHtml += '<button class="pp-hint-btn" disabled><span style="display:inline-block;animation:spin 1s linear infinite">\u2699</span> Generating hint...</button>';
  } else if (s.hintsLoaded.length < 3) {
    hintsHtml += '<button class="pp-hint-btn" onclick="ppRevealHint()">\uD83D\uDCA1 Get Hint ' + (s.hintsLoaded.length + 1) + '</button>';
  }
  hintsHtml += '</div>';

  let solutionHtml = '<div class="pp-solution-section">';
  if (s.solutionRevealed) {
    solutionHtml += '<div class="pp-solution">';
    if (p.final_answer) solutionHtml += '<div style="margin-bottom:10px"><strong>Final Answer:</strong> <span class="rendered-math">' + renderMathText(p.final_answer) + '</span></div>';
    if (p.full_solution) solutionHtml += '<div><strong>Full Solution:</strong></div><div class="rendered-math" style="margin-top:6px;white-space:pre-wrap">' + renderMathText(p.full_solution) + '</div>';
    solutionHtml += '</div>';
  } else if (s.solutionLoading) {
    solutionHtml += '<button class="pp-sol-btn" disabled><span style="display:inline-block;animation:spin 1s linear infinite">\u2699</span> Generating solution...</button>';
  } else {
    solutionHtml += '<button class="pp-sol-btn" onclick="ppRevealSolution()">\uD83D\uDD13 Show Solution</button>';
  }
  solutionHtml += '</div>';

  let resultHtml = '';
  if (s.aiResult) {
    const rLabels = {complete:'\u2713 Complete', partial:'\u00BD Partial', failed:'\u2717 Failed'};
    resultHtml = '<div class="pp-ai-result">' +
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
        '<span>\uD83E\uDD16 AI Evaluation</span>' +
        '<span class="ai-rating-badge ' + s.aiResult.rating + '">' + (rLabels[s.aiResult.rating] || s.aiResult.rating) + '</span>' +
      '</div>' +
      '<div class="ai-explanation">' + escHtml(s.aiResult.ai_explanation) + '</div>' +
    '</div>';
  }

  let ratingHtml = '';
  if (!s.submitted) {
    ratingHtml = '<div style="margin-top:16px">' +
      '<div style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em">Rate your attempt</div>' +
      '<div class="pp-manual-rating">' +
        '<button class="r-complete' + (s.rating==='complete'?' sel':'') + '" onclick="ppSetRating(\'complete\')">\u2713 Complete</button>' +
        '<button class="r-partial' + (s.rating==='partial'?' sel':'') + '" onclick="ppSetRating(\'partial\')">\u00BD Partial</button>' +
        '<button class="r-failed' + (s.rating==='failed'?' sel':'') + '" onclick="ppSetRating(\'failed\')">\u2717 Failed</button>' +
      '</div>' +
    '</div>';
  } else {
    const rColors = {complete:'var(--green)', partial:'var(--amber)', failed:'var(--red)'};
    ratingHtml = '<div style="margin-top:16px;text-align:center;padding:16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius)">' +
      '<div style="font-size:16px;margin-bottom:4px">\u2705 Attempt Saved</div>' +
      '<div style="font-size:13px;color:' + (rColors[s.rating] || 'var(--muted)') + ';font-weight:600">' + (s.rating ? s.rating.charAt(0).toUpperCase() + s.rating.slice(1) : '') + '</div>' +
    '</div>';
  }

  el.innerHTML =
    '<div class="pp-header">' +
      '<h2>' + escHtml(p.title) + '</h2>' +
      '<span class="pp-diff pp-diff-' + p.difficulty + '">' + (diffLabels[p.difficulty] || 'Medium') + '</span>' +
      (p.skill_tag ? '<span class="pp-skill">' + escHtml(p.skill_tag) + '</span>' : '') +
      '<button class="pp-close" onclick="ppCloseSolve()">&times;</button>' +
    '</div>' +
    '<div class="pp-body">' +
      '<div class="pp-prompt rendered-math">' + renderMathText(p.prompt) + '</div>' +
      hintsHtml +
      '<div class="pp-answer-section">' +
        '<label style="display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em">Your answer</label>' +
        '<textarea id="pp-user-answer" rows="5" placeholder="Write your solution here..."' + (s.submitted ? ' disabled' : '') + '>' + escHtml(s.userAnswer) + '</textarea>' +
        '<div id="pp-answer-preview" class="math-preview rendered-math"></div>' +
        '<div class="math-helper" style="margin-top:6px">' +
          '<math-field id="mf-pp-helper" virtual-keyboard-mode="onfocus" style="flex:1;min-height:38px;font-size:14px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:8px"></math-field>' +
          '<button type="button" class="mh-insert" onclick="insertMath(\'mf-pp-helper\',\'pp-user-answer\')">&#x2795; Insert Math</button>' +
        '</div>' +
      '</div>' +
      (!s.submitted ? '<div class="pp-submit-bar">' +
        '<button class="pp-submit-btn ai" onclick="ppSubmitAI()" id="pp-ai-btn">\uD83E\uDD16 Submit for AI Evaluation</button>' +
      '</div>' : '') +
      resultHtml +
      solutionHtml +
      ratingHtml +
      (s.submitted ? '<div style="text-align:center;margin-top:16px"><button class="pp-submit-btn ai" onclick="ppCloseSolve()" style="min-width:160px">Done</button></div>' : '') +
      (!s.submitted && s.rating ? '<div style="text-align:center;margin-top:12px"><button class="pp-submit-btn ai" onclick="ppSubmitFinal()">\uD83D\uDCBE Save Attempt</button></div>' : '') +
      ppAttemptHistoryHtml(s) +
    '</div>';

  /* Wire up live preview */
  const ta = document.getElementById('pp-user-answer');
  if (ta && !s.submitted) {
    ta.addEventListener('input', function() {
      ppSolveState.userAnswer = ta.value;
      livePreview('pp-user-answer', 'pp-answer-preview');
    });
  }
  /* Render math in prompt, hints, solution */
  el.querySelectorAll('.rendered-math').forEach(renderMathIn);
}

async function ppRevealHint() {
  if (!ppSolveState || ppSolveState.hintLoading) return;
  ppSolveState.hintLoading = true;
  ppRenderSolve();
  try {
    const res = await fetch('/api/problems/' + ppSolveState.problem.id + '/hint', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ hints_so_far: ppSolveState.hintsLoaded }),
    });
    const data = await res.json();
    if (res.ok && data.hint) {
      ppSolveState.hintsLoaded.push(data.hint);
      ppSolveState.hintsRevealed = ppSolveState.hintsLoaded.length;
    } else {
      toast(data.error || 'Failed to generate hint', 'warn');
    }
  } catch(e) {
    toast('Failed to generate hint', 'warn');
  }
  ppSolveState.hintLoading = false;
  ppRenderSolve();
}

async function ppRevealSolution() {
  if (!ppSolveState || ppSolveState.solutionLoading) return;
  ppSolveState.solutionLoading = true;
  ppRenderSolve();
  try {
    const res = await fetch('/api/problems/' + ppSolveState.problem.id + '/solution', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
    });
    const data = await res.json();
    if (res.ok) {
      ppSolveState.problem.final_answer = data.final_answer || '';
      ppSolveState.problem.full_solution = data.full_solution || '';
      ppSolveState.solutionRevealed = true;
    } else {
      toast(data.error || 'Failed to generate solution', 'warn');
    }
  } catch(e) {
    toast('Failed to generate solution', 'warn');
  }
  ppSolveState.solutionLoading = false;
  ppRenderSolve();
}

function ppSetRating(r) {
  if (!ppSolveState || ppSolveState.submitted) return;
  ppSolveState.rating = r;
  ppRenderSolve();
}

async function ppSubmitAI() {
  if (!ppSolveState) return;
  const ta = document.getElementById('pp-user-answer');
  if (ta) ppSolveState.userAnswer = ta.value;
  if (!ppSolveState.userAnswer.trim()) {
    toast('Write an answer before submitting for AI evaluation', 'warn');
    return;
  }
  const btn = document.getElementById('pp-ai-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span style="display:inline-block;animation:spin 1s linear infinite">\u2699</span> Evaluating...'; }

  try {
    const res = await fetch('/api/problems/' + ppSolveState.problem.id + '/attempt', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        user_answer: ppSolveState.userAnswer,
        hints_used: ppSolveState.hintsRevealed,
        solution_viewed: ppSolveState.solutionRevealed ? 1 : 0,
      }),
    });
    const data = await res.json();
    if (res.ok) {
      ppSolveState.aiResult = data;
      ppSolveState.rating = data.rating;
      ppSolveState.submitted = true;
      ppRenderSolve();
      ppLoadProblems();
    } else {
      toast(data.error || 'AI evaluation failed', 'warn');
      if (btn) { btn.disabled = false; btn.innerHTML = '\uD83E\uDD16 Submit for AI Evaluation'; }
    }
  } catch(e) {
    toast('AI evaluation failed', 'warn');
    if (btn) { btn.disabled = false; btn.innerHTML = '\uD83E\uDD16 Submit for AI Evaluation'; }
  }
}

async function ppSubmitFinal() {
  if (!ppSolveState || ppSolveState.submitted) return;
  const ta = document.getElementById('pp-user-answer');
  if (ta) ppSolveState.userAnswer = ta.value;

  try {
    const res = await fetch('/api/problems/' + ppSolveState.problem.id + '/attempt', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        user_answer: ppSolveState.userAnswer,
        rating: ppSolveState.rating,
        hints_used: ppSolveState.hintsRevealed,
        solution_viewed: ppSolveState.solutionRevealed ? 1 : 0,
      }),
    });
    const data = await res.json();
    if (res.ok) {
      ppSolveState.submitted = true;
      ppRenderSolve();
      ppLoadProblems();
      toast('Attempt saved \u2014 ' + ppSolveState.rating, 'success');
    } else {
      toast(data.error || 'Failed to save', 'warn');
    }
  } catch(e) { toast('Failed to save attempt', 'warn'); }
}

/* ═══ Problem Generator Modal ═══ */

let ppPdfFile = null;
let ppPdfRecommended = 5;

function ppSwitchGenTab(tab) {
  document.querySelectorAll('#pp-gen-modal .gen-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('#pp-gen-modal .gen-tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('pp-gen-tab-' + tab).classList.add('active');
  document.getElementById('pp-gen-panel-' + tab).classList.add('active');
}

function ppPopulateGenTopicSelects() {
  const ids = ['pp-prompt-topic-select', 'pp-pdf-topic-select'];
  ids.forEach(function(id) {
    const sel = document.getElementById(id);
    if (!sel) return;
    const isPrompt = id.indexOf('prompt') !== -1;
    let opts = '<option value="">— Select topic —</option>';
    opts += isPrompt ? '<option value="__new__">+ Create new topic</option>' : '<option value="__new__">+ Create new topic from PDF</option>';
    all.forEach(t => { opts += '<option value="' + t.id + '">' + escHtml(t.name) + '</option>'; });
    sel.innerHTML = opts;
    if (ppCurrentTopic) sel.value = ppCurrentTopic;
    const nameInputId = isPrompt ? 'pp-prompt-new-topic-name' : 'pp-pdf-new-topic-name';
    sel.onchange = function() {
      document.getElementById(nameInputId).style.display = sel.value === '__new__' ? '' : 'none';
    };
  });
}

function ppOpenGenModal() {
  /* Reset prompt tab */
  document.getElementById('pp-gen-prompt-text').value = '';
  document.getElementById('pp-prompt-new-topic-name').style.display = 'none';
  document.getElementById('pp-prompt-new-topic-name').value = '';
  document.getElementById('pp-prompt-slider').value = 5;
  document.getElementById('pp-prompt-count').textContent = '5';
  document.getElementById('pp-prompt-generate-btn').disabled = false;
  document.getElementById('pp-prompt-generate-btn').textContent = 'Generate Problems';
  /* Reset PDF tab */
  ppPdfFile = null;
  document.getElementById('pp-pdf-file-info').style.display = 'none';
  document.getElementById('pp-pdf-slider-section').style.display = 'none';
  document.getElementById('pp-pdf-prompt-text').style.display = 'none';
  document.getElementById('pp-pdf-prompt-text').value = '';
  document.getElementById('pp-pdf-preview').style.display = 'none';
  document.getElementById('pp-pdf-page-range').style.display = 'none';
  document.getElementById('pp-pdf-generate-btn').disabled = true;
  document.getElementById('pp-pdf-generate-btn').textContent = 'Generate Problems';
  document.getElementById('pp-pdf-dropzone').style.display = '';
  document.getElementById('pp-pdf-new-topic-name').style.display = 'none';
  document.getElementById('pp-pdf-new-topic-name').value = '';
  /* Populate topic selectors */
  ppPopulateGenTopicSelects();
  /* Default to prompt tab */
  ppSwitchGenTab('prompt');
  document.getElementById('pp-gen-modal').classList.add('open');
}

function ppCloseGenModal() {
  document.getElementById('pp-gen-modal').classList.remove('open');
  ppPdfFile = null;
  const inp = document.getElementById('pp-pdf-file-input');
  if (inp) inp.value = '';
}

function ppUpdatePdfSlider(val) {
  document.getElementById('pp-pdf-count').textContent = val;
  const badge = document.getElementById('pp-pdf-rec-badge');
  const diff = Math.abs(parseInt(val) - ppPdfRecommended);
  badge.style.display = diff <= 1 ? '' : 'none';
}

(function() {
  const dz = document.getElementById('pp-pdf-dropzone');
  const inp = document.getElementById('pp-pdf-file-input');
  if (!dz || !inp) return;
  dz.addEventListener('click', function(e) { if (e.target !== inp) inp.click(); });
  dz.addEventListener('dragover', function(e) { e.preventDefault(); dz.style.borderColor = 'var(--teal)'; dz.style.background = 'var(--teal-light)'; });
  dz.addEventListener('dragleave', function() { dz.style.borderColor = 'var(--border)'; dz.style.background = ''; });
  dz.addEventListener('drop', function(e) {
    e.preventDefault(); dz.style.borderColor = 'var(--border)'; dz.style.background = '';
    if (e.dataTransfer.files.length) ppHandlePdfFile(e.dataTransfer.files[0]);
  });
  inp.addEventListener('change', function() { if (inp.files[0]) ppHandlePdfFile(inp.files[0]); });
})();

async function ppHandlePdfFile(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) { toast('Please select a PDF file', 'warn'); return; }
  ppPdfFile = file;
  document.getElementById('pp-pdf-file-name').textContent = file.name;
  document.getElementById('pp-pdf-file-meta').textContent = (file.size / 1024).toFixed(0) + ' KB';
  document.getElementById('pp-pdf-file-info').style.display = 'flex';
  document.getElementById('pp-pdf-dropzone').style.display = 'none';
  document.getElementById('pp-pdf-generate-btn').disabled = true;
  document.getElementById('pp-pdf-generate-btn').textContent = 'Analyzing...';
  /* Estimate */
  const formData = new FormData();
  formData.append('pdf', file);
  try {
    const res = await fetch('/api/estimate-pdf', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      toast(data.error || 'Failed to analyze PDF', 'warn');
      document.getElementById('pp-pdf-file-info').style.display = 'none';
      document.getElementById('pp-pdf-dropzone').style.display = '';
      document.getElementById('pp-pdf-generate-btn').disabled = true;
      document.getElementById('pp-pdf-generate-btn').textContent = 'Generate Problems';
      ppPdfFile = null;
      return;
    }
    document.getElementById('pp-pdf-file-meta').textContent =
      data.page_count + ' page' + (data.page_count !== 1 ? 's' : '') +
      ' \u00B7 ' + (data.char_count / 1000).toFixed(1) + 'k characters';
    /* Show page range controls */
    const totalPages = data.page_count;
    document.getElementById('pp-pdf-total-pages').textContent = totalPages + ' pages total';
    var defEnd = Math.min(totalPages, 30);
    initDualRange('pp-pdf', totalPages, 1, defEnd, function(s, e) {
      updatePdfPreviewHighlight('pp-pdf', s, e);
    });
    document.getElementById('pp-pdf-page-range').style.display = '';
    renderPdfPreview('pp-pdf', file);
    ppPdfRecommended = Math.max(1, Math.min(20, Math.round(data.recommended_cards / 2)));
    document.getElementById('pp-pdf-slider').value = ppPdfRecommended;
    ppUpdatePdfSlider(ppPdfRecommended);
    document.getElementById('pp-pdf-rec-badge').style.display = '';
    document.getElementById('pp-pdf-slider-section').style.display = '';
    document.getElementById('pp-pdf-prompt-text').style.display = '';
    document.getElementById('pp-pdf-generate-btn').disabled = false;
    document.getElementById('pp-pdf-generate-btn').textContent = 'Generate Problems';
  } catch(e) {
    toast('Failed to analyze PDF: ' + e.message, 'warn');
    document.getElementById('pp-pdf-file-info').style.display = 'none';
    document.getElementById('pp-pdf-dropzone').style.display = '';
    document.getElementById('pp-pdf-generate-btn').disabled = true;
    document.getElementById('pp-pdf-generate-btn').textContent = 'Generate Problems';
    ppPdfFile = null;
  }
}

async function ppGenerateFromPdf() {
  if (!ppPdfFile) return;
  const selVal = document.getElementById('pp-pdf-topic-select').value;
  if (!selVal) { toast('Please select a topic', 'warn'); return; }
  const numProblems = parseInt(document.getElementById('pp-pdf-slider').value) || 5;
  const btn = document.getElementById('pp-pdf-generate-btn');
  btn.disabled = true;
  btn.innerHTML = '<span style="display:inline-block;animation:spin 1s linear infinite">\u2699</span> Generating ' + numProblems + ' problems...';

  const formData = new FormData();
  formData.append('pdf', ppPdfFile);
  formData.append('num_problems', numProblems);
  const pageStart = parseInt(document.getElementById('pp-pdf-page-start').value) || 1;
  const pageEnd = parseInt(document.getElementById('pp-pdf-page-end').value) || 9999;
  formData.append('page_start', pageStart);
  formData.append('page_end', pageEnd);
  const pdfPrompt = document.getElementById('pp-pdf-prompt-text').value.trim();
  if (pdfPrompt) formData.append('prompt', pdfPrompt);
  if (selVal === '__new__') {
    const newName = document.getElementById('pp-pdf-new-topic-name').value.trim();
    if (newName) formData.append('new_topic_name', newName);
  } else {
    formData.append('topic_id', selVal);
  }

  try {
    const res = await fetch('/api/import-pdf-problems', { method: 'POST', body: formData });
    const result = await res.json();
    ppCloseGenModal();
    if (res.ok && result.ok) {
      let msg = result.problem_count + ' practice problems';
      if (result.extracted_count > 0 && result.generated_count > 0) {
        msg += ' (' + result.extracted_count + ' extracted from textbook, ' + result.generated_count + ' AI-generated)';
      } else if (result.extracted_count > 0) {
        msg += ' (extracted from textbook)';
      } else {
        msg += ' (AI-generated)';
      }
      toast(msg + '!', 'success');
      await load();
      if (result.topic_id) {
        ppCurrentTopic = result.topic_id;
        const ppSel = document.getElementById('pp-topic-select');
        ppPopulateTopics();
        ppSel.value = result.topic_id;
      }
      await ppLoadProblems();
    } else {
      toast(result.error || 'PDF import failed', 'warn');
    }
  } catch(e) {
    ppCloseGenModal();
    toast('PDF import failed: ' + e.message, 'warn');
  }
}

async function ppGenerateFromPrompt() {
  const promptText = document.getElementById('pp-gen-prompt-text').value.trim();
  if (!promptText) { toast('Please enter a prompt describing what problems to generate', 'warn'); return; }
  const selVal = document.getElementById('pp-prompt-topic-select').value;
  if (!selVal) { toast('Please select a topic', 'warn'); return; }
  const numProblems = parseInt(document.getElementById('pp-prompt-slider').value) || 5;
  const btn = document.getElementById('pp-prompt-generate-btn');
  btn.disabled = true;
  btn.innerHTML = '<span style="display:inline-block;animation:spin 1s linear infinite">\u2699</span> Generating ' + numProblems + ' problems...';

  const body = { prompt: promptText, num_problems: numProblems };
  if (selVal === '__new__') {
    const newName = document.getElementById('pp-prompt-new-topic-name').value.trim();
    if (newName) body.new_topic_name = newName;
  } else {
    body.topic_id = selVal;
  }

  try {
    const res = await fetch('/api/generate-problems-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const result = await res.json();
    ppCloseGenModal();
    if (res.ok && result.ok) {
      toast(result.problem_count + ' practice problems generated!', 'success');
      await load();
      if (result.topic_id) {
        ppCurrentTopic = result.topic_id;
        const ppSel = document.getElementById('pp-topic-select');
        ppPopulateTopics();
        ppSel.value = result.topic_id;
      }
      await ppLoadProblems();
    } else {
      toast(result.error || 'Problem generation failed', 'warn');
    }
  } catch(e) {
    ppCloseGenModal();
    toast('Problem generation failed: ' + e.message, 'warn');
  }
}

load();
loadStats();
setInterval(load, 60000);
setInterval(checkBrowserNotifications, 300000);
</script>
</body>
</html>"""


# ── Card Editor Page ──────────────────────────────────────────────────────

CARDS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cards — {{ topic_name }}</title>
<script>(function(){var t=localStorage.getItem('theme');if(t==='dark'||(t!=='light'&&window.matchMedia('(prefers-color-scheme:dark)').matches))document.documentElement.dataset.theme='dark';})()</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/mathlive"></script>
<script>
function insertCustomMatrix() {
  var size = prompt('Enter matrix size as rows x cols (e.g. 3x4):');
  if (!size) return;
  var parts = size.toLowerCase().split('x');
  var r = parseInt(parts[0],10), c = parseInt(parts[1],10);
  if (!r || !c || r < 1 || c < 1 || r > 20 || c > 20) { alert('Invalid size. Use format like 3x4 (max 20x20).'); return; }
  var type = prompt('Bracket type?  ( ) = pmatrix,  [ ] = bmatrix,  | | = vmatrix,  { } = Bmatrix\nEnter: p, b, v, or B', 'b');
  var env = type === 'p' ? 'pmatrix' : type === 'v' ? 'vmatrix' : type === 'B' ? 'Bmatrix' : 'bmatrix';
  var rows = [];
  for (var i = 0; i < r; i++) {
    var cols = [];
    for (var j = 0; j < c; j++) cols.push('\\placeholder{}');
    rows.push(cols.join(' & '));
  }
  var latex = '\\begin{' + env + '} ' + rows.join(' \\\\ ') + ' \\end{' + env + '}';
  var mf = document.querySelector('math-field:focus-within') || document.querySelector('math-field:focus');
  if (!mf) { var all = document.querySelectorAll('math-field'); mf = all[all.length-1]; }
  if (mf && mf.executeCommand) {
    mf.executeCommand(['insert', latex]);
  }
}
window.addEventListener('load', function() {
  if (typeof mathVirtualKeyboard === 'undefined') return;
  document.addEventListener('pointerup', function(e) {
    var t = e.target;
    while (t && t !== document) {
      if (t.textContent && t.textContent.trim() === 'N\u00D7M') { setTimeout(insertCustomMatrix, 50); return; }
      t = t.parentElement;
    }
  });
  mathVirtualKeyboard.layouts = [
    'numeric', 'symbols', 'alphabetic', 'greek',
    {label:'\u23A1 \u23A4', tooltip:'Matrices & brackets', rows:[
      [{latex:'\\begin{pmatrix} \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} \\end{pmatrix}', label:'(\u22C5\u22C5)', class:'small'},
       {latex:'\\begin{bmatrix} \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} \\end{bmatrix}', label:'[\u22C5\u22C5]', class:'small'},
       {latex:'\\begin{vmatrix} \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} \\end{vmatrix}', label:'|\u22C5\u22C5|', class:'small'},
       {latex:'\\begin{Bmatrix} \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} \\end{Bmatrix}', label:'{\u22C5\u22C5}', class:'small'}],
      [{latex:'\\begin{pmatrix} \\placeholder{} & \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} & \\placeholder{} \\\\ \\placeholder{} & \\placeholder{} & \\placeholder{} \\end{pmatrix}', label:'3\u00D73', class:'small'},
       {class:'action', label:'N\u00D7M', command:['performWithFeedback','insertCustomMatrix()']},
       {latex:'\\vec{\\placeholder{}}', label:'v\u20D7'}, {latex:'\\hat{\\placeholder{}}', label:'v\u0302'}, {latex:'\\dot{\\placeholder{}}', label:'v\u0307'}, {latex:'\\ddot{\\placeholder{}}', label:'v\u0308'}],
      [{latex:'\\det', label:'det'}, {latex:'\\operatorname{tr}', label:'tr'}, {latex:'\\operatorname{rank}', label:'rank'}, {latex:'\\dim', label:'dim'}, {latex:'\\mathbf{I}', label:'\uD835\uDC08'}, {latex:'\\mathbf{0}', label:'\uD835\uDFCE'}],
      [{latex:'\\cdot', label:'\u22C5'}, {latex:'\\times', label:'\u00D7'}, {latex:'\\otimes', label:'\u2297'}, {latex:'\\oplus', label:'\u2295'}, {latex:'^{\\top}', label:'\u22A4'}, {latex:'^{\\dagger}', label:'\u2020'}]
    ]},
    {label:'\u2200', tooltip:'Logic & set theory', rows:[
      [{latex:'\\land', label:'\u2227'}, {latex:'\\lor', label:'\u2228'}, {latex:'\\neg', label:'\u00AC'}, {latex:'\\implies', label:'\u21D2'}, {latex:'\\iff', label:'\u21D4'}, {latex:'\\oplus', label:'\u2295'}],
      [{latex:'\\forall', label:'\u2200'}, {latex:'\\exists', label:'\u2203'}, {latex:'\\nexists', label:'\u2204'}, {latex:'\\top', label:'\u22A4'}, {latex:'\\bot', label:'\u22A5'}, {latex:'\\vdash', label:'\u22A2'}],
      [{latex:'\\in', label:'\u2208'}, {latex:'\\notin', label:'\u2209'}, {latex:'\\subset', label:'\u2282'}, {latex:'\\subseteq', label:'\u2286'}, {latex:'\\supset', label:'\u2283'}, {latex:'\\supseteq', label:'\u2287'}],
      [{latex:'\\cup', label:'\u222A'}, {latex:'\\cap', label:'\u2229'}, {latex:'\\setminus', label:'\u2216'}, {latex:'\\emptyset', label:'\u2205'}, {latex:'\\mathbb{N}', label:'\u2115'}, {latex:'\\mathbb{Z}', label:'\u2124'}, {latex:'\\mathbb{R}', label:'\u211D'}, {latex:'\\mathbb{C}', label:'\u2102'}]
    ]},
    {label:'\u21D2', tooltip:'Arrows & relations', rows:[
      [{latex:'\\leftarrow', label:'\u2190'}, {latex:'\\rightarrow', label:'\u2192'}, {latex:'\\leftrightarrow', label:'\u2194'}, {latex:'\\Leftarrow', label:'\u21D0'}, {latex:'\\Rightarrow', label:'\u21D2'}, {latex:'\\Leftrightarrow', label:'\u21D4'}],
      [{latex:'\\uparrow', label:'\u2191'}, {latex:'\\downarrow', label:'\u2193'}, {latex:'\\mapsto', label:'\u21A6'}, {latex:'\\hookrightarrow', label:'\u21AA'}, {latex:'\\nearrow', label:'\u2197'}, {latex:'\\searrow', label:'\u2198'}],
      [{latex:'\\equiv', label:'\u2261'}, {latex:'\\approx', label:'\u2248'}, {latex:'\\sim', label:'\u223C'}, {latex:'\\cong', label:'\u2245'}, {latex:'\\propto', label:'\u221D'}, {latex:'\\neq', label:'\u2260'}],
      [{latex:'\\leq', label:'\u2264'}, {latex:'\\geq', label:'\u2265'}, {latex:'\\ll', label:'\u226A'}, {latex:'\\gg', label:'\u226B'}, {latex:'\\prec', label:'\u227A'}, {latex:'\\succ', label:'\u227B'}]
    ]},
    {label:'\u222B', tooltip:'Calculus & analysis', rows:[
      [{latex:'\\frac{d}{d\\placeholder{}}', label:'d/dx'}, {latex:'\\frac{\\partial}{\\partial \\placeholder{}}', label:'\u2202/\u2202x'}, {latex:'\\nabla', label:'\u2207'}, {latex:'\\Delta', label:'\u0394'}, {latex:'\\partial', label:'\u2202'}],
      [{latex:'\\int_{\\placeholder{}}^{\\placeholder{}}', label:'\u222B'}, {latex:'\\iint', label:'\u222C'}, {latex:'\\iiint', label:'\u222D'}, {latex:'\\oint', label:'\u222E'}, {latex:'\\lim_{\\placeholder{} \\to \\placeholder{}}', label:'lim'}],
      [{latex:'\\sum_{\\placeholder{}}^{\\placeholder{}}', label:'\u2211'}, {latex:'\\prod_{\\placeholder{}}^{\\placeholder{}}', label:'\u220F'}, {latex:'\\infty', label:'\u221E'}, {latex:'\\to', label:'\u2192'}, {latex:'\\pm', label:'\u00B1'}, {latex:'\\mp', label:'\u2213'}],
      [{latex:'\\sin', label:'sin'}, {latex:'\\cos', label:'cos'}, {latex:'\\tan', label:'tan'}, {latex:'\\ln', label:'ln'}, {latex:'\\log', label:'log'}, {latex:'\\exp', label:'exp'}]
    ]},
    {label:'\u21CC', tooltip:'Chemistry & physics', rows:[
      [{latex:'\\rightleftharpoons', label:'\u21CC'}, {latex:'\\xrightarrow{\\placeholder{}}', label:'\u2192\u0332'}, {latex:'\\xleftarrow{\\placeholder{}}', label:'\u2190\u0332'}, {latex:'\\uparrow', label:'\u2191'}, {latex:'\\downarrow', label:'\u2193'}],
      [{latex:'^{\\placeholder{}}_{\\placeholder{}}\\text{\\placeholder{}}', label:'\u00B9X\u2081'}, {latex:'\\Delta H', label:'\u0394H'}, {latex:'\\Delta G', label:'\u0394G'}, {latex:'\\Delta S', label:'\u0394S'}, {latex:'K_{eq}', label:'K\u2091\u2091'}],
      [{latex:'\\alpha', label:'\u03B1'}, {latex:'\\beta', label:'\u03B2'}, {latex:'\\gamma', label:'\u03B3'}, {latex:'\\lambda', label:'\u03BB'}, {latex:'\\mu', label:'\u03BC'}, {latex:'\\nu', label:'\u03BD'}, {latex:'\\omega', label:'\u03C9'}],
      [{latex:'\\hbar', label:'\u210F'}, {latex:'\\ell', label:'\u2113'}, {latex:'\\varepsilon_0', label:'\u03B5\u2080'}, {latex:'\\mu_0', label:'\u03BC\u2080'}, {latex:'k_B', label:'kB'}, {latex:'\\sigma', label:'\u03C3'}]
    ]}
  ];
});
</script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#FAF8F5;--surface:#fff;--border:#E8E0D8;
  --text:#1a1a1a;--muted:#6b6b6b;--hint:#aaa;
  --teal:#B85C38;--teal-light:#FDF0EB;--teal-mid:#E8B4A0;--accent:#B85C38;
  --amber:#7A4A0A;--amber-light:#FAEEDA;--amber-mid:#FAC775;
  --red:#9B2828;--red-light:#FCEBEB;--red-mid:#F7C1C1;
  --green:#3B6D11;--green-light:#EAF3DE;--green-mid:#C0DD97;
  --blue:#185FA5;--blue-light:#E6F1FB;
}
[data-theme="dark"]{
  --bg:#181818;--surface:#222;--border:#333;--text:#ececec;--muted:#aaa;--hint:#666;
  --teal:#E8956E;--teal-light:rgba(232,149,110,.12);--teal-mid:rgba(232,149,110,.3);--accent:#E8956E;
  --amber:#FBBF24;--amber-light:rgba(251,191,36,.1);--amber-mid:rgba(251,191,36,.25);
  --red:#F87171;--red-light:rgba(248,113,113,.1);--red-mid:rgba(248,113,113,.25);
  --green:#86EFAC;--green-light:rgba(134,239,172,.1);--green-mid:rgba(134,239,172,.25);
  --blue:#60A5FA;--blue-light:rgba(96,165,250,.1);
}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;
     -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
     transition:background .2s,color .2s}

/* Theme toggle */
.theme-toggle{background:none;border:1px solid var(--border);border-radius:8px;padding:4px 10px;
  cursor:pointer;font-size:15px;color:var(--muted);transition:all .15s;display:flex;align-items:center;gap:4px}
.theme-toggle:hover{border-color:var(--teal-mid);color:var(--text);background:var(--teal-light)}

/* MathLive dark mode + overrides */
[data-theme="dark"] math-field{--hue:15;--_text-font-family:inherit;background:var(--surface);color:var(--text);border-color:var(--border)}
[data-theme="dark"] math-field::part(menu-toggle){color:var(--muted)}
math-field::part(menu-toggle){display:none}
[data-theme="dark"] .katex{color:var(--text)}

/* Global scrollbar */
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}
*{scrollbar-width:thin;scrollbar-color:var(--border) var(--bg)}

.top-bar{background:var(--surface);border-bottom:1px solid var(--border);
  padding:.8rem 1.5rem;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:100;
  box-shadow:0 1px 3px rgba(0,0,0,.04)}
.top-bar a{color:var(--muted);text-decoration:none;font-size:13px;display:flex;align-items:center;gap:4px;
  padding:4px 10px;border-radius:8px;transition:all .15s}
.top-bar a:hover{color:var(--text);background:var(--bg)}
.top-bar h1{font-size:17px;font-weight:700;flex:1;letter-spacing:-.02em}
.top-bar .count{font-size:12px;color:var(--muted)}

.main{max-width:700px;margin:0 auto;padding:1.5rem}

button{font-family:inherit;cursor:pointer;border-radius:8px;font-size:13px;
       padding:6px 14px;border:1px solid var(--border);background:var(--surface);color:var(--text);transition:all .15s}
button:hover{background:var(--bg);border-color:var(--teal-mid)}
.btn-primary{background:var(--teal);color:#fff;border-color:var(--teal);font-weight:600;
  padding:8px 18px;border-radius:10px;box-shadow:0 2px 8px rgba(184,92,56,.2)}
.btn-primary:hover{box-shadow:0 4px 14px rgba(184,92,56,.3);transform:translateY(-1px)}

/* Add card form */
.add-section{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:1.2rem 1.4rem;margin-bottom:1.25rem;box-shadow:0 1px 4px rgba(0,0,0,.03)}
.add-section h2{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:12px}
.add-section .field{display:flex;flex-direction:column;gap:5px}
.add-section label{font-size:11px;color:var(--muted);font-weight:500}
.add-section select,.add-section textarea,.add-section input[type=text]{
  font-family:inherit;font-size:14px;padding:10px 12px;
  border:2px solid var(--border);border-radius:10px;
  background:var(--surface);color:var(--text);outline:none;width:100%;
  transition:border-color .2s,box-shadow .2s;box-sizing:border-box}
.add-section select{height:44px;-webkit-appearance:auto;appearance:auto}
.add-section textarea{resize:vertical;min-height:44px;line-height:1.5}
.add-section select:focus,.add-section textarea:focus,.add-section input:focus{border-color:var(--teal);box-shadow:0 0 0 3px rgba(184,92,56,.1)}
.add-section textarea::placeholder{color:var(--hint)}
.add-section math-field{display:block;min-height:44px;padding:6px 10px;
  border:2px solid var(--border);border-radius:10px;font-size:16px;outline:none;
  background:var(--surface);color:var(--text);width:100%;box-sizing:border-box;
  transition:border-color .2s,box-shadow .2s;
  --caret-color:var(--teal);--selection-background-color:rgba(184,92,56,.18);
  --contains-highlight-background-color:transparent}
.add-section math-field:focus-within{border-color:var(--teal);box-shadow:0 0 0 3px rgba(184,92,56,.1)}

/* MC answer choices builder */
.mc-choices-section{margin-top:12px;padding-top:12px;border-top:1px dashed var(--border);transition:all .3s ease}
.mc-choices-section.hidden{display:none}
.mc-choices-label{font-size:11px;color:var(--muted);font-weight:600;margin-bottom:8px;display:block;
  text-transform:uppercase;letter-spacing:.04em}
.mc-choices-list{display:flex;flex-direction:column;gap:6px}
.mc-choice-row{display:flex;align-items:center;gap:8px;animation:mcRowIn .2s ease}
@keyframes mcRowIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
.mc-choice-toggle{width:32px;height:32px;border-radius:50%;border:2px solid var(--border);
  background:var(--surface);cursor:pointer;display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:700;transition:all .15s;flex-shrink:0;outline:none}
.mc-choice-toggle:hover{transform:scale(1.1)}
.mc-choice-toggle.correct{background:var(--teal);border-color:var(--teal);color:#fff}
.mc-choice-toggle.wrong{background:var(--red);border-color:var(--red);color:#fff}
.mc-choice-input{font-family:inherit;font-size:13px;padding:8px 12px;
  border:2px solid var(--border);border-radius:10px;background:var(--surface);color:var(--text);
  outline:none;flex:1;transition:border-color .2s,box-shadow .2s}
.mc-choice-input:focus{border-color:var(--teal);box-shadow:0 0 0 3px rgba(184,92,56,.1)}
.mc-choice-input::placeholder{color:var(--hint)}
.mc-choice-input.correct-border{border-color:var(--teal)}
.mc-choice-input.wrong-border{border-color:var(--red)}
.mc-choice-mathfield{font-size:14px;padding:6px 10px;
  border:2px solid var(--border);border-radius:10px;background:var(--surface);color:var(--text);
  outline:none;flex:1;transition:border-color .2s,box-shadow .2s;min-height:38px;
  --caret-color:var(--teal);--selection-background-color:rgba(184,92,56,.18);
  --contains-highlight-background-color:transparent}
.mc-choice-mathfield:focus-within{border-color:var(--teal);box-shadow:0 0 0 3px rgba(184,92,56,.1)}
.mc-choice-mathfield.correct-border{border-color:var(--teal)}
.mc-choice-mathfield.wrong-border{border-color:var(--red)}
.mc-choice-remove{
  background:transparent;cursor:pointer;display:flex;align-items:center;justify-content:center;
  font-size:16px;color:var(--hint);transition:all .15s;flex-shrink:0}
.mc-choice-remove:hover{background:var(--red-light);color:var(--red);border-color:var(--red-mid)}
.mc-add-choice-btn{font-family:inherit;font-size:12px;font-weight:600;color:var(--blue);
  background:transparent;border:1.5px dashed var(--border);border-radius:10px;padding:8px 16px;
  cursor:pointer;transition:all .15s;margin-top:8px;width:100%;text-align:center}
.mc-add-choice-btn:hover{border-color:var(--blue);background:rgba(59,130,246,.05)}

/* Card display choice list */
.fc-choices-list{display:flex;flex-direction:column;gap:4px;padding:6px 0}
.fc-choice-item{display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:8px;
  font-size:13px;line-height:1.5}
.fc-choice-correct{background:rgba(184,92,56,.06);border:1px solid rgba(184,92,56,.15)}
.fc-choice-wrong{background:rgba(239,68,68,.04);border:1px solid rgba(239,68,68,.1)}
.fc-choice-badge{width:20px;height:20px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;font-size:10px;font-weight:700;flex-shrink:0}
.fc-choice-badge.correct{background:var(--teal);color:#fff}
.fc-choice-badge.wrong{background:var(--red);color:#fff}

/* Card list */
.card-list{display:flex;flex-direction:column;gap:10px}
.fc{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:1.1rem 1.3rem;transition:box-shadow .2s,transform .15s}
.fc:hover{box-shadow:0 2px 12px rgba(0,0,0,.06);transform:translateY(-1px)}
.fc-top{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:6px}
.fc-type{font-size:10px;color:var(--hint);background:var(--bg);border:1px solid var(--border);
  border-radius:6px;padding:3px 10px;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
.fc-actions{display:flex;gap:4px}
.fc-actions button{font-size:12px;padding:3px 8px;border-radius:6px}
.fc-edit{color:var(--blue);border-color:var(--blue-light)}
.fc-edit:hover{background:var(--blue-light)}
.fc-del{color:var(--red);border-color:transparent}
.fc-del:hover{background:var(--red-light);border-color:var(--red-mid)}
.fc-q{font-size:15px;font-weight:600;line-height:1.5;margin-bottom:6px}
.fc-a{font-size:13px;color:var(--muted);line-height:1.5;padding:8px 12px;background:var(--bg);border-radius:8px;border:1px solid var(--border)}
.fc-a-label{font-size:11px;color:var(--hint);text-transform:uppercase;letter-spacing:.03em;margin-bottom:4px}

/* Inline edit mode */
.fc.editing{border-color:var(--teal-mid);box-shadow:0 0 0 3px var(--teal-light)}
.fc .edit-fields{display:none;margin-top:10px}
.fc.editing .edit-fields{display:flex;flex-direction:column;gap:10px}
.fc.editing .fc-q,.fc.editing .fc-a,.fc.editing .fc-a-label{display:none}
.edit-fields textarea,.edit-fields select{font-family:inherit;font-size:14px;padding:10px 12px;
  border:2px solid var(--border);border-radius:10px;background:var(--surface);color:var(--text);outline:none;width:100%;
  transition:border-color .2s,box-shadow .2s;line-height:1.5}
.edit-fields textarea{resize:vertical;min-height:48px}
.edit-fields textarea:focus,.edit-fields select:focus{border-color:var(--teal);box-shadow:0 0 0 3px rgba(184,92,56,.1)}
.edit-fields textarea::placeholder{color:var(--hint)}
.edit-actions{display:flex;gap:8px;justify-content:flex-end}

.empty{text-align:center;padding:3rem;color:var(--hint)}

/* Cards toolbar */
.cards-toolbar{display:flex;gap:8px;align-items:center;margin-bottom:1rem;flex-wrap:wrap}
.cards-toolbar input[type=text]{flex:1;min-width:160px;padding:9px 12px;border:2px solid var(--border);
  border-radius:10px;font-size:13px;font-family:inherit;background:var(--surface);color:var(--text);outline:none;
  transition:border-color .2s,box-shadow .2s}
.cards-toolbar input:focus{border-color:var(--teal);box-shadow:0 0 0 3px rgba(184,92,56,.1)}
.cards-toolbar input::placeholder{color:var(--hint)}
.cards-toolbar select{padding:8px 12px;border:2px solid var(--border);border-radius:10px;font-size:12px;
  font-family:inherit;background:var(--surface);color:var(--text);outline:none;cursor:pointer;
  transition:border-color .2s,box-shadow .2s}
.cards-toolbar select:focus{border-color:var(--teal);box-shadow:0 0 0 3px rgba(184,92,56,.1)}

/* Bulk import collapsible */
#bulk-body.open{max-height:600px !important}

/* Card difficulty indicator */
.fc-stats{display:flex;gap:10px;margin-top:6px;font-size:11px;color:var(--hint)}
.fc-stats span{display:flex;align-items:center;gap:3px}

/* Toast */
.toast-wrap{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.toast{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 16px;
  font-size:13px;box-shadow:0 4px 16px rgba(0,0,0,.1);pointer-events:auto;
  animation:toastIn .3s ease forwards;display:flex;align-items:center;gap:8px;max-width:320px}
.toast.out{animation:toastOut .25s ease forwards}
.toast-icon{font-size:16px;flex-shrink:0}
.toast-success{border-left:3px solid var(--teal)}
.toast-info{border-left:3px solid var(--blue)}
.toast-warn{border-left:3px solid #EF9F27}
@keyframes toastIn{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:translateX(0)}}
@keyframes toastOut{from{opacity:1;transform:translateX(0)}to{opacity:0;transform:translateX(30px)}}

/* Math helper widget — inline math-field + insert button */
.math-helper{display:flex;align-items:center;gap:6px;border:1px solid var(--border);border-radius:8px;padding:3px 4px;margin-top:4px}
.math-helper math-field{flex:1;min-width:120px;font-size:14px;border:none;outline:none;background:transparent;color:var(--text);min-height:32px}
.math-helper .mh-insert{padding:3px 10px;font-size:11px;font-weight:600;border-radius:6px;border:1px solid var(--teal-mid);
  background:var(--teal-light);color:var(--teal);cursor:pointer;white-space:nowrap;transition:all .15s}
.math-helper .mh-insert:hover{background:var(--teal-mid);color:#fff}
.math-helper-label{font-size:10px;color:var(--hint);margin-top:2px}

.math-preview{min-height:36px;padding:8px 14px;background:var(--bg);border:1px solid var(--border);
  border-radius:10px;margin-top:6px;font-size:16px;line-height:1.6;overflow-x:auto;
  transition:border-color .2s;display:none}
.math-preview:not(:empty){border-color:var(--teal-mid)}
.math-preview .katex{font-size:1.15em !important}
.math-preview-hint{color:var(--hint);font-size:12px;font-style:italic}
.math-preview-text{color:var(--text);font-size:15px}

/* KaTeX overrides */
.katex{font-size:1em !important}
.rendered-math .katex{font-size:1.1em !important}
</style>
</head>
<body>

<div class="top-bar">
  <a href="/">&larr; Back</a>
  <h1>{{ topic_name }}</h1>
  <span class="count" id="card-count"></span>
  <button class="theme-toggle" onclick="toggleTheme()" id="theme-btn" title="Toggle light/dark mode">☼</button>
</div>
<div class="toast-wrap" id="toast-wrap"></div>

<div class="main">
  <div class="add-section">
    <h2>Add New Card</h2>
    <div style="display:flex;gap:12px;align-items:flex-end;margin-bottom:14px;flex-wrap:wrap">
      <div class="field" style="flex:0 0 160px">
        <label>Type</label>
        <select id="new-type" onchange="toggleAnswerMode()">
          <option value="qa">Q &amp; A</option>
          <option value="recall">Free Recall</option>
        </select>
      </div>
      <div style="flex:1"></div>
      <button class="btn-primary" onclick="addCard()" style="white-space:nowrap;height:44px;padding:0 24px">+ Add Card</button>
    </div>
    <div class="field" style="margin-bottom:14px">
      <label>Question / Prompt</label>
      <textarea id="new-q" rows="2" placeholder="e.g. What is the forgetting curve?" oninput="livePreview('new-q','new-q-preview')"></textarea>
      <div id="new-q-preview" class="math-preview rendered-math"></div>
      <div class="math-helper">
        <math-field id="mf-new-q" virtual-keyboard-mode="onfocus"></math-field>
        <button type="button" class="mh-insert" onclick="insertMath('mf-new-q','new-q')">&#x2795; Insert Math</button>
      </div>
    </div>
    <div class="field" id="new-a-field" style="margin-bottom:14px">
      <label>Answer</label>
      <textarea id="new-a" rows="2" placeholder="Answer (for free recall)" oninput="livePreview('new-a','new-a-preview')"></textarea>
      <div id="new-a-preview" class="math-preview rendered-math"></div>
      <div class="math-helper">
        <math-field id="mf-new-a" virtual-keyboard-mode="onfocus"></math-field>
        <button type="button" class="mh-insert" onclick="insertMath('mf-new-a','new-a')">&#x2795; Insert Math</button>
      </div>
    </div>
    <div id="mc-choices-section" class="mc-choices-section">
      <label class="mc-choices-label">Answer Choices <span style="font-size:10px;color:var(--hint)">Click &#x2713;/&#x2717; to mark correct or wrong</span></label>
      <div id="mc-choices-list" class="mc-choices-list"></div>
      <button type="button" class="mc-add-choice-btn" onclick="addChoiceLine()">+ Add Choice</button>
    </div>
  </div>

  <div class="add-section" style="margin-bottom:1.25rem">
    <div style="display:flex;align-items:center;justify-content:space-between;cursor:pointer" onclick="document.getElementById('bulk-body').classList.toggle('open')">
      <h2 style="margin:0">Bulk Import</h2>
      <span style="font-size:12px;color:var(--hint)">&#x25BC;</span>
    </div>
    <div id="bulk-body" style="overflow:hidden;max-height:0;transition:max-height .3s ease">
      <div style="margin-top:10px">
        <div style="display:flex;gap:10px;align-items:flex-end;margin-bottom:8px">
          <div style="display:flex;flex-direction:column;gap:4px;flex:0 0 140px">
            <label style="font-size:11px;color:var(--muted)">Default type</label>
            <select id="bulk-type" style="font-family:inherit;font-size:13px;padding:6px 10px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);outline:none">
              <option value="qa">Q &amp; A</option>
              <option value="recall">Free Recall</option>
            </select>
          </div>
          <div style="flex:1;font-size:11px;color:var(--hint);line-height:1.4;padding-bottom:6px">
            Blank line between cards. For Q&amp;A: first line = question, second = answer.<br>
            For recall: each block is one prompt (no answer needed).<br>
            Override per-card with <code>[qa]</code> or <code>[recall]</code> prefix.
          </div>
        </div>
        <textarea id="bulk-text" rows="8" style="width:100%;font-family:inherit;font-size:13px;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);outline:none;resize:vertical" placeholder="What is spaced repetition?
A learning technique that spaces reviews over increasing intervals

[recall] Explain the forgetting curve in your own words

What is active recall?
Actively retrieving information from memory rather than passively reviewing"></textarea>
        <div style="display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap">
          <button class="btn-primary" onclick="bulkImport()">Import Cards</button>
          <div style="display:flex;align-items:center;gap:6px;margin-left:auto;border:1px solid var(--border);border-radius:8px;padding:4px">
            <math-field id="mf-bulk" virtual-keyboard-mode="onfocus" default-mode="math" style="flex:1;min-width:120px;font-size:14px;border:none;outline:none;background:transparent;color:var(--text);min-height:32px"></math-field>
            <button type="button" class="btn-primary" style="padding:4px 12px;font-size:12px" onclick="insertBulkMath()" title="Insert math expression into textarea">&#x2795; Insert</button>
          </div>
          <span id="bulk-preview" style="font-size:12px;color:var(--hint)"></span>
        </div>
      </div>
    </div>
  </div>

  <div class="cards-toolbar">
    <input type="text" id="cards-search" placeholder="Search cards..." oninput="renderCards()">
    <select id="cards-sort" onchange="renderCards()">
      <option value="default">Sort: Default</option>
      <option value="box-asc">Box (low first)</option>
      <option value="box-desc">Box (high first)</option>
      <option value="alpha">Alphabetical</option>
      <option value="fails">Most Failed</option>
    </select>
  </div>

  <div class="card-list" id="card-list">
    <div class="empty">Loading...</div>
  </div>
</div>

<script>
/* ── Theme Toggle ────────────────────────────────── */
function toggleTheme(){
  const html=document.documentElement;
  const isDark=html.dataset.theme==='dark';
  html.dataset.theme=isDark?'':'dark';
  localStorage.setItem('theme',isDark?'light':'dark');
  document.getElementById('theme-btn').textContent=isDark?'☼':'☾';
}
(function(){var b=document.getElementById('theme-btn');if(b)b.textContent=document.documentElement.dataset.theme==='dark'?'☾':'☼';})();

/* ── Math helpers ────────────────────────────────── */

/* Generic: insert LaTeX from a math-field helper into a target textarea/input */
function insertMath(mfId, targetId) {
  const mf = document.getElementById(mfId);
  const target = document.getElementById(targetId);
  if (!mf || !target) return;
  const latex = mf.value.trim();
  if (!latex) return;
  const wrap = '$' + latex + '$';
  if (target.tagName === 'TEXTAREA' || target.type === 'text') {
    const start = target.selectionStart || 0, end = target.selectionEnd || 0;
    target.value = target.value.substring(0, start) + wrap + target.value.substring(end);
    target.selectionStart = target.selectionEnd = start + wrap.length;
  } else {
    target.value += wrap;
  }
  mf.value = '';
  target.focus();
  target.dispatchEvent(new Event('input'));
}

/* Shortcut for bulk import */
function insertBulkMath() { insertMath('mf-bulk', 'bulk-text'); }

/* Live preview: render LaTeX from textarea into a preview div */
function livePreview(srcId, prevId) {
  const src = document.getElementById(srcId);
  const prev = document.getElementById(prevId);
  if (!src || !prev) return;
  const val = src.value.trim();
  if (!val || !val.includes('$')) { prev.style.display = 'none'; prev.innerHTML = ''; return; }
  prev.style.display = 'block';
  prev.textContent = val;
  renderMathIn(prev);
}

function renderMathIn(el) {
  if (typeof renderMathInElement === 'function') {
    renderMathInElement(el, {
      delimiters: [
        {left: '$$', right: '$$', display: true},
        {left: '$', right: '$', display: false},
        {left: '\\(', right: '\\)', display: false},
        {left: '\\[', right: '\\]', display: true},
      ],
      throwOnError: false,
    });
  }
}

function renderMathText(text) {
  const span = document.createElement('span');
  span.textContent = text;
  const escaped = span.innerHTML;
  const tmp = document.createElement('span');
  tmp.className = 'rendered-math';
  tmp.innerHTML = escaped;
  renderMathIn(tmp);
  return tmp.innerHTML;
}

const TID = {{ topic_id }};
let cards = [];

function toast(msg, type='success') {
  const wrap = document.getElementById('toast-wrap');
  const icons = {success:'\u2705', info:'\u2139\ufe0f', warn:'\u26a0\ufe0f'};
  const el = document.createElement('div');
  el.className = 'toast toast-' + type;
  el.innerHTML = `<span class="toast-icon">${icons[type]||''}</span><span>${msg}</span>`;
  wrap.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, 3000);
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

/* Toggle answer mode based on card type */
function toggleAnswerMode() {
  const ct = document.getElementById('new-type').value;
  const mcSec = document.getElementById('mc-choices-section');
  const aField = document.getElementById('new-a-field');
  if (ct === 'qa') {
    if (mcSec) mcSec.classList.remove('hidden');
    if (aField) aField.style.display = 'none';
    /* Ensure at least one correct + one wrong line */
    const list = document.getElementById('mc-choices-list');
    if (list && !list.children.length) {
      addChoiceLine(true);
      addChoiceLine(false);
      addChoiceLine(false);
      addChoiceLine(false);
    }
  } else {
    if (mcSec) mcSec.classList.add('hidden');
    if (aField) aField.style.display = '';
  }
}

let choiceCounter = 0;
function addChoiceLine(isCorrect) {
  const list = document.getElementById('mc-choices-list');
  if (!list) return;
  const id = ++choiceCounter;
  const correct = isCorrect === true;
  const row = document.createElement('div');
  row.className = 'mc-choice-row';
  row.id = 'mc-row-' + id;
  row.dataset.correct = correct ? '1' : '0';
  row.innerHTML =
    '<button type="button" class="mc-choice-toggle ' + (correct ? 'correct' : 'wrong') + '" onclick="toggleChoiceCorrect(' + id + ')" title="Click to toggle correct/wrong">' + (correct ? '\u2713' : '\u2717') + '</button>' +
    '<input type="text" class="mc-choice-input ' + (correct ? 'correct-border' : 'wrong-border') + '" id="mc-input-' + id + '" placeholder="' + (correct ? 'Correct answer' : 'Wrong answer') + '">' +
    '<button type="button" class="mc-choice-remove" onclick="removeChoiceLine(' + id + ')" title="Remove">\u00D7</button>';
  list.appendChild(row);
}

function removeChoiceLine(id) {
  const row = document.getElementById('mc-row-' + id);
  if (row) {
    row.style.opacity = '0';
    row.style.transform = 'translateX(20px)';
    row.style.transition = 'all .15s';
    setTimeout(() => row.remove(), 150);
  }
}

function toggleChoiceCorrect(id) {
  const row = document.getElementById('mc-row-' + id);
  if (!row) return;
  const isCorrect = row.dataset.correct === '1';
  const newCorrect = !isCorrect;
  row.dataset.correct = newCorrect ? '1' : '0';
  const toggle = row.querySelector('.mc-choice-toggle');
  const inp = row.querySelector('.mc-choice-input');
  if (toggle) {
    toggle.className = 'mc-choice-toggle ' + (newCorrect ? 'correct' : 'wrong');
    toggle.textContent = newCorrect ? '\u2713' : '\u2717';
  }
  if (inp) {
    inp.className = 'mc-choice-input ' + (newCorrect ? 'correct-border' : 'wrong-border');
    inp.setAttribute('placeholder', newCorrect ? 'Correct answer' : 'Wrong answer');
  }
}

/* Init default choice lines on page load */
(function initChoiceLines() {
  const init = () => {
    toggleAnswerMode();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else setTimeout(init, 0);
})();

async function loadCards() {
  try {
    const res = await fetch('/api/topics/'+TID+'/cards');
    cards = await res.json();
    renderCards();
  } catch(e) {
    document.getElementById('card-list').innerHTML = '<div class="empty">Failed to load cards.</div>';
  }
}

function renderCards() {
  document.getElementById('card-count').textContent = cards.length + ' card' + (cards.length !== 1 ? 's' : '');
  const el = document.getElementById('card-list');
  if (!cards.length) {
    el.innerHTML = '<div class="empty">No cards yet. Add your first card above.</div>';
    return;
  }
  /* Filter by search */
  const searchEl = document.getElementById('cards-search');
  const q = searchEl ? searchEl.value.trim().toLowerCase() : '';
  let filtered = cards;
  if (q) {
    filtered = cards.filter(c => c.question.toLowerCase().includes(q) || (c.answer||'').toLowerCase().includes(q));
  }
  /* Sort */
  const sortEl = document.getElementById('cards-sort');
  const sortBy = sortEl ? sortEl.value : 'default';
  const sorted = [...filtered];
  if (sortBy === 'box-asc') sorted.sort((a,b) => (a.box||1) - (b.box||1));
  else if (sortBy === 'box-desc') sorted.sort((a,b) => (b.box||1) - (a.box||1));
  else if (sortBy === 'alpha') sorted.sort((a,b) => a.question.localeCompare(b.question));
  else if (sortBy === 'fails') sorted.sort((a,b) => (b.fail_count||0) - (a.fail_count||0));

  if (!sorted.length) {
    el.innerHTML = '<div class="empty">No cards match your search.</div>';
    return;
  }
  const typeLabels = {qa:'Q & A', recall:'Free Recall'};
  el.innerHTML = sorted.map((c, i) => {
    const wo = (c.wrong_options && Array.isArray(c.wrong_options)) ? c.wrong_options : [];
    /* Build unified choices display for qa cards */
    let choicesDisplay = '';
    if (c.card_type === 'qa' && (c.answer || wo.length)) {
      let items = '';
      if (c.answer) {
        items += '<div class="fc-choice-item fc-choice-correct"><span class="fc-choice-badge correct">\u2713</span><span class="rendered-math">' + renderMathText(c.answer) + '</span></div>';
      }
      wo.forEach(w => {
        items += '<div class="fc-choice-item fc-choice-wrong"><span class="fc-choice-badge wrong">\u2717</span><span class="rendered-math">' + renderMathText(w) + '</span></div>';
      });
      choicesDisplay = '<div class="fc-a-label">Answer Choices</div><div class="fc-choices-list">' + items + '</div>';
    } else if (c.answer) {
      choicesDisplay = '<div class="fc-a-label">Answer</div><div class="fc-a rendered-math">' + renderMathText(c.answer) + '</div>';
    }
    return `
    <div class="fc" id="fc-${c.id}">
      <div class="fc-top">
        <span class="fc-type">${typeLabels[c.card_type]||c.card_type}</span>
        <div class="fc-actions">
          <button class="fc-edit" onclick="startEdit(${c.id})">Edit</button>
          <button class="fc-del" onclick="deleteCard(${c.id})">Delete</button>
        </div>
      </div>
      <div class="fc-q rendered-math">${renderMathText(c.question)}</div>
      ${choicesDisplay}
      <div class="fc-stats">
        <span title="Leitner box (1=hardest, 5=mastered)">\u2610 Box ${c.box || 1}</span>
        <span title="Successful recalls" style="color:var(--green)">\u2713 ${c.success_count || 0}</span>
        <span title="Failed recalls" style="color:var(--red)">\u2717 ${c.fail_count || 0}</span>
        ${c.last_rating ? `<span title="Last rating">${c.last_rating === 'complete' ? '\u2705' : c.last_rating === 'partial' ? '\u26a0' : '\u274c'} ${c.last_rating}</span>` : ''}
      </div>
      <div class="edit-fields">
        <select id="et-${c.id}" onchange="toggleEditAnswerMode(${c.id})">
          <option value="qa" ${c.card_type==='qa'?'selected':''}>Q & A</option>
          <option value="recall" ${c.card_type==='recall'?'selected':''}>Free Recall</option>
        </select>
        <textarea id="eq-${c.id}" rows="2" data-preview="edit-preview-${c.id}" onfocus="activeEditField='eq-${c.id}'">${escHtml(c.question)}</textarea>
        <div id="edit-a-field-${c.id}" style="${c.card_type==='qa'?'display:none':''}">
          <textarea id="ea-${c.id}" rows="2" placeholder="Answer" data-preview="edit-preview-${c.id}" onfocus="activeEditField='ea-${c.id}'">${escHtml(c.answer)}</textarea>
        </div>
        <div id="edit-mc-section-${c.id}" class="mc-choices-section${c.card_type==='qa'?'':' hidden'}" style="margin-top:0;padding-top:0;border-top:none">
          <label class="mc-choices-label">Answer Choices</label>
          <div id="edit-mc-list-${c.id}" class="mc-choices-list"></div>
          <button type="button" class="mc-add-choice-btn" onclick="addEditChoiceLine(${c.id}, false)">+ Add Choice</button>
        </div>
        <div class="math-preview" id="edit-preview-${c.id}" style="min-height:28px;margin-top:4px"><span class="math-preview-hint">Live preview...</span></div>
        <div class="edit-actions">
          <button onclick="cancelEdit(${c.id})">Cancel</button>
          <button class="btn-primary" onclick="saveEdit(${c.id})">Save</button>
        </div>
      </div>
    </div>
  `}).join('');
}

async function addCard() {
  const q = document.getElementById('new-q').value.trim();
  const ct = document.getElementById('new-type').value;
  if (!q) { toast('Enter a question or prompt', 'warn'); return; }
  let a = '';
  const wo = [];
  if (ct === 'qa') {
    /* Gather from MC choice lines */
    const rows = document.querySelectorAll('#mc-choices-list .mc-choice-row');
    rows.forEach(row => {
      const inp = row.querySelector('.mc-choice-input');
      const val = inp ? inp.value.trim() : '';
      if (!val) return;
      if (row.dataset.correct === '1') {
        if (!a) a = val;   /* first correct = main answer */
        else wo.push(val); /* extra correct treated as wrong for MC shuffle */
      } else {
        wo.push(val);
      }
    });
    if (!a) { toast('Mark at least one choice as correct', 'warn'); return; }
  } else {
    a = document.getElementById('new-a').value.trim();
  }
  const res = await fetch('/api/topics/'+TID+'/cards', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({question:q, answer:a, card_type:ct, wrong_options:wo})
  });
  if (res.ok) {
    document.getElementById('new-q').value = '';
    document.getElementById('new-a').value = '';
    const mfQ = document.getElementById('mf-new-q');
    const mfA = document.getElementById('mf-new-a');
    if (mfQ) mfQ.value = '';
    if (mfA) mfA.value = '';
    /* Reset choice lines */
    const list = document.getElementById('mc-choices-list');
    if (list) list.innerHTML = '';
    choiceCounter = 0;
    addChoiceLine(true);
    addChoiceLine(false);
    addChoiceLine(false);
    addChoiceLine(false);
    toast('Card added', 'success');
    loadCards();
  } else {
    toast('Failed to add card', 'warn');
  }
}

async function deleteCard(cid) {
  if (!confirm('Delete this card?')) return;
  const res = await fetch('/api/cards/'+cid, {method:'DELETE'});
  if (res.ok) {
    toast('Card deleted', 'info');
    loadCards();
  }
}

let activeEditField = '';
let editChoiceCounter = {};

function toggleEditAnswerMode(cid) {
  const ct = document.getElementById('et-'+cid).value;
  const mcSec = document.getElementById('edit-mc-section-'+cid);
  const aField = document.getElementById('edit-a-field-'+cid);
  if (ct === 'qa') {
    if (mcSec) mcSec.classList.remove('hidden');
    if (aField) aField.style.display = 'none';
  } else {
    if (mcSec) mcSec.classList.add('hidden');
    if (aField) aField.style.display = '';
  }
}

function addEditChoiceLine(cid, isCorrect, value) {
  const list = document.getElementById('edit-mc-list-'+cid);
  if (!list) return;
  if (!editChoiceCounter[cid]) editChoiceCounter[cid] = 0;
  const id = ++editChoiceCounter[cid];
  const uid = cid + '-' + id;
  const correct = isCorrect === true;
  const row = document.createElement('div');
  row.className = 'mc-choice-row';
  row.id = 'emc-row-' + uid;
  row.dataset.correct = correct ? '1' : '0';
  row.innerHTML =
    '<button type="button" class="mc-choice-toggle ' + (correct ? 'correct' : 'wrong') + '" onclick="toggleEditChoiceCorrect(\'' + uid + '\')" title="Click to toggle correct/wrong">' + (correct ? '\u2713' : '\u2717') + '</button>' +
    '<input type="text" class="mc-choice-input ' + (correct ? 'correct-border' : 'wrong-border') + '" id="emc-input-' + uid + '" placeholder="' + (correct ? 'Correct answer' : 'Wrong answer') + '" value="' + escHtml(value || '') + '">' +
    '<button type="button" class="mc-choice-remove" onclick="removeEditChoiceLine(\'' + uid + '\')" title="Remove">\u00D7</button>';
  list.appendChild(row);
}

function removeEditChoiceLine(uid) {
  const row = document.getElementById('emc-row-' + uid);
  if (row) {
    row.style.opacity = '0';
    row.style.transform = 'translateX(20px)';
    row.style.transition = 'all .15s';
    setTimeout(() => row.remove(), 150);
  }
}

function toggleEditChoiceCorrect(uid) {
  const row = document.getElementById('emc-row-' + uid);
  if (!row) return;
  const isCorrect = row.dataset.correct === '1';
  const newCorrect = !isCorrect;
  row.dataset.correct = newCorrect ? '1' : '0';
  const toggle = row.querySelector('.mc-choice-toggle');
  const input = row.querySelector('.mc-choice-input');
  if (toggle) {
    toggle.className = 'mc-choice-toggle ' + (newCorrect ? 'correct' : 'wrong');
    toggle.textContent = newCorrect ? '\u2713' : '\u2717';
  }
  if (input) {
    input.className = 'mc-choice-input ' + (newCorrect ? 'correct-border' : 'wrong-border');
    input.placeholder = newCorrect ? 'Correct answer' : 'Wrong answer';
  }
}

function startEdit(cid) {
  document.querySelectorAll('.fc.editing').forEach(el => el.classList.remove('editing'));
  const el = document.getElementById('fc-'+cid);
  if (el) {
    el.classList.add('editing');
    activeEditField = 'eq-'+cid;
    /* Populate edit MC choice lines */
    const card = cards.find(c => c.id === cid);
    if (card && card.card_type === 'qa') {
      const list = document.getElementById('edit-mc-list-'+cid);
      if (list && !list.children.length) {
        editChoiceCounter[cid] = 0;
        if (card.answer) addEditChoiceLine(cid, true, card.answer);
        const wo = (card.wrong_options && Array.isArray(card.wrong_options)) ? card.wrong_options : [];
        wo.forEach(w => addEditChoiceLine(cid, false, w));
        /* Ensure at least one empty wrong line */
        if (!wo.length) addEditChoiceLine(cid, false, '');
      }
    }
  }
}

function cancelEdit(cid) {
  const el = document.getElementById('fc-'+cid);
  if (el) el.classList.remove('editing');
  loadCards();
}

async function saveEdit(cid) {
  const q = document.getElementById('eq-'+cid).value.trim();
  const ct = document.getElementById('et-'+cid).value;
  if (!q) { toast('Question cannot be empty', 'warn'); return; }
  let a = '';
  const wo = [];
  if (ct === 'qa') {
    /* Gather from edit MC choice lines */
    const rows = document.querySelectorAll('#edit-mc-list-'+cid+' .mc-choice-row');
    rows.forEach(row => {
      const input = row.querySelector('.mc-choice-input');
      const val = input ? input.value.trim() : '';
      if (!val) return;
      if (row.dataset.correct === '1') {
        if (!a) a = val;
        else wo.push(val);
      } else {
        wo.push(val);
      }
    });
    if (!a) { toast('Mark at least one choice as correct', 'warn'); return; }
  } else {
    a = document.getElementById('ea-'+cid).value.trim();
  }
  const res = await fetch('/api/cards/'+cid, {
    method:'PUT',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({question:q, answer:a, card_type:ct, wrong_options:wo})
  });
  if (res.ok) {
    toast('Card updated', 'success');
    loadCards();
  } else {
    toast('Failed to update', 'warn');
  }
}

/* Keyboard shortcut: Ctrl+Enter to add card */
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    const path = e.composedPath ? e.composedPath() : [];
    const inNewCard = path.some(el => el.id === 'new-q' || el.id === 'new-a' || el.id === 'mf-new-q' || el.id === 'mf-new-a');
    if (inNewCard) { addCard(); return; }
    const inBulk = path.some(el => el.id === 'bulk-text' || el.id === 'mf-bulk');
    if (inBulk) bulkImport();
  }
});

/* Bulk import: live preview counter */
document.addEventListener('input', e => {
  if (e.target && (e.target.id === 'bulk-text' || e.target.id === 'bulk-type')) {
    const raw = document.getElementById('bulk-text').value.trim();
    const dt = document.getElementById('bulk-type').value;
    const typeNames = {qa:'Q&A', recall:'Recall'};
    const blocks = raw ? raw.split(/\n\s*\n/).filter(b => b.trim()) : [];
    if (!blocks.length) {
      document.getElementById('bulk-preview').textContent = '';
      return;
    }
    const prefixRe = /^\[(qa|recall)\]\s*/i;
    let counts = {qa:0, recall:0};
    blocks.forEach(b => {
      const m = b.trim().match(prefixRe);
      const t = m ? m[1].toLowerCase() : dt;
      counts[t] = (counts[t]||0) + 1;
    });
    const parts = [];
    if (counts.qa) parts.push(counts.qa + ' Q&A');
    if (counts.recall) parts.push(counts.recall + ' Recall');
    document.getElementById('bulk-preview').textContent = blocks.length + ' card(s): ' + parts.join(', ');
  }
});

async function bulkImport() {
  const text = document.getElementById('bulk-text').value.trim();
  if (!text) { toast('Paste some cards first', 'warn'); return; }
  const defaultType = document.getElementById('bulk-type').value;
  const res = await fetch('/api/topics/'+TID+'/cards/bulk', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text, default_type: defaultType})
  });
  const data = await res.json();
  if (res.ok) {
    document.getElementById('bulk-text').value = '';
    document.getElementById('bulk-preview').textContent = '';
    toast(data.added + ' card(s) imported', 'success');
    loadCards();
  } else {
    toast(data.error || 'Import failed', 'warn');
  }
}

loadCards();
toggleAnswerMode();
</script>
</body>
</html>"""


# ── Entry Point ───────────────────────────────────────────────────────────

LOGO_PATH = Path(__file__).parent / "logo.png"
STATIC_DIR = Path(__file__).parent / "static"

@app.route("/logo.png")
def serve_logo():
    return send_file(LOGO_PATH, mimetype="image/png")


@app.route("/static/<path:filename>")
def serve_static(filename):
    fpath = STATIC_DIR / filename
    if fpath.exists():
        return send_file(fpath)
    return "Not found", 404


STATS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Study Statistics</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0f172a; }
</style>
</head>
<body>
<div id="stats-root"></div>
<script src="/static/stats-bundle.js"></script>
</body>
</html>"""


@app.route("/statistics")
def statistics_page():
    return STATS_HTML


@app.route("/cards/<int:tid>")
def cards_page(tid):
    with get_db() as db:
        row = db.execute("SELECT name FROM topics WHERE id=?", (tid,)).fetchone()
    if not row:
        return "Topic not found", 404
    return render_template_string(CARDS_HTML, topic_id=tid, topic_name=row["name"])


# ── Settings API ──────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    """Return current settings (API key masked for display)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    masked = ""
    if key:
        masked = key[:8] + "•" * (len(key) - 12) + key[-4:] if len(key) > 12 else "••••"
    return jsonify({
        "has_anthropic_lib": _HAS_ANTHROPIC,
        "ai_connected": ANTHROPIC_CLIENT is not None,
        "api_key_masked": masked,
    })


@app.route("/api/settings/api-key", methods=["POST"])
def set_api_key():
    """Set or update the Anthropic API key at runtime and persist to .env."""
    data = request.json or {}
    key = data.get("api_key", "").strip()

    # Allow clearing the key
    os.environ["ANTHROPIC_API_KEY"] = key
    _init_anthropic(key)

    # Persist to .env so it survives restarts
    env_path = Path(__file__).parent / ".env"
    _update_env_file(env_path, "ANTHROPIC_API_KEY", key)

    masked = ""
    if key:
        masked = key[:8] + "•" * (len(key) - 12) + key[-4:] if len(key) > 12 else "••••"
    return jsonify({
        "ok": True,
        "ai_connected": ANTHROPIC_CLIENT is not None,
        "api_key_masked": masked,
    })


def _update_env_file(path: Path, var: str, value: str):
    """Write or update a single VAR=value line in a .env file."""
    lines = []
    found = False
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if line.startswith(f"{var}=") or line.startswith(f"{var} ="):
                lines[i] = f"{var}={value}"
                found = True
                break
    if not found:
        lines.append(f"{var}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    # When launched via pythonw.exe (no console), stdout/stderr are None.
    # Redirect to devnull so print() and logging don't crash.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    init_db()
    check_notifications()
    t = threading.Thread(target=_reminder_loop, daemon=True)
    t.start()

    # Run Flask in a background daemon thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()

    # Open a native desktop window
    window = webview.create_window(
        "Study Tracker",
        f"http://127.0.0.1:{PORT}",
        width=1280,
        height=860,
        min_size=(800, 600),
    )
    webview.start()   # blocks until window is closed
    sys.exit(0)
