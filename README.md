# TT Rate Tracker

Tracks **HNB's USD TT (Telegraphic Transfer) rate**, keeps a clean **daily
series**, forecasts the **next business day** with a self-correcting model, and
alerts you on a buy-rate change via **MacroDroid push and/or a formatted HTML
email**. Runs entirely free on GitHub Actions.

## How it works

```
cron-job.org (weekday business hours)  ──workflow_dispatch──►  GitHub Actions
                                                                     │
                                                                     ▼
                              scrape.py  ──fetch──►  HNB official JSON API
                                   │                 venus.hnb.lk/api/get_rates_contents_web
                                   │
                                   ├─ update data/daily.csv (the model's training series)
                                   ├─ forecast next business day  (naive/EWMA/Holt, auto-picked)
                                   │
                                   └─ BUY rate changed? ──yes──►  📱 MacroDroid push
                                                                  ✉️  HTML email (rate + forecast)
```

Why **daily**? HNB's own API timestamps show it sets this rate **once per
business day in the morning** (all currencies stamped within ~90 s around
~04:36 UTC), and its "last updated" field is a *date*, not a time. So the natural
modelling frequency is daily — that's what we store and forecast on.

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

## Cadence (and why it's not every 15 minutes any more)

The rate changes ~once per business day, so blanket 15-min polling wasted ~95 of
every 96 runs. The schedule is now **every 20 min, 03:00–13:59 UTC, Mon–Fri**
(`cron: */20 3-13 * * 1-5`) — i.e. ~08:30–19:30 SL time on weekdays. That catches
the daily morning change within ~20 min, with nothing wasted on nights/weekends.

- **Cost:** free regardless. On a **public** repo Actions minutes are unlimited;
  even on a **private** repo this is ~33 runs/weekday ≈ 700 min/month, well under
  the 2,000 free. (Billing rounds each run up to 1 minute, so *frequency*, not the
  ~12 s runtime, is what would spend a private budget.)

### ⚠️ GitHub's `schedule` is unreliable — cron-job.org is the real trigger
GitHub's built-in cron is best-effort: on low-activity repos runs are delayed by
hours or dropped. **Reliable triggering comes from a free external scheduler
(cron-job.org) calling the workflow via the GitHub API** — see
**[EXTERNAL_CRON_SETUP.md](EXTERNAL_CRON_SETUP.md)**. The `schedule:` block above
is just a fallback. Other free options: Cloudflare Workers Cron, Deno Deploy.

## Setup

1. **Push this repo** to GitHub and **enable Actions**.
2. **Reliable trigger:** set up cron-job.org → [EXTERNAL_CRON_SETUP.md](EXTERNAL_CRON_SETUP.md).
3. **Pick channels** (at least one):
   - **MacroDroid push** — add secret `MACRODROID_WEBHOOK_URL` (and optional
     `WEBHOOK_SHARED_SECRET`); see [MACRODROID_WEBHOOK_SETUP.md](MACRODROID_WEBHOOK_SETUP.md).
   - **Email** — add `SMTP_USERNAME` / `SMTP_PASSWORD` / `EMAIL_TO` and set
     `NOTIFY_EMAIL: "true"` in the workflow; see [EMAIL_SETUP.md](EMAIL_SETUP.md).
4. **First run:** Actions → *TT Rate Tracker* → **Run workflow** (tick *force*).

## Local testing

```bash
python3 scrape.py --dry-run     # fetch + print, no notifications, no writes
python3 scrape.py --email-test --dry-run   # build the email, don't send
MACRODROID_WEBHOOK_URL="https://trigger.macrodroid.com/<id>/usd_rate" \
  python3 scrape.py --force     # actually notify once
```

No third-party Python packages required (standard library only).

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `NOTIFY_MACRODROID` | `true` | Master toggle for the MacroDroid push channel. |
| `MACRODROID_WEBHOOK_URL` | – | Your MacroDroid webhook (set as a GitHub secret). |
| `WEBHOOK_METHOD` | `GET` | `GET` (query params) or `POST` (JSON body). |
| `WEBHOOK_SHARED_SECRET` | – | Optional token appended as `?token=…` for the macro to verify. |
| `NOTIFY_EMAIL` | `false` | Master toggle for the HTML email channel. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | – | SMTP login + **app password / SMTP key** (secrets). Any provider — see [EMAIL_SETUP.md](EMAIL_SETUP.md). |
| `EMAIL_TO` | – | Recipient(s), comma-separated (secret). |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `465` | SMTP server. `465`=SSL, `587`=STARTTLS (auto). Gmail/Brevo/Mailjet/SMTP2GO all work. |
| `EMAIL_DAILY_DIGEST` | `false` | `true` = one summary email/day, even on flat days. |
| `EMAIL_DAILY_AFTER_UTC` | `05:30` | Only send signal/digest emails after this UTC time. |
| `ALWAYS_POST` | `false` | `true` = notify every run, not just on change. |
| `CURRENCY_CODE` | `USD` | Track a different currency if you want. |

### Prediction / feedback-loop config (optional)

The forecaster (naive / EWMA / Holt-trend, auto-selected by live error) runs on
every poll. The **feedback loop always runs** (it logs each forecast, reconciles
it against the realised rate, and updates rolling metrics). The **prediction
webhook is off until you set `PREDICTION_WEBHOOK_URL`**.

| Variable | Default | Purpose |
|---|---|---|
| `PREDICTION_WEBHOOK_URL` | – | Second webhook for BUY/SELL/WATCH alerts (set as a GitHub secret). |
| `PREDICTION_WEBHOOK_METHOD` | `GET` | `GET` (query params) or `POST` (JSON body). |
| `PREDICTION_NOTIFY_EVERY_RUN` | `false` | `true` = send a prediction every run (dashboard mode), not only on BUY/SELL. |
| `PREDICTION_HORIZON_DAYS` | `1` | Forecast horizon in **business days** (the rate is daily). |
| `PREDICTION_MIN_SAMPLES` | `10` | Minimum daily points before predicting (else `WARMUP`). |
| `PREDICTION_DELTA_THRESHOLD` | `0.10` | Min predicted move (LKR) to emit an actionable signal. |
| `PREDICTION_EWMA_ALPHA` | `0.3` | Level smoothing. |
| `PREDICTION_EWMA_BETA` | `0.2` | Variance (uncertainty band) smoothing. |
| `PREDICTION_HOLT_GAMMA` | `0.1` | Trend smoothing for the Holt model. |

**Signals:** `BUY` (USD likely to rise), `SELL` (USD likely to fall), `HOLD`
(move within noise/threshold), `WATCH` (signal present but recent error high —
drift guard), `WARMUP` (not enough data yet).

## Data files

- **`data/daily.csv`** — **the model's training series.** One row per SL business
  day: `date, dow, buy_open, buy_close, buy_high, buy_low, sell_close, spread,
  num_changes`. Regular daily spacing + features → far better for forecasting than
  the raw event log. CSV (not Parquet) on purpose: instant to load, git-diffable,
  zero dependencies at this scale.
- `data/history.jsonl` — append-only event log (source of truth), one line per
  buy change, enriched with spread + the API's `updated_on`.
- `data/latest.json` — last acted-on rate (change detection).
- `data/predictions.jsonl` — one forecast per business day, reconciled against the
  realised next-day close (the feedback-loop train/eval log).
- `data/prediction_metrics.json` — rolling MAE, naive MAE, directional hit-rate.
- `data/notify_state.json` — per-day email dedup (so 33 runs/day ≠ 33 emails).

> **Honest expectation:** this rate is *administratively set* (HNB tracks the
> interbank USD/LKR + a margin), so day-to-day it's sticky and the naive baseline
> is hard to beat. Treat the forecast as a **drift/direction indicator**, not a
> precise oracle. The value compounds as the daily series grows.

## Roadmap

- [x] Fetch HNB USD TT rate from the official API.
- [x] Alert on change — MacroDroid push and/or HTML email (each toggleable).
- [x] Daily series + next-business-day forecaster (naive/EWMA/Holt, auto-selected)
  with BUY/SELL/WATCH signals and a self-correcting feedback loop
  (see [ADR-0001-lightweight-predictions.md](ADR-0001-lightweight-predictions.md)).
- [ ] Phase 2 — local laptop training + compiled inference once enough daily
  history accumulates and it beats the live baseline.
