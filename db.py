"""
db.py — SQLite persistence layer for the tee-time monitor.

Tables
------
tee_times    : every tee-time slot observation fetched from TeeItUp
               (ALL slots, not just deals — this is the price history)
alerts_sent  : de-duplication log so we don't spam the same slot

Key analysis fields added:
  days_ahead      : tee_date - fetch_date  → price-decay curve
  is_hot_deal     : TeeItUp's own flag (independent of our thresholds)
  rate_name       : "18 Holes", "Hot Deal", "Prepaid - 18 Holes", etc.
  booked_players  : players already booked in the slot
  max_players     : total capacity of the slot
  occupancy_pct   : booked_players / max_players * 100 (NULL if unknown)
"""

import sqlite3
import os
from datetime import datetime, date, timezone
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "tee_times.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CREATE_TEE_TIMES = """
CREATE TABLE IF NOT EXISTS tee_times (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id       INTEGER NOT NULL,
    course_name     TEXT    NOT NULL,
    tee_date        TEXT    NOT NULL,        -- YYYY-MM-DD (local Arlington date)
    tee_time        TEXT    NOT NULL,        -- HH:MM 24-hour (local Arlington time)
    price           REAL,                   -- dollars (already divided from cents)
    holes           INTEGER,
    rate_name       TEXT,                   -- "18 Holes", "Hot Deal", "Prepaid - 18 Holes"
    is_hot_deal     INTEGER DEFAULT 0,      -- 1 if TeeItUp showAsHotDeal=true
    players_available INTEGER,              -- max_players (capacity of slot)
    booked_players  INTEGER,               -- players already booked
    occupancy_pct   REAL,                  -- booked/max * 100, NULL if unknown
    days_ahead      INTEGER,               -- tee_date - fetch_date (days in advance)
    fetched_at      TEXT    NOT NULL       -- ISO-8601 UTC timestamp
);
"""

CREATE_ALERTS_SENT = """
CREATE TABLE IF NOT EXISTS alerts_sent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL,
    tee_date    TEXT    NOT NULL,
    tee_time    TEXT    NOT NULL,
    price       REAL,
    alert_type  TEXT    NOT NULL,
    sent_at     TEXT    NOT NULL
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tt_course_date    ON tee_times (course_id, tee_date);",
    "CREATE INDEX IF NOT EXISTS idx_tt_days_ahead     ON tee_times (course_id, days_ahead);",
    "CREATE INDEX IF NOT EXISTS idx_tt_fetched        ON tee_times (fetched_at);",
    "CREATE INDEX IF NOT EXISTS idx_tt_analysis       ON tee_times (course_id, tee_date, tee_time, days_ahead);",
    "CREATE INDEX IF NOT EXISTS idx_as_lookup         ON alerts_sent (course_id, tee_date, tee_time, sent_at);",
]

# Columns added after initial release — safe to re-run, ignored if already exist
MIGRATIONS = [
    ("tee_times", "rate_name",        "TEXT"),
    ("tee_times", "is_hot_deal",      "INTEGER DEFAULT 0"),
    ("tee_times", "booked_players",   "INTEGER"),
    ("tee_times", "occupancy_pct",    "REAL"),
    ("tee_times", "days_ahead",       "INTEGER"),
]


def init_db() -> None:
    with _connect() as conn:
        conn.execute(CREATE_TEE_TIMES)
        conn.execute(CREATE_ALERTS_SENT)
        for idx in CREATE_INDEXES:
            conn.execute(idx)
        # Apply migrations — ignore errors for columns that already exist
        for table, col, col_type in MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass   # column already exists
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
    rate_name: Optional[str] = None,
    is_hot_deal: bool = False,
    booked_players: Optional[int] = None,
) -> int:
    """Insert one tee-time observation. Returns the new row id."""
    fetched_at = datetime.now(timezone.utc).isoformat()

    # days_ahead: how far in the future is this tee time from today's fetch
    try:
        days_ahead = (date.fromisoformat(tee_date) - date.today()).days
    except (ValueError, TypeError):
        days_ahead = None

    # occupancy
    occupancy_pct = None
    if booked_players is not None and players_available and players_available > 0:
        occupancy_pct = round(booked_players / players_available * 100, 1)

    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO tee_times
                (course_id, course_name, tee_date, tee_time,
                 price, holes, rate_name, is_hot_deal,
                 players_available, booked_players, occupancy_pct,
                 days_ahead, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                course_id, course_name, tee_date, tee_time,
                price, holes, rate_name, int(is_hot_deal),
                players_available, booked_players, occupancy_pct,
                days_ahead, fetched_at,
            ),
        )
        return cur.lastrowid


def record_alert_sent(
    course_id: int,
    tee_date: str,
    tee_time: str,
    price: Optional[float],
    alert_type: str,
) -> None:
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
    tee_time_hour: int,
    day_of_week: int,
    days: int = 7,
) -> Optional[float]:
    """
    Average price for same course/hour/day-of-week over the last N days.
    Returns None if fewer than 3 observations.
    """
    sqlite_dow = (day_of_week + 1) % 7
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt, AVG(price) AS avg_price
            FROM   tee_times
            WHERE  course_id = ?
              AND  price IS NOT NULL AND price > 0
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
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM   alerts_sent
            WHERE  course_id = ? AND tee_date = ? AND tee_time = ?
              AND  sent_at >= datetime('now', ? || ' hours')
            """,
            (course_id, tee_date, tee_time, f"-{hours}"),
        ).fetchone()
    return bool(row and row["cnt"] > 0)


def get_recent_tee_times(days_back: int = 7) -> list[dict]:
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


def get_all_tee_times() -> list[dict]:
    """Return every row in the database — used by export_csv.py."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tee_times ORDER BY fetched_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_fetch_time() -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(fetched_at) AS latest FROM tee_times"
        ).fetchone()
    return row["latest"] if row else None


if __name__ == "__main__":
    init_db()
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM tee_times").fetchone()[0]
    print(f"[db] Schema ready. Rows: {count}. DB: {DB_PATH}")
