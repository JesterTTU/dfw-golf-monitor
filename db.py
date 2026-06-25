"""
db.py — SQLite persistence layer for the tee-time monitor.

Tables
------
tee_times    : every tee-time slot fetched from TeeItUp
alerts_sent  : de-duplication log so we don't spam the same slot

Usage
-----
    from db import init_db, insert_tee_time, get_rolling_average, was_alert_sent_recently
    init_db()   # call once at startup
"""

import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional

# Default DB path sits next to this file; override via DB_PATH env var.
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "tee_times.db"))


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # dict-like rows
    conn.execute("PRAGMA journal_mode=WAL")  # safe concurrent reads
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CREATE_TEE_TIMES = """
CREATE TABLE IF NOT EXISTS tee_times (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id         INTEGER NOT NULL,
    course_name       TEXT    NOT NULL,
    tee_date          TEXT    NOT NULL,   -- YYYY-MM-DD
    tee_time          TEXT    NOT NULL,   -- HH:MM  (24-hour)
    price             REAL,
    holes             INTEGER,
    players_available INTEGER,
    fetched_at        TEXT    NOT NULL    -- ISO-8601 UTC timestamp
);
"""

CREATE_ALERTS_SENT = """
CREATE TABLE IF NOT EXISTS alerts_sent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL,
    tee_date    TEXT    NOT NULL,
    tee_time    TEXT    NOT NULL,
    price       REAL,
    alert_type  TEXT    NOT NULL,   -- 'threshold' | 'below_average'
    sent_at     TEXT    NOT NULL    -- ISO-8601 UTC timestamp
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tt_course_date ON tee_times (course_id, tee_date);",
    "CREATE INDEX IF NOT EXISTS idx_tt_fetched    ON tee_times (fetched_at);",
    "CREATE INDEX IF NOT EXISTS idx_as_lookup     ON alerts_sent (course_id, tee_date, tee_time, sent_at);",
]


def init_db() -> None:
    """Create tables and indexes if they don't exist yet."""
    with _connect() as conn:
        conn.execute(CREATE_TEE_TIMES)
        conn.execute(CREATE_ALERTS_SENT)
        for idx in CREATE_INDEXES:
            conn.execute(idx)
    print(f"[db] Initialized database at {DB_PATH}")


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def insert_tee_time(
    course_id: int,
    course_name: str,
    tee_date: str,
    tee_time: str,
    price: Optional[float],
    holes: Optional[int],
    players_available: Optional[int],
) -> int:
    """
    Insert a single tee-time observation.
    Returns the new row id.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO tee_times
                (course_id, course_name, tee_date, tee_time,
                 price, holes, players_available, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (course_id, course_name, tee_date, tee_time,
             price, holes, players_available, fetched_at),
        )
        return cur.lastrowid


def record_alert_sent(
    course_id: int,
    tee_date: str,
    tee_time: str,
    price: Optional[float],
    alert_type: str,          # 'threshold' or 'below_average'
) -> None:
    """Log that an alert was fired so we can suppress duplicates."""
    sent_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO alerts_sent
                (course_id, tee_date, tee_time, price, alert_type, sent_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (course_id, tee_date, tee_time, price, alert_type, sent_at),
        )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_rolling_average(
    course_id: int,
    tee_time_hour: int,      # 0-23  — we bucket by hour, not exact minute
    day_of_week: int,        # 0=Monday … 6=Sunday  (Python datetime.weekday())
    days: int = 7,
) -> Optional[float]:
    """
    Return the average price for tee times on the same course, same hour-of-day,
    and same day-of-week observed over the last *days* days.

    Returns None if there are fewer than 3 observations (not enough data).

    SQLite's strftime('%w', date) returns 0=Sunday … 6=Saturday.
    Python's weekday() returns 0=Monday … 6=Sunday.
    We convert Python → SQLite format: sqlite_dow = (python_dow + 1) % 7
    """
    sqlite_dow = (day_of_week + 1) % 7   # convert Python to SQLite convention

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt, AVG(price) AS avg_price
            FROM   tee_times
            WHERE  course_id = ?
              AND  price IS NOT NULL
              AND  price > 0
              AND  CAST(SUBSTR(tee_time, 1, 2) AS INTEGER) = ?
              AND  CAST(strftime('%w', tee_date) AS INTEGER) = ?
              AND  fetched_at >= datetime('now', ? || ' days')
            """,
            (course_id, tee_time_hour, sqlite_dow, f"-{days}"),
        ).fetchone()

    if row and row["cnt"] >= 3:
        return round(row["avg_price"], 2)
    return None


def was_alert_sent_recently(
    course_id: int,
    tee_date: str,
    tee_time: str,
    hours: int = 6,
) -> bool:
    """
    Return True if any alert for this exact course+date+time slot was sent
    within the last *hours* hours.  Prevents duplicate Discord pings.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM   alerts_sent
            WHERE  course_id = ?
              AND  tee_date  = ?
              AND  tee_time  = ?
              AND  sent_at  >= datetime('now', ? || ' hours')
            """,
            (course_id, tee_date, tee_time, f"-{hours}"),
        ).fetchone()
    return bool(row and row["cnt"] > 0)


# ---------------------------------------------------------------------------
# Query helpers used by export_dashboard.py
# ---------------------------------------------------------------------------

def get_recent_tee_times(days_back: int = 7) -> list[dict]:
    """
    Return all tee-time rows fetched in the last *days_back* days,
    most-recent fetch first.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT  tt.*,
                    CASE WHEN a.course_id IS NOT NULL THEN 1 ELSE 0 END AS flagged
            FROM    tee_times tt
            LEFT JOIN (
                SELECT DISTINCT course_id, tee_date, tee_time
                FROM   alerts_sent
            ) a USING (course_id, tee_date, tee_time)
            WHERE   tt.fetched_at >= datetime('now', ? || ' days')
            ORDER   BY tt.fetched_at DESC
            """,
            (f"-{days_back}",),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_fetch_time() -> Optional[str]:
    """Return the most recent fetched_at timestamp across all rows."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(fetched_at) AS latest FROM tee_times"
        ).fetchone()
    return row["latest"] if row else None


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print("[db] Schema ready.  DB path:", DB_PATH)
