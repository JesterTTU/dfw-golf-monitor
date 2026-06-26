"""
web.py — FastAPI web dashboard for the DFW Golf Monitor (Saturn deployment).

Endpoints:
  GET /               → 3-tab SPA  (Dashboard / Trends / Health)
  GET /api/data       → tee-time JSON consumed by Dashboard tab
  GET /api/health     → GitHub Actions status + DB stats + sync info
  GET /api/trends     → price-decay curves, DOW heatmap, best booking windows
  POST /api/sync      → manually pull the latest price_history.csv from GitHub

Background task: syncs price_history.csv from GitHub every 30 min and
loads new rows into the local SQLite DB (INSERT OR IGNORE on id).
"""

import asyncio
import csv
import io
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from db import init_db, DB_PATH, get_latest_fetch_time
from export_dashboard import build_export

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
REPO           = "JesterTTU/dfw-golf-monitor"
CSV_URL        = f"https://raw.githubusercontent.com/{REPO}/main/price_history.csv"
SYNC_INTERVAL  = int(os.environ.get("SYNC_INTERVAL_MINUTES", "30")) * 60   # seconds

# ---------------------------------------------------------------------------
# Sync state  (module-level, single process)
# ---------------------------------------------------------------------------
_last_sync_at:    Optional[datetime] = None
_last_sync_rows:  int                = 0
_last_sync_new:   int                = 0
_sync_error:      Optional[str]      = None
_sync_lock                           = asyncio.Lock()


# ---------------------------------------------------------------------------
# CSV → SQLite sync
# ---------------------------------------------------------------------------

async def sync_from_github() -> dict:
    global _last_sync_at, _last_sync_rows, _last_sync_new, _sync_error

    async with _sync_lock:
        try:
            hdrs = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(CSV_URL, headers=hdrs)
                resp.raise_for_status()

            reader = csv.DictReader(io.StringIO(resp.text))
            rows   = list(reader)

            conn    = sqlite3.connect(DB_PATH)
            new     = 0
            skipped = 0
            for row in rows:
                try:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO tee_times
                            (id, course_id, course_name, tee_date, tee_time,
                             price, holes, rate_name, is_hot_deal, days_ahead,
                             players_available, booked_players, occupancy_pct, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(row["id"]),
                            int(row["course_id"]),
                            row["course_name"],
                            row["tee_date"],
                            row["tee_time"],
                            float(row["price"])           if row.get("price")            else None,
                            int(row["holes"])             if row.get("holes")            else None,
                            row.get("rate_name")          or None,
                            int(row.get("is_hot_deal", 0) or 0),
                            int(row["days_ahead"])        if row.get("days_ahead")       else None,
                            int(row["players_available"]) if row.get("players_available") else None,
                            int(row["booked_players"])    if row.get("booked_players")   else None,
                            float(row["occupancy_pct"])   if row.get("occupancy_pct")    else None,
                            row["fetched_at"],
                        ),
                    )
                    new     += cur.rowcount
                    skipped += 1 - cur.rowcount
                except (ValueError, KeyError) as e:
                    logger.warning("[sync] skipping row id=%s: %s", row.get("id"), e)

            conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM tee_times").fetchone()[0]
            conn.close()

            _last_sync_at    = datetime.now(timezone.utc)
            _last_sync_rows  = len(rows)
            _last_sync_new   = new
            _sync_error      = None
            logger.info("[sync] CSV rows=%d  new=%d  skipped=%d  total_db=%d", len(rows), new, skipped, total)
            return {"rows_in_csv": len(rows), "new": new, "skipped": skipped, "total_in_db": total}

        except Exception as exc:
            _sync_error = str(exc)
            logger.error("[sync] failed: %s", exc)
            raise


async def _background_sync():
    """Periodic background sync loop."""
    while True:
        try:
            await sync_from_github()
        except Exception:
            pass  # already logged inside sync_from_github
        await asyncio.sleep(SYNC_INTERVAL)


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Initial sync
    try:
        await sync_from_github()
    except Exception as e:
        logger.warning("[startup] Initial sync failed (proceeding anyway): %s", e)
    # Start background loop
    task = asyncio.create_task(_background_sync())
    yield
    task.cancel()


app = FastAPI(title="DFW Golf Monitor", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "saturn_index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>saturn_index.html not found</h1>", status_code=500)


@app.get("/api/data")
async def api_data(days: int = 30):
    """Dashboard data: last N days of tee times (same shape as export_dashboard.py)."""
    try:
        payload = build_export(days_back=days)
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/health")
async def api_health():
    """Monitor health: GitHub Actions status + DB stats + last sync info."""
    # --- GitHub Actions last runs ---
    runs = []
    try:
        hdrs = {
            "Accept": "application/vnd.github.v3+json",
            **({"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}),
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{REPO}/actions/runs?per_page=5",
                headers=hdrs,
            )
            resp.raise_for_status()
            for r in resp.json().get("workflow_runs", [])[:5]:
                runs.append({
                    "id":         r["id"],
                    "status":     r["status"],
                    "conclusion": r.get("conclusion"),
                    "created_at": r["created_at"],
                    "updated_at": r.get("updated_at"),
                    "url":        r["html_url"],
                })
    except Exception as exc:
        runs = [{"error": str(exc)}]

    # --- DB stats ---
    db_stats = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute(
            "SELECT COUNT(*) as total, MIN(fetched_at) as first, MAX(fetched_at) as last FROM tee_times"
        ).fetchone()
        hot  = conn.execute("SELECT COUNT(*) FROM tee_times WHERE is_hot_deal=1").fetchone()[0]
        conn.close()
        db_stats = {
            "total_rows": row[0],
            "first_fetch": row[1],
            "last_fetch":  row[2],
            "hot_deals":   hot,
        }
    except Exception as exc:
        db_stats = {"error": str(exc)}

    return JSONResponse({
        "server_time":    datetime.now(timezone.utc).isoformat(),
        "last_sync_at":   _last_sync_at.isoformat() if _last_sync_at else None,
        "last_sync_rows": _last_sync_rows,
        "last_sync_new":  _last_sync_new,
        "sync_error":     _sync_error,
        "sync_interval_minutes": SYNC_INTERVAL // 60,
        "github_runs":    runs,
        "db":             db_stats,
    })


@app.get("/api/trends")
async def api_trends():
    """Price decay curves, DOW heatmap, best booking windows — straight SQL."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # 1. Price decay by days-ahead bucket
        decay = conn.execute("""
            SELECT
                course_name,
                CASE
                    WHEN days_ahead = 0                  THEN '0 Same Day'
                    WHEN days_ahead = 1                  THEN '1 Day Out'
                    WHEN days_ahead BETWEEN 2 AND 3      THEN '2-3 Days'
                    WHEN days_ahead BETWEEN 4 AND 7      THEN '4-7 Days'
                    WHEN days_ahead BETWEEN 8 AND 14     THEN '8-14 Days'
                    ELSE '15+ Days'
                END AS bucket,
                ROUND(AVG(price), 2) AS avg_price,
                ROUND(MIN(price), 2) AS min_price,
                ROUND(MAX(price), 2) AS max_price,
                COUNT(*)             AS observations,
                SUM(is_hot_deal)     AS hot_deals
            FROM tee_times
            WHERE price > 0 AND holes = 18 AND days_ahead IS NOT NULL
            GROUP BY course_name, bucket
            ORDER BY course_name, MIN(days_ahead)
        """).fetchall()

        # 2. DOW × time-block heatmap (avg price)
        heatmap = conn.execute("""
            SELECT
                course_name,
                CAST(strftime('%w', tee_date) AS INTEGER) AS dow,
                CASE
                    WHEN CAST(SUBSTR(tee_time,1,2) AS INTEGER) < 11 THEN 'Morning (6-11)'
                    WHEN CAST(SUBSTR(tee_time,1,2) AS INTEGER) < 14 THEN 'Midday (11-2)'
                    WHEN CAST(SUBSTR(tee_time,1,2) AS INTEGER) < 16 THEN 'Afternoon (2-4)'
                    WHEN CAST(SUBSTR(tee_time,1,2) AS INTEGER) < 18 THEN 'Twilight (4-6)'
                    ELSE 'Evening (6+)'
                END AS time_block,
                ROUND(AVG(price), 2) AS avg_price,
                COUNT(*)             AS observations
            FROM tee_times
            WHERE price > 0 AND holes = 18
            GROUP BY course_name, dow, time_block
            ORDER BY course_name, dow, MIN(CAST(SUBSTR(tee_time,1,2) AS INTEGER))
        """).fetchall()

        # 3. Best booking windows summary
        windows = conn.execute("""
            SELECT
                course_name,
                CASE
                    WHEN days_ahead = 0             THEN '0 Same Day'
                    WHEN days_ahead BETWEEN 1 AND 2 THEN '1-2 Days Out'
                    WHEN days_ahead BETWEEN 3 AND 4 THEN '3-4 Days Out'
                    WHEN days_ahead BETWEEN 5 AND 7 THEN '5-7 Days Out'
                END AS window,
                ROUND(AVG(price), 2) AS avg_price,
                ROUND(MIN(price), 2) AS min_price,
                COUNT(*)             AS observations,
                ROUND(100.0 * SUM(is_hot_deal) / COUNT(*), 1) AS hot_deal_pct
            FROM tee_times
            WHERE price > 0 AND holes = 18 AND days_ahead IS NOT NULL AND days_ahead <= 7
            GROUP BY course_name, window
            ORDER BY course_name, avg_price
        """).fetchall()

        # 4. Recent price trend: daily avg per course over last 14 days
        daily = conn.execute("""
            SELECT
                course_name,
                DATE(fetched_at) AS fetch_date,
                ROUND(AVG(price), 2) AS avg_price,
                COUNT(*) AS observations
            FROM tee_times
            WHERE price > 0 AND holes = 18
              AND fetched_at >= datetime('now', '-14 days')
            GROUP BY course_name, fetch_date
            ORDER BY course_name, fetch_date
        """).fetchall()

        conn.close()

        DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

        return JSONResponse({
            "decay":   [dict(r) for r in decay],
            "heatmap": [
                {**dict(r), "dow_name": DOW_NAMES[r["dow"]]}
                for r in heatmap
            ],
            "windows": [dict(r) for r in windows],
            "daily":   [dict(r) for r in daily],
        })

    except Exception as exc:
        logger.exception("trends error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/sync")
async def api_sync():
    """Manually trigger a data sync from GitHub."""
    try:
        result = await sync_from_github()
        return JSONResponse({"ok": True, **result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "web:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
        reload=False,
    )
