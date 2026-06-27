"""
monitor.py — Main polling script for the Arlington TX tee-time monitor.

Run:
    python monitor.py                    # uses config.json in same directory
    DB_PATH=/path/to/custom.db python monitor.py

Cron / GitHub Actions:
    The script exits 0 on success, 1 only for unrecoverable startup errors.
    Per-course fetch errors are logged but do NOT crash the process.

─────────────────────────────────────────────────────────────────────────────
API (discovered June 2026 via Chrome DevTools):

  GET https://phx-api-be-east-1b.kenna.io/v2/tee-times
    ?date=YYYY-MM-DD
    &facilityIds={1315 | 1319}

  Required header:  x-be-alias: city-of-arlington
  No auth / login required.

Response shape:
  [
    {
      "dayInfo": { "sunrise": "...", "sunset": "..." },
      "teetimes": [
        {
          "teetime":       "2026-06-29T13:10:00.000Z",   <- UTC ISO
          "backNine":      false,
          "minPlayers":    1,
          "maxPlayers":    4,
          "bookedPlayers": 0,
          "rates": [
            {
              "name":           "18 Holes",
              "holes":          18,
              "allowedPlayers": [1,2,3,4],
              "showAsHotDeal":  false,
              "dueOnlineRiding": 6800,   <- cents (divide by 100 for dollars)
              "greenFeeCart":    8100,   <- cents
              "promotion": {
                "discount":     0.02,
                "greenFeeCart": 7900    <- cents, with online discount applied
              }
            },
            { "name": "Hot Deal", "showAsHotDeal": true, ... }
          ]
        }
      ]
    }
  ]

Facility IDs:
  Tierra Verde Golf Club  -> 1315
  Texas Rangers Golf Club -> 1319
─────────────────────────────────────────────────────────────────────────────
"""

import json
import logging
import os
import sys
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import requests

from db import init_db, insert_tee_time, get_rolling_average, upsert_courses
from alerts import send_alert

# ---------------------------------------------------------------------------
# Logging — timestamps visible in GitHub Actions logs
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API constants (confirmed live, no auth needed)
# ---------------------------------------------------------------------------
KENNA_BASE      = "https://phx-api-be-east-1b.kenna.io"
TEEITUP_API     = f"{KENNA_BASE}/v2/tee-times"
DEFAULT_BE_ALIAS = "city-of-arlington"      # fallback x-be-alias header value

# Arlington TX is Central Time
ARLINGTON_TZ = ZoneInfo("America/Chicago")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: Optional[str] = None) -> dict:
    """
    Load config.json.  Secrets can be overridden by environment variables:
      DISCORD_WEBHOOK -> overrides config discord_webhook_url
      DB_PATH         -> overrides default db path (handled in db.py)
    """
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.json")

    if not os.path.exists(path):
        logger.error(
            "config.json not found at %s.  "
            "Copy config.example.json -> config.json and fill in your values.",
            path,
        )
        sys.exit(1)

    with open(path) as f:
        cfg = json.load(f)

    # Allow env-var override of webhook (useful for GitHub Actions secrets)
    env_webhook = os.environ.get("DISCORD_WEBHOOK")
    if env_webhook:
        cfg["discord_webhook_url"] = env_webhook

    return cfg


# ---------------------------------------------------------------------------
# Date / time helpers
# ---------------------------------------------------------------------------

def dates_to_fetch(days_ahead: int) -> list[str]:
    """Return ISO date strings for today + the next N days (Arlington local date)."""
    today = datetime.now(ARLINGTON_TZ).date()
    return [(today + timedelta(days=i)).isoformat() for i in range(days_ahead)]


def is_weekend(date_str: str) -> bool:
    d = date.fromisoformat(date_str)
    return d.weekday() >= 5   # 5=Saturday, 6=Sunday


def is_friday(date_str: str) -> bool:
    return date.fromisoformat(date_str).weekday() == 4


def teetime_to_local_hhmm(teetime_utc: str) -> str:
    """
    Convert a UTC ISO-8601 teetime string to local Arlington HH:MM.
    e.g. "2026-06-29T13:10:00.000Z" -> "08:10" (CDT = UTC-5)
    """
    try:
        dt_utc = datetime.fromisoformat(teetime_utc.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ARLINGTON_TZ)
        return dt_local.strftime("%H:%M")
    except (ValueError, AttributeError):
        if "T" in teetime_utc:
            return teetime_utc.split("T")[1][:5]
        return teetime_utc[:5]


def is_twilight(tee_time_hhmm: str) -> bool:
    """Any tee time at or after 16:00 (4 PM) local."""
    try:
        hour = int(tee_time_hhmm[:2])
        return hour >= 16
    except (ValueError, AttributeError):
        return False


def is_super_twilight(tee_time_hhmm: str) -> bool:
    """Any tee time at or after 17:30 (5:30 PM) local."""
    try:
        hour, minute = int(tee_time_hhmm[:2]), int(tee_time_hhmm[3:5])
        return (hour > 17) or (hour == 17 and minute >= 30)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Threshold logic
# ---------------------------------------------------------------------------

def get_threshold(date_str: str, tee_time_hhmm: str, thresholds: dict) -> float:
    """
    Return the deal-price threshold (in dollars) for a given date + local time.

    Priority (most specific first):
      1. super twilight (>= 17:30) — uses twilight_any if super_twilight not set
      2. twilight_any  (>= 16:00)
      3. weekend
      4. friday
      5. weekday
    """
    if is_super_twilight(tee_time_hhmm):
        return float(thresholds.get("super_twilight", thresholds.get("twilight_any", 999)))
    if is_twilight(tee_time_hhmm):
        return float(thresholds.get("twilight_any", 999))
    if is_weekend(date_str):
        return float(thresholds.get("weekend", 999))
    if is_friday(date_str):
        return float(thresholds.get("friday", 999))
    return float(thresholds.get("weekday", 999))


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def fetch_tee_times(
    facility_id: int,
    date_str: str,
    session: requests.Session,
    be_alias: str = DEFAULT_BE_ALIAS,
) -> list[dict]:
    """
    Fetch available tee times for one facility on one date.

    Returns a list of raw teetime slot dicts (from response[0]['teetimes']).
    Returns [] on API or parse errors (caller logs them).
    """
    params  = {"date": date_str, "facilityIds": facility_id}
    headers = {
        "x-be-alias": be_alias,
        "Accept":     "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; TeeTimeMonitor/1.0)",
    }

    resp = session.get(TEEITUP_API, params=params, headers=headers, timeout=20)
    resp.raise_for_status()

    data = resp.json()

    # Response is an array; element [0] has "teetimes" list
    if not isinstance(data, list) or not data:
        logger.warning("[fetch] Unexpected response for facility=%d date=%s: %s",
                       facility_id, date_str, type(data).__name__)
        return []

    teetimes = data[0].get("teetimes", [])

    # Log a sample on first successful call for debugging
    if not fetch_tee_times._logged_sample and teetimes:
        logger.info(
            "[fetch] LIVE API SAMPLE (facility=%d, date=%s):\n%s",
            facility_id, date_str,
            json.dumps(teetimes[:2], indent=2),
        )
        fetch_tee_times._logged_sample = True

    return teetimes


fetch_tee_times._logged_sample = False   # module-level flag


# ---------------------------------------------------------------------------
# Price extraction helpers
# ---------------------------------------------------------------------------

def best_price_dollars(rate: dict) -> Optional[float]:
    """
    Extract the best (lowest) price in dollars from a rate dict.

    The API stores prices in CENTS as integers.
    Priority: promotion.greenFeeCart > dueOnlineRiding > greenFeeCart
    (promotion.greenFeeCart is the online-discount price — the one shown to users)
    """
    # Online-discounted price (e.g., 2% off for booking online)
    promo = rate.get("promotion") or {}
    promo_price_cents = promo.get("greenFeeCart")
    if promo_price_cents and promo_price_cents > 0:
        return round(promo_price_cents / 100, 2)

    # What you actually pay when riding (may differ from greenFeeCart)
    due_cents = rate.get("dueOnlineRiding")
    if due_cents and due_cents > 0:
        return round(due_cents / 100, 2)

    # Fallback: base green fee + cart
    gfc = rate.get("greenFeeCart")
    if gfc and gfc > 0:
        return round(gfc / 100, 2)

    return None


# ---------------------------------------------------------------------------
# Single teetime slot processing
# ---------------------------------------------------------------------------

def process_teetime(
    slot: dict,
    course_id: int,
    course_name: str,
    date_str: str,
    thresholds: dict,
    below_avg_pct: float,
    hot_deal_pct: float,
    config: dict,
    players_needed: int,
    booking_alias: str = "city-of-arlington",
) -> None:
    """
    Process one teetime slot (which may have multiple rates).

    Each rate is stored independently, and the cheapest rate is used for
    threshold / rolling-average alert checks.
    """
    teetime_utc = slot.get("teetime", "")
    if not teetime_utc:
        return

    local_hhmm  = teetime_to_local_hhmm(teetime_utc)
    max_players = slot.get("maxPlayers") or slot.get("players")
    rates       = slot.get("rates", [])

    if not rates:
        return

    # Filter rates to 18-hole options that allow the requested player count
    eighteen_hole = [r for r in rates if r.get("holes") == 18
                     and not r.get("isSimulator")
                     and (not r.get("allowedPlayers")
                          or players_needed in r.get("allowedPlayers", [players_needed]))]

    candidate_rates = eighteen_hole if eighteen_hole else [
        r for r in rates if not r.get("isSimulator")
    ]

    if not candidate_rates:
        return

    # Find cheapest rate for alerting
    best_rate  = None
    best_price = None

    for rate in candidate_rates:
        p = best_price_dollars(rate)
        if p is None:
            continue
        if best_price is None or p < best_price:
            best_price = p
            best_rate  = rate

    # Persist ALL candidate rates (for price history + analysis)
    booked = slot.get("bookedPlayers") or slot.get("booked_players")
    for rate in candidate_rates:
        p     = best_price_dollars(rate)
        holes = rate.get("holes")
        insert_tee_time(
            course_id=course_id,
            course_name=course_name,
            tee_date=date_str,
            tee_time=local_hhmm,
            price=p,
            holes=holes,
            players_available=max_players,
            rate_name=rate.get("name"),
            is_hot_deal=bool(rate.get("showAsHotDeal")),
            booked_players=booked,
        )

    if best_price is None or best_price <= 0 or best_rate is None:
        return

    holes = best_rate.get("holes")
    # NOTE: we still store TeeItUp's showAsHotDeal flag in the DB for analysis,
    # but we do NOT use it to trigger alerts — their definition is unreliable
    # (e.g. 14% off labelled as "Hot Deal"). We use our own pct-based tiers instead.

    threshold = get_threshold(date_str, local_hhmm, thresholds)

    # --- Check 1: fixed-threshold deal ---
    if best_price < threshold:
        reason = (
            f"${best_price:.2f} is below the ${threshold:.0f} threshold "
            f"({_day_label(date_str, local_hhmm)})"
        )
        send_alert(
            course_id=course_id,
            course_name=course_name,
            tee_date=date_str,
            tee_time=local_hhmm,
            price=best_price,
            holes=holes,
            players_available=max_players,
            alert_type="threshold",
            reason=reason,
            config=config,
            booking_alias=booking_alias,
        )
        return

    # --- Check 2: percentage below rolling 7-day average (our own hot deal definition) ---
    # Two tiers:
    #   hot_deal_pct  (default 25%) → "hot_deal"  alert → hot-deals Discord channel
    #   below_avg_pct (default 20%) → "below_average" alert → main Discord channel
    # Checked high→low so the stronger discount wins.
    try:
        hour = int(local_hhmm[:2])
        dow  = date.fromisoformat(date_str).weekday()
    except ValueError:
        return

    avg = get_rolling_average(course_id, hour, dow, days=7)
    if avg is not None:
        pct_below = (avg - best_price) / avg * 100
        dow_name  = date.fromisoformat(date_str).strftime('%A')

        if pct_below >= hot_deal_pct:
            reason = (
                f"${best_price:.2f} is {pct_below:.0f}% below 7-day avg "
                f"of ${avg:.2f} ({dow_name} {local_hhmm}) — "
                f"exceeds hot-deal threshold of {hot_deal_pct:.0f}%"
            )
            send_alert(
                course_id=course_id,
                course_name=course_name,
                tee_date=date_str,
                tee_time=local_hhmm,
                price=best_price,
                holes=holes,
                players_available=max_players,
                alert_type="hot_deal",
                reason=reason,
                config=config,
                booking_alias=booking_alias,
            )
        elif pct_below >= below_avg_pct:
            reason = (
                f"${best_price:.2f} is {pct_below:.0f}% below 7-day avg "
                f"of ${avg:.2f} for {local_hhmm} on {dow_name}s"
            )
            send_alert(
                course_id=course_id,
                course_name=course_name,
                tee_date=date_str,
                tee_time=local_hhmm,
                price=best_price,
                holes=holes,
                players_available=max_players,
                alert_type="below_average",
                reason=reason,
                config=config,
                booking_alias=booking_alias,
            )


def _day_label(date_str: str, tee_time_hhmm: str) -> str:
    if is_super_twilight(tee_time_hhmm):
        return "super twilight"
    if is_twilight(tee_time_hhmm):
        return "twilight"
    if is_weekend(date_str):
        return "weekend"
    if is_friday(date_str):
        return "friday"
    return "weekday"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logger.info("=" * 60)
    logger.info("Tee-time monitor starting  %s", datetime.now(timezone.utc).isoformat())
    logger.info("=" * 60)

    config = load_config()
    init_db()

    courses       = config.get("courses", [])
    upsert_courses(courses)   # keep courses table in sync with config
    days_ahead    = int(config.get("poll_days_ahead", 7))
    thresholds    = config.get("alert_thresholds", {})
    below_avg_pct = float(config.get("alert_below_average_pct", 20))
    hot_deal_pct  = float(config.get("hot_deal_pct", 25))
    dates         = dates_to_fetch(days_ahead)

    logger.info("Courses to check : %s", [c["name"] for c in courses])
    logger.info("Dates to check   : %s -> %s (%d days)", dates[0], dates[-1], len(dates))
    logger.info("Thresholds       : %s", thresholds)

    session     = requests.Session()
    total_slots = 0
    errors      = 0

    for course in courses:
        course_id   = course["id"]
        course_name = course["name"]
        players     = course.get("players", 2)
        be_alias    = course.get("alias", DEFAULT_BE_ALIAS)

        for date_str in dates:
            try:
                slots = fetch_tee_times(course_id, date_str, session, be_alias=be_alias)
                logger.info("[%s] %s -> %d teetimes", course_name, date_str, len(slots))

                for slot in slots:
                    process_teetime(
                        slot=slot,
                        course_id=course_id,
                        course_name=course_name,
                        date_str=date_str,
                        thresholds=thresholds,
                        below_avg_pct=below_avg_pct,
                        hot_deal_pct=hot_deal_pct,
                        config=config,
                        players_needed=players,
                        booking_alias=be_alias,
                    )
                total_slots += len(slots)

            except requests.HTTPError as exc:
                errors += 1
                logger.warning("[%s] HTTP error on %s: %s", course_name, date_str, exc)
            except requests.RequestException as exc:
                errors += 1
                logger.warning("[%s] Network error on %s: %s", course_name, date_str, exc)
            except Exception as exc:
                errors += 1
                logger.exception("[%s] Unexpected error on %s: %s", course_name, date_str, exc)

    logger.info(
        "Poll complete. Total teetimes=%d, errors=%d, UTC=%s",
        total_slots, errors, datetime.now(timezone.utc).isoformat(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
