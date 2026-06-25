"""
export_dashboard.py — Read SQLite and write dashboard_data.json for dashboard.html.

Run:
    python export_dashboard.py
    python export_dashboard.py --days 14   # include the last 14 days of data

The output file (dashboard_data.json) is consumed by dashboard.html
which reads it via fetch() or XMLHttpRequest when served locally.

GitHub Actions uploads dashboard_data.json as a workflow artifact so you
can download it and open it alongside dashboard.html in your browser.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from db import init_db, get_recent_tee_times, get_latest_fetch_time


def build_export(days_back: int) -> dict:
    """Pull data from SQLite and build the JSON payload."""
    rows = get_recent_tee_times(days_back=days_back)

    # Load thresholds from config.json so the dashboard can display them
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    thresholds = {}
    courses = []
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        thresholds = cfg.get("alert_thresholds", {})
        courses    = cfg.get("courses", [])
    except (FileNotFoundError, json.JSONDecodeError):
        pass  # dashboard will just not show threshold labels

    latest = get_latest_fetch_time()

    return {
        "exported_at":  datetime.now(timezone.utc).isoformat(),
        "last_fetched": latest,
        "thresholds":   thresholds,
        "courses":      courses,
        "tee_times":    rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export tee-time data to JSON for the dashboard.")
    parser.add_argument("--days", type=int, default=7, help="Days of history to include (default 7)")
    parser.add_argument("--output", default=None, help="Output file path (default: dashboard_data.json)")
    args = parser.parse_args()

    init_db()

    payload = build_export(days_back=args.days)

    out_path = args.output or os.path.join(os.path.dirname(__file__), "dashboard_data.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    print(
        f"[export] Wrote {len(payload['tee_times'])} rows → {out_path}  "
        f"(last fetched: {payload['last_fetched']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
