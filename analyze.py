#!/usr/bin/env python3
"""
analyze.py — Price pattern analysis for the DFW golf tee-time monitor.

Usage:
    python analyze.py                     # full report to stdout
    python analyze.py --course 1315       # Tierra Verde only
    python analyze.py --csv               # also write analysis_report.csv
    python analyze.py --days 30           # look back 30 days (default: all)

What it produces:
  1. Price-decay curves  — how price changes as a tee time gets closer
     (days_ahead=7 vs 3 vs 1 vs 0)
  2. Day-of-week heatmap — avg price by day + time block
  3. Hot-deal trigger patterns — when do Hot Deals appear?
  4. Price predictions — for each upcoming slot, estimated price in 1/3/5 days
  5. Best booking windows — statistically cheapest time to book

Requires at least a few days of data to be meaningful.
"""

import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "tee_times.db"))

DAY_NAMES  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TIME_BLOCKS = [
    ("Morning",   6, 11),   # 6 AM – 10:59 AM
    ("Midday",   11, 14),   # 11 AM – 1:59 PM
    ("Afternoon",14, 16),   # 2 PM – 3:59 PM
    ("Twilight", 16, 18),   # 4 PM – 5:59 PM
    ("Evening",  18, 22),   # 6 PM+
]

COURSE_NAMES = {1315: "Tierra Verde", 1319: "Texas Rangers"}


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_rows(course_id: Optional[int] = None, days_back: Optional[int] = None):
    filters = ["price IS NOT NULL", "price > 0", "holes = 18"]
    params  = []
    if course_id:
        filters.append("course_id = ?")
        params.append(course_id)
    if days_back:
        filters.append("fetched_at >= datetime('now', ? || ' days')")
        params.append(f"-{days_back}")
    where = " AND ".join(filters)
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM tee_times WHERE {where} ORDER BY fetched_at ASC",
            params,
        ).fetchall()


# ---------------------------------------------------------------------------
# Helper: group rows
# ---------------------------------------------------------------------------

def rows_by(rows, key_fn):
    groups = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    return groups


def avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def pct_hot_deal(rows_list):
    if not rows_list:
        return 0.0
    hot = sum(1 for r in rows_list if r["is_hot_deal"])
    return round(hot / len(rows_list) * 100, 1)


# ---------------------------------------------------------------------------
# Section 1: Price-decay curve
# ---------------------------------------------------------------------------

def price_decay_curve(rows, course_id):
    name = COURSE_NAMES.get(course_id, f"Course {course_id}")
    course_rows = [r for r in rows if r["course_id"] == course_id and r["days_ahead"] is not None]
    if not course_rows:
        return

    # Bucket by days_ahead (0-1, 2-3, 4-5, 6-7, 8-14, 15+)
    buckets = {
        "Same day (0)":   lambda d: d == 0,
        "1 day out":      lambda d: d == 1,
        "2-3 days out":   lambda d: 2 <= d <= 3,
        "4-5 days out":   lambda d: 4 <= d <= 5,
        "6-7 days out":   lambda d: 6 <= d <= 7,
        "8-14 days out":  lambda d: 8 <= d <= 14,
    }

    print(f"\n{'='*60}")
    print(f"PRICE DECAY CURVE — {name}")
    print(f"{'='*60}")
    print(f"{'Days in Advance':<20} {'Avg Price':>10} {'Min':>8} {'Max':>8} {'Hot%':>7} {'Obs':>5}")
    print("-" * 60)

    for label, fn in buckets.items():
        bucket_rows = [r for r in course_rows if fn(r["days_ahead"])]
        if not bucket_rows:
            continue
        prices  = [r["price"] for r in bucket_rows]
        avg_p   = avg(prices)
        min_p   = min(prices)
        max_p   = max(prices)
        hot_pct = pct_hot_deal(bucket_rows)
        print(f"  {label:<18} ${avg_p:>8.2f}  ${min_p:>6.2f}  ${max_p:>6.2f}  {hot_pct:>5.1f}%  {len(bucket_rows):>4}")

    # Day-of-week sensitivity for "next 7 days" window
    prime_rows = [r for r in course_rows if r["days_ahead"] is not None and 0 <= r["days_ahead"] <= 7]
    if prime_rows:
        dow_groups = rows_by(prime_rows, lambda r: datetime.strptime(r["tee_date"], "%Y-%m-%d").weekday())
        print(f"\n  Price by day of week (next 7 days window):")
        for dow in range(7):
            g = dow_groups.get(dow, [])
            if g:
                print(f"    {DAY_NAMES[dow]}:  avg ${avg([r['price'] for r in g]):.2f}  "
                      f"(range ${min(r['price'] for r in g):.2f}–${max(r['price'] for r in g):.2f})")


# ---------------------------------------------------------------------------
# Section 2: Day-of-week × time-block heatmap
# ---------------------------------------------------------------------------

def dow_time_heatmap(rows, course_id):
    name = COURSE_NAMES.get(course_id, f"Course {course_id}")
    course_rows = [r for r in rows if r["course_id"] == course_id]
    if not course_rows:
        return

    print(f"\n{'='*60}")
    print(f"PRICE HEATMAP (avg $) — {name}")
    print(f"{'='*60}")

    # Header
    header = f"{'':15}" + "".join(f"  {n:>5}" for n in DAY_NAMES)
    print(header)
    print("-" * (15 + 7 * 7))

    for block_name, h_start, h_end in TIME_BLOCKS:
        row_str = f"  {block_name:<13}"
        for dow in range(7):
            cell_rows = [
                r for r in course_rows
                if datetime.strptime(r["tee_date"], "%Y-%m-%d").weekday() == dow
                and h_start <= int(r["tee_time"][:2]) < h_end
            ]
            if cell_rows:
                cell_avg = avg([r["price"] for r in cell_rows])
                row_str += f"  ${cell_avg:>5.0f}"
            else:
                row_str += f"  {'---':>5}"
        print(row_str)

    print(f"\n  (based on {len(course_rows)} total observations)")


# ---------------------------------------------------------------------------
# Section 3: Hot Deal trigger analysis
# ---------------------------------------------------------------------------

def hot_deal_triggers(rows, course_id):
    name = COURSE_NAMES.get(course_id, f"Course {course_id}")
    hot_rows = [r for r in rows
                if r["course_id"] == course_id and r["is_hot_deal"] and r["days_ahead"] is not None]
    if not hot_rows:
        print(f"\n{name}: No Hot Deal observations yet — keep the monitor running.")
        return

    print(f"\n{'='*60}")
    print(f"HOT DEAL TRIGGERS — {name}")
    print(f"{'='*60}")

    # Days ahead when Hot Deals appear
    days_counts = defaultdict(int)
    for r in hot_rows:
        days_counts[r["days_ahead"]] += 1

    print(f"  Hot Deals observed: {len(hot_rows)}")
    print(f"  When they appear (days before tee time):")
    for d in sorted(days_counts.keys()):
        bar = "█" * min(days_counts[d], 30)
        print(f"    {d:>2} day(s) out: {days_counts[d]:>3}x  {bar}")

    # Time blocks where hot deals concentrate
    block_counts = defaultdict(int)
    for r in hot_rows:
        hour = int(r["tee_time"][:2])
        for bname, h_start, h_end in TIME_BLOCKS:
            if h_start <= hour < h_end:
                block_counts[bname] += 1
                break

    print(f"\n  Time blocks where Hot Deals appear:")
    for bname, _, _ in TIME_BLOCKS:
        cnt = block_counts.get(bname, 0)
        if cnt:
            pct = cnt / len(hot_rows) * 100
            bar = "█" * min(cnt, 30)
            print(f"    {bname:<12}: {cnt:>3}x ({pct:.0f}%)  {bar}")

    # Avg discount vs same slot non-hot-deal
    hot_prices  = [r["price"] for r in hot_rows]
    norm_rows   = [r for r in rows if r["course_id"] == course_id and not r["is_hot_deal"]
                   and r["price"] and r["holes"] == 18]
    norm_avg    = avg([r["price"] for r in norm_rows])
    hot_avg     = avg(hot_prices)
    if norm_avg and hot_avg:
        discount_pct = (norm_avg - hot_avg) / norm_avg * 100
        print(f"\n  Avg Hot Deal price:    ${hot_avg:.2f}")
        print(f"  Avg standard price:    ${norm_avg:.2f}")
        print(f"  Typical discount:      {discount_pct:.0f}% off standard")


# ---------------------------------------------------------------------------
# Section 4: Upcoming slot predictions
# ---------------------------------------------------------------------------

def price_predictions(rows, course_id):
    name = COURSE_NAMES.get(course_id, f"Course {course_id}")
    course_rows = [r for r in rows if r["course_id"] == course_id and r["days_ahead"] is not None]
    if len(course_rows) < 20:
        print(f"\n{name}: Need more data for predictions (have {len(course_rows)} 18-hole observations).")
        return

    print(f"\n{'='*60}")
    print(f"PRICE PREDICTIONS — {name}")
    print(f"{'='*60}")
    print(f"  Predicted prices for upcoming tee times based on observed decay curve.")
    print(f"  Confidence increases with more data. Check back after 2+ weeks of runs.\n")

    # Build a decay multiplier per days_ahead bucket
    # e.g. "price at day 1 is 0.87x the price at day 7"
    ref_rows  = [r for r in course_rows if 6 <= r["days_ahead"] <= 7]
    ref_price = avg([r["price"] for r in ref_rows])

    decay = {}
    for label, d_min, d_max in [
        ("7 days", 6, 7), ("5 days", 4, 5), ("3 days", 2, 3),
        ("1 day",  1, 1), ("same day", 0, 0)
    ]:
        bucket = [r for r in course_rows if d_min <= r["days_ahead"] <= d_max]
        bucket_avg = avg([r["price"] for r in bucket])
        if ref_price and bucket_avg:
            ratio = bucket_avg / ref_price
            decay[label] = (bucket_avg, ratio, len(bucket))

    if ref_price:
        print(f"  Price decay model (ref = 7-day price ${ref_price:.2f}):")
        for label, (p, ratio, cnt) in decay.items():
            print(f"    At {label:<10}: ${p:.2f}  ({ratio:.2f}x)  [{cnt} obs]")

    # Upcoming 7 days predictions
    today = date.today()
    print(f"\n  Upcoming tee time price forecast:")
    print(f"  {'Date':<15} {'Day':<5} {'Now est.':>9} {'3d est.':>9} {'1d est.':>9}")
    print(f"  {'-'*50}")

    for days_out in range(1, 8):
        future_date = today + timedelta(days=days_out)
        dow = future_date.weekday()

        # Get avg for that day-of-week from historical data
        dow_rows = [r for r in course_rows
                    if datetime.strptime(r["tee_date"], "%Y-%m-%d").weekday() == dow
                    and 6 <= r["days_ahead"] <= 7]  # 7-day-out reference price
        dow_avg = avg([r["price"] for r in dow_rows])

        if not dow_avg:
            continue

        # Apply decay ratios for predictions
        est_3d = dow_avg * decay.get("3 days", (dow_avg, 1.0, 0))[1]
        est_1d = dow_avg * decay.get("1 day",  (dow_avg, 1.0, 0))[1]

        print(f"  {str(future_date):<15} {DAY_NAMES[dow]:<5} "
              f"${dow_avg:>8.2f}  ${est_3d:>8.2f}  ${est_1d:>8.2f}")


# ---------------------------------------------------------------------------
# Section 5: Best booking windows summary
# ---------------------------------------------------------------------------

def best_booking_windows(rows):
    print(f"\n{'='*60}")
    print(f"BEST BOOKING WINDOWS (all courses)")
    print(f"{'='*60}")

    for course_id, name in COURSE_NAMES.items():
        course_rows = [r for r in rows if r["course_id"] == course_id and r["days_ahead"] is not None]
        if not course_rows:
            continue

        # Find the days_ahead bucket with the lowest avg price
        best_d, best_price = None, None
        for d in range(8):
            bucket = [r for r in course_rows if r["days_ahead"] == d]
            if len(bucket) < 3:
                continue
            p = avg([r["price"] for r in bucket])
            if best_price is None or p < best_price:
                best_d, best_price = d, p

        # Find the cheapest day-of-week
        dow_avgs = {}
        for dow in range(7):
            g = [r for r in course_rows
                 if datetime.strptime(r["tee_date"], "%Y-%m-%d").weekday() == dow]
            if len(g) >= 3:
                dow_avgs[dow] = avg([r["price"] for r in g])

        cheapest_dow = min(dow_avgs, key=dow_avgs.get) if dow_avgs else None

        print(f"\n  {name}:")
        if best_d is not None:
            print(f"    • Cheapest by timing:   Book {best_d} day(s) ahead (avg ${best_price:.2f})")
        if cheapest_dow is not None:
            print(f"    • Cheapest day of week: {DAY_NAMES[cheapest_dow]} "
                  f"(avg ${dow_avgs[cheapest_dow]:.2f})")

        # Any time block that's consistently cheap
        cheap_blocks = []
        for bname, h_start, h_end in TIME_BLOCKS:
            b_rows = [r for r in course_rows
                      if h_start <= int(r["tee_time"][:2]) < h_end]
            if len(b_rows) >= 3:
                cheap_blocks.append((bname, avg([r["price"] for r in b_rows]), len(b_rows)))
        if cheap_blocks:
            cheapest_block = min(cheap_blocks, key=lambda x: x[1])
            print(f"    • Cheapest time block:  {cheapest_block[0]} "
                  f"(avg ${cheapest_block[1]:.2f}, {cheapest_block[2]} obs)")


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(rows, output_path: str = "analysis_report.csv") -> None:
    if not rows:
        return
    cols = ["course_name", "tee_date", "tee_time", "rate_name", "price",
            "holes", "days_ahead", "is_hot_deal", "booked_players",
            "players_available", "occupancy_pct", "fetched_at"]
    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(dict(r))
    print(f"\n[analyze] CSV written: {output_path}  ({len(rows)} rows)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analyze tee-time price history")
    parser.add_argument("--course", type=int, choices=[1315, 1319],
                        help="Filter to one course (1315=Tierra Verde, 1319=Texas Rangers)")
    parser.add_argument("--days", type=int, default=None,
                        help="Only look at data from the last N days")
    parser.add_argument("--csv", action="store_true",
                        help="Also export a CSV analysis report")
    args = parser.parse_args()

    rows = fetch_rows(course_id=args.course, days_back=args.days)

    if not rows:
        print("No data yet — run monitor.py first to accumulate observations.")
        return

    print(f"\nDFW Golf Tee-Time Analysis  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Total 18-hole observations: {len(rows)}")
    print(f"Date range: {rows[0]['fetched_at'][:10]} → {rows[-1]['fetched_at'][:10]}")

    course_ids = [args.course] if args.course else list(COURSE_NAMES.keys())

    for cid in course_ids:
        price_decay_curve(rows, cid)
        dow_time_heatmap(rows, cid)
        hot_deal_triggers(rows, cid)
        price_predictions(rows, cid)

    best_booking_windows(rows)

    if args.csv:
        export_csv(fetch_rows(args.course, args.days), "analysis_report.csv")

    print(f"\n{'='*60}")
    print("TIP: Run again after 1-2 weeks for meaningful predictions.")
    print("     Patterns become clear after ~500+ observations per course.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
