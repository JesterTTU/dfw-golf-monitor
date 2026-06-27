# DFW Golf Tee-Time Monitor

Monitors **16 DFW-area golf courses** every hour via GitHub Actions, stores price history in SQLite, and fires Discord alerts when a deal is detected.

Live dashboard: **https://golf.thornado.fun**

---

## Monitored Courses

| Course | Facility IDs |
|--------|-------------|
| City of Arlington (Tierra Verde, Texas Rangers, Lake Arlington) | 1315, 1319, 4992 |
| Tangle Ridge | 846 |
| Pecan Hollow | 1307 |
| Firewheel (Bridges, Old, Lakes) | 9413, 9415, 9414 |
| Waterchase | 48 |
| Mesquite Golf Club | 3184 |
| Irving Golf Club | 3186 |
| Prairie Lakes | 1311 |
| Champions Circle | 1280 |
| Tour 18 | 1391 |
| Wildhorse at Robson Ranch | 3793 |
| Cleburne Golf Links | 6335 |

---

## How It Works

**API (no auth required):**
```
GET https://phx-api-be-east-1b.kenna.io/v2/tee-times
  ?date=YYYY-MM-DD
  &facilityIds=1315
  x-be-alias: city-of-arlington
```

Prices are returned in **cents** (`greenFeeCart`, `dueOnlineRiding`, `promotion.greenFeeCart`).

**Two alert types:**
1. **threshold** — price drops below your configured dollar threshold (by day type + time of day)
2. **hot_deal / below_average** — price is ≥25% / ≥20% below the rolling 7-day average for that slot + day-of-week bucket

Alerts route to separate Discord channels: hot deals get their own channel, everything else goes to the main channel.

---

## Quick Start

### 1. Clone and install
```bash
git clone https://github.com/JesterTTU/dfw-golf-monitor
cd dfw-golf-monitor/tee-time-monitor
pip install -r requirements.txt
```

### 2. Create config.json
```bash
cp config.example.json config.json
# Edit config.json: paste your Discord webhook URLs
```

### 3. Run locally
```bash
python monitor.py
```

---

## Discord Setup

1. Server Settings → Integrations → Webhooks → New Webhook
2. Create two webhooks: one for general alerts, one for hot deals
3. Paste URLs into `config.json` as `discord_webhook_url` and `discord_webhook_hot_deals_url`

**Security:** Never commit `config.json` — it's in `.gitignore`. Use GitHub Actions Secrets for CI.

---

## GitHub Actions Setup

1. Go to **Settings → Secrets → Actions → New repository secret**
2. Add `DISCORD_WEBHOOK` — your main channel webhook URL
3. Add `DISCORD_WEBHOOK_HOT_DEALS` — your hot deals channel webhook URL

The workflow at `.github/workflows/monitor.yaml` runs automatically every hour.
