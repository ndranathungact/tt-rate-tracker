# TT Rate Tracker

Tracks **HNB's USD TT (Telegraphic Transfer) rate** and pushes an alert to your
phone (via a MacroDroid webhook) whenever it changes. Runs entirely free on
GitHub Actions.

## How it works

```
GitHub Actions (cron, every 15 min)
        │
        ▼
  scrape.py  ──fetch──►  HNB official JSON API
        │                 venus.hnb.lk/api/get_rates_contents_web
        │
        ├─ rate changed?  ──no──►  do nothing
        │
        └─ yes ──►  POST to MacroDroid webhook  ──►  📱 phone notification
                    append to data/history.jsonl (price history for later predictions)
```

### Why an API instead of scraping HTML
HNB's public exchange-rate page is a reCAPTCHA-gated React app — a normal scraper
gets nothing. The page itself loads from an **unauthenticated official JSON
endpoint**, which is exactly what this project calls. It's faster, cleaner, and
far less fragile than HTML scraping, and it includes an `updated_on` timestamp.

Example response (trimmed):
```json
{"currencyCode":"USD","buyingRate":333,"sellingRate":341.5,
 "updated_on":"2026-06-25T04:35:59.000Z","status":"CURRENT"}
```

## Feasibility / cost (the short version)

| Question | Answer |
|---|---|
| Can we get the HNB USD TT rate reliably? | **Yes** — official JSON API, no scraping. |
| More often than once a day? | **Yes** — polls every 15 min. |
| Free on GitHub Actions? | **Yes** — see the repo-visibility note below. |
| Need the paid (<500 LKR/mo) alternative? | **No.** |

**Currently configured for a PUBLIC repo, every 15 min** (`cron: 7,22,37,52 * * * *`).

- **Public repo (current):** Actions minutes are free and **unlimited** → every
  15 min costs nothing. Note: billing is per-run rounded up to 1 minute, so on a
  *private* repo frequency, not runtime, is what spends the budget.
- **Private repo:** Free plan includes only 2,000 min/month. Every 15 min ≈
  2,880 min → over the limit. Drop the `cron` in
  [.github/workflows/tt-rate.yml](.github/workflows/tt-rate.yml) to
  `7,37 * * * *` (every 30 min ≈ 1,440 min) to stay free.
- Minutes are offset to `:07`/`:22`/`:37`/`:52` (not `:00`/`:30`) because the
  top/bottom of the hour is GitHub's most congested, most-delayed scheduler slot.
- GitHub disables a *scheduled* workflow after **60 days of repo inactivity**.
  The commit-on-change keeps the repo active in practice; if rates somehow don't
  move for 60 days, re-enable it from the Actions tab.

### ⚠️ GitHub's `schedule` is unreliable — use cron-job.org for punctual runs
GitHub's built-in cron is best-effort: on new/low-activity repos the first runs
are delayed by hours or dropped entirely. **For reliable triggering, a free
external scheduler (cron-job.org) pokes the workflow via the GitHub API** — see
**[EXTERNAL_CRON_SETUP.md](EXTERNAL_CRON_SETUP.md)**. The `schedule:` block then
acts as a harmless fallback (or you can remove it).

### Other free schedulers (alternatives to cron-job.org)
- **Cloudflare Workers Cron Triggers** — free tier, can run the fetch + POST.
- **Deno Deploy / Val Town** — free scheduled functions.

## Setup

1. **Create the GitHub repo** and push this folder to it (public recommended).
2. **Set the webhook secret:** repo → Settings → Secrets and variables → Actions
   → add `MACRODROID_WEBHOOK_URL` (and optionally `WEBHOOK_SHARED_SECRET`).
   See [MACRODROID_WEBHOOK_SETUP.md](MACRODROID_WEBHOOK_SETUP.md) for the full,
   secure Android setup.
3. **Enable Actions** if prompted, then trigger a first run:
   Actions → *TT Rate Tracker* → **Run workflow** (tick *force*).

## Local testing

```bash
python3 scrape.py --dry-run     # fetch + print, no webhook, no state change
MACRODROID_WEBHOOK_URL="https://trigger.macrodroid.com/<id>/usd_rate" \
  python3 scrape.py --force     # actually post once
```

No third-party Python packages required (standard library only).

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `MACRODROID_WEBHOOK_URL` | – | Your MacroDroid webhook (set as a GitHub secret). |
| `WEBHOOK_METHOD` | `GET` | `GET` (query params) or `POST` (JSON body). |
| `WEBHOOK_SHARED_SECRET` | – | Optional token appended as `?token=…` for the macro to verify. |
| `ALWAYS_POST` | `false` | `true` = post every run, not just on change. |
| `CURRENCY_CODE` | `USD` | Track a different currency if you want. |

## Data / history

- `data/latest.json` — the last rate we acted on (used for change detection).
- `data/history.jsonl` — one line per change. This is your dataset for the
  **future price-prediction** feature.

## Roadmap

- [x] Fetch HNB USD TT rate from the official API.
- [x] Alert via MacroDroid on change.
- [x] Persist a price history.
- [ ] Predictions (later) — train on `data/history.jsonl`.
