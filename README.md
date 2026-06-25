# Arlington Golf Tee-Time Monitor

Polls the **Tierra Verde** and **Texas Rangers** golf courses (City of Arlington, TX) every 2 hours via GitHub Actions, stores price history in SQLite, and fires Discord alerts when a deal is found.

---

## How It Works

**API (no auth required):**
```
GET https://phx-api-be-east-1b.kenna.io/v2/tee-times
  ?date=YYYY-MM-DD
  &facilityIds=1315          # Tierra Verde (use 1319 for Texas Rangers)
  x-be-alias: city-of-arlington
```

Prices are returned in **cents** (`greenFeeCart`, `dueOnlineRiding`, `promotion.greenFeeCart`). The `showAsHotDeal: true` flag marks explicit TeeItUp Hot Deals.

**Three alert types:**
1. **hot_deal** — TeeItUp explicitly marks the slot as a Hot Deal
2. **threshold** — price drops below your configured dollar threshold (by day type + time)
3. **below_average** — price drops more than X% below the rolling 7-day average for that slot

---

## Quick Start

### 1. Clone and install
```bash
git clone https://github.com/YOUR_USERNAME/dfw-golf-monitor
cd dfw-golf-monitor/tee-time-monitor
pip install -r requirements.txt
```

### 2. Create config.json
```bash
cp config.example.json config.json
# Edit config.json: paste your Discord webhook URL
```

### 3. Run locally
```bash
python monitor.py
# First run logs a live API sample — verify prices look correct
```

### 4. Deploy to GitHub Actions (free, runs every 2 hours)
See "GitHub Actions Setup" below.

---

## Discord Setup

1. Open Discord → Server Settings → Integrations → Webhooks → New Webhook
2. Choose the channel, copy the webhook URL
3. Paste it into `config.json` as `discord_webhook_url`
4. Test: `python alerts.py` — you should get a test ping in Discord

---

## GitHub Actions Setup

After pushing this repo to GitHub:

1. Go to **Settings → Secrets and variables → Actions → New repository secret**
2. Name: `DISCORD_WEBHOOK`
3. Value: your Discord webhook URL

The workflow at `.github/workflows/monitor.yml` runs automatically every 2 hours.
To trigger a manual run: **Actions → Tee Time Monitor → Run workflow**.

---

## Alert Thresholds

Edit `config.json` to tune when you get alerted:

```json
"alert_thresholds": {
  "weekday":       65,    // Mon–Thu, any time (Tierra Verde runs $69–96 standard)
  "friday":        80,    // Friday standard
  "weekend":       90,    // Sat–Sun standard
  "twilight_any":  50,    // 4 PM+ (TV runs ~$62 standard)
  "super_twilight": 40    // 5:30 PM+ (TV runs ~$49 standard)
}
```

You'll also get an alert any time a price is 20% below its 7-day rolling average
(configurable as `alert_below_average_pct`).

---

## Dashboard

Run `python export_dashboard.py` to write `dashboard_data.json`, then open
`dashboard.html` in a browser. Auto-refreshes every 2 minutes. Color coding:
- **Green** = deal (below threshold or below average)
- **Yellow** = normal
- **Gray** = expensive

---

## Files

| File | Purpose |
|------|---------|
| `monitor.py` | Main polling script |
| `db.py` | SQLite schema + queries |
| `alerts.py` | Discord + email alert delivery |
| `export_dashboard.py` | SQLite → dashboard_data.json |
| `dashboard.html` | Static HTML dashboard |
| `config.example.json` | Template config (copy to config.json) |
| `requirements.txt` | Python deps |
| `.github/workflows/monitor.yml` | GitHub Actions cron |

---

## Live Price Reference (June 2026)

| Course | Slot | Current Range | Alert Threshold |
|--------|------|--------------|-----------------|
| Tierra Verde | Weekday morning/afternoon | $69–$96 | $65 |
| Tierra Verde | Friday | $80–$107 | $80 |
| Tierra Verde | Weekend | $90–$107 | $90 |
| Tierra Verde | Twilight (4 PM+) | $62 | $50 |
| Tierra Verde | Super Twilight (5:30 PM+) | $49 | $40 |
| Texas Rangers | Weekday | $98–$150 | included in weekday alert |
| Texas Rangers | Twilight | $74–$92 | included in twilight alert |

Hot Deals are always alerted regardless of threshold (they're already flagged by TeeItUp).

---

## Notes

- The API endpoint was discovered via Chrome DevTools in June 2026. If it changes,
  reload `https://city-of-arlington.book.teeitup.com/teetimes?course=1315` in Chrome,
  open DevTools → Network, and look for `v2/tee-times` in the kenna.io calls.
- Poll rate: every 2 hours via GitHub Actions (free tier). This is well under any
  reasonable rate limit — the site loads this API call on every page view.
- Legal: no authentication is required to view tee times on this site. This tool
  reads publicly available pricing data. See *hiQ Labs v. LinkedIn* (9th Cir. 2022).
