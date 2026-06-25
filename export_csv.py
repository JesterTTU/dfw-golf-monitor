#!/usr/bin/env python3
"""
export_csv.py — Export the full tee-time price history to CSV.

The GitHub Actions workflow commits price_history.csv back to the repo
after every run, so data accumulates forever even if the SQLite cache
is lost.  The CSV is also easy to open in Excel or import into pandas.

Usage:
    python export_csv.py                   # writes price_history.csv
    python export_csv.py --out prices.csv  # custom output path
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

# Allow running from any directory
sys.path.insert(0, os.path.dirname(__file__))
from db import get_all_tee_times, get_latest_fetch_time

COLUMNS = [
    "id", "course_id", "course_name",
    "tee_date", "tee_time",
    "price", "holes", "rate_name",
    "is_hot_deal", "days_ahead",
    "players_available", "booked_players", "occupancy_pct",
    "fetched_at",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="price_history.csv")
    args = parser.parse_args()

    rows = get_all_tee_times()
    if not rows:
        print("[export_csv] No data yet — skipping CSV export.")
        return

    out_path = os.path.join(os.path.dirname(__file__), args.out)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in COLUMNS})

    latest = get_latest_fetch_time() or "unknown"
    print(f"[export_csv] Wrote {len(rows)} rows → {out_path}")
    print(f"[export_csv] Latest observation: {latest}")


if __name__ == "__main__":
    main()
