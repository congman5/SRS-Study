#!/usr/bin/env python3
"""
Insert test topics into topics.db simulating 0–10 reviews
done at the correct spaced-repetition schedule.
"""
import json, math, sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "topics.db"

# Same constants as app.py
THRESHOLD = 0.80
A_INIT    = 0.20
K_INIT    = 0.30
A_GAIN    = 0.12
K_FACTOR  = 0.50

def update_curve(a, k):
    return min(0.95, a + A_GAIN * (1.0 - a)), max(0.005, k * K_FACTOR)

def days_to_threshold(a, k):
    if a >= THRESHOLD:
        return 365.0
    inner = (THRESHOLD - a) / (1.0 - a)
    if inner <= 0.0:
        return 365.0
    return max(0.5, -math.log(inner) / k)

conn = sqlite3.connect(DB_PATH)
conn.execute("""CREATE TABLE IF NOT EXISTS topics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    learned_date TEXT    NOT NULL,
    a            REAL    NOT NULL DEFAULT 0.20,
    k            REAL    NOT NULL DEFAULT 0.30,
    last_review  TEXT,
    next_review  TEXT    NOT NULL,
    review_count INTEGER NOT NULL DEFAULT 0,
    history      TEXT    NOT NULL DEFAULT '[]'
)""")

# Start learning date far enough back so all reviews fit in the past
learn_start = date.today() - timedelta(days=200)

for n_reviews in range(11):  # 0 through 10 reviews
    name = f"Topic with {n_reviews} review{'s' if n_reviews != 1 else ''}"
    a, k = A_INIT, K_INIT
    learned = learn_start
    history = []
    last_review = None
    cursor = learned

    for rev in range(n_reviews):
        interval = round(days_to_threshold(a, k))
        cursor = cursor + timedelta(days=interval)
        a, k = update_curve(a, k)
        history.append(cursor.isoformat())
        last_review = cursor.isoformat()

    # Schedule next review from last review (or learned date)
    next_interval = round(days_to_threshold(a, k))
    ref = cursor if last_review else learned
    next_review = (ref + timedelta(days=next_interval)).isoformat()

    conn.execute(
        "INSERT INTO topics (name, learned_date, a, k, last_review, next_review, review_count, history) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (name, learned.isoformat(), round(a, 6), round(k, 6),
         last_review, next_review, n_reviews, json.dumps(history)),
    )
    print(f"  {name:40s}  a={a:.4f}  k={k:.6f}  interval={next_interval:>4d}d  next={next_review}")

conn.commit()
conn.close()
print(f"\nDone — inserted 11 test topics into {DB_PATH}")
