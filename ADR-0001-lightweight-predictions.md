# ADR-0001: Lightweight USD TT Predictions Under GitHub Actions Free Tier

## Status

Accepted, then revised — see "Update 2026-06-26" at the end.

## Date

2026-06-25

## Context

Current tracker behaviour:

- Polls HNB API every 15 minutes.
- Sends MacroDroid notification when BUY rate changes.
- Persists change events to `data/history.jsonl`.
- End-to-end runtime is about 11 seconds per run.

Constraints and goals:

- Keep total workflow runtime comfortably under 60 seconds.
- Stay safe on public GitHub Actions quota.
- Keep dependencies minimal and avoid heavy model training in CI.
- Add prediction notifications in addition to change alerts.
- Continue supporting external triggering from cron-job.org.

## Decision

Implement a lightweight, no-third-party time-series forecaster directly in `scrape.py` using:

1. Last-observation baseline (naive forecast):
   - `y_hat(t+1) = y(t)`
2. Exponential weighted moving average (EWMA) for short-term level:
   - `level_t = alpha * y_t + (1 - alpha) * level_{t-1}`
   - Forecast is `level_t`
3. Exponential weighted variance for uncertainty band:
   - `var_t = beta * (y_t - level_t)^2 + (1 - beta) * var_{t-1}`
   - 80% band approx: `level_t +/- 1.28 * sqrt(var_t)`

At runtime, compute both naive and EWMA errors on recent history (rolling backtest) and use the better model for the outgoing prediction payload. In practice this gives robust behaviour with tiny data and almost zero compute cost.

This design includes an explicit feedback loop:

- Every prediction is logged with timestamp, horizon, chosen model, and confidence band.
- When actual data for that horizon becomes available, prediction error is computed and logged.
- Rolling error metrics are used to auto-select/tune lightweight models over time.

## Why this is the best free-tier fit

- Fast: O(n) over a small local JSONL history; typical execution in milliseconds.
- No pip installs: only Python standard library, matching current project style.
- Reliable with sparse and irregular updates (history only records changes).
- Transparent and debuggable; no hidden model state beyond history itself.
- Easy rollback: prediction path can be toggled without affecting rate-change alerts.

## Rejected alternatives

1. ARIMA/SARIMA (statsmodels):
   - Heavier dependency footprint and slower cold-start/setup in Actions.
   - Overkill for low-frequency, low-volume series.

2. Prophet/neural models:
   - Too heavy for sub-minute objective and free-tier discipline.
   - Adds maintenance overhead and package compatibility risk.

3. Separate training workflow:
   - More moving parts and additional CI minutes.
   - Limited upside for a single, slowly changing rate series.

## Future path: local training + compiled inference (phase 2)

This is viable later, but only after enough history exists.

Readiness gate before attempting offline training:

- Minimum 2,000 to 5,000 observations (roughly 3 to 9 months at 15-minute cadence if all points are stored).
- Stable backtest lift over EWMA baseline (for example >= 10% lower MAE).
- End-to-end CI runtime remains under 60 seconds after integration.

Recommended architecture:

1. Train locally on laptop:
   - Feature set: lagged rates, rolling volatility, hour-of-day, day-of-week, rate-change flags.
   - Candidate model family: small gradient-boosted trees for tabular time-series features.
2. Export compact artefact:
   - Keep model under ~1 to 5 MB.
   - Version model artefact with metadata (train window, metrics, feature schema).
3. Fast inference in CI:
   - Option A (simplest): run inference in Python with very small runtime dependency footprint.
   - Option B (max speed): compile model to native predictor (C/C++/Go binding) and call from script.
4. Safety fallback:
   - If model file missing or fails validation, automatically fall back to EWMA path.

Important note for current dataset:

- With very sparse history (currently near-empty), compiled models add complexity without accuracy benefit.
- Start with online EWMA now, collect richer history, then revisit compiled inference when thresholds are met.

## Proposed webhook behaviour

Keep existing change webhook and add a second optional prediction webhook.

New env vars:

- `PREDICTION_WEBHOOK_URL` (optional)
- `PREDICTION_HORIZON_MIN` (default `15`)
- `PREDICTION_MIN_SAMPLES` (default `12`)
- `PREDICTION_NOTIFY_EVERY_RUN` (default `false`)
- `PREDICTION_DELTA_THRESHOLD` (default `0.10` LKR)

Rules:

- If not enough samples, skip prediction silently.
- If enabled, send prediction payload to `PREDICTION_WEBHOOK_URL`.
- Default behaviour: notify only when predicted move magnitude exceeds threshold.
- Optional mode: notify every run for dashboard-style updates.

Suggested prediction payload:

```json
{
  "currency": "USD",
  "current_buy": 333.00,
  "predicted_buy": 333.12,
  "horizon_min": 15,
  "model": "ewma",
  "band_low": 332.95,
  "band_high": 333.29,
  "delta": 0.12,
  "generated_at": "2026-06-25T12:30:00+00:00"
}
```

## Feedback loop and continual learning path

The system should learn from new data in two layers.

Layer 1 (always on, in CI, near-zero cost):

- Online adaptation from each new observation using EWMA updates.
- Rolling model-score table (for example MAE over last N forecasts) to switch between naive and EWMA when performance changes.
- Drift guard: if rolling error spikes above threshold, mark prediction confidence as low and reduce alert aggressiveness.

Layer 2 (periodic, local laptop retraining):

- Weekly or monthly walk-forward retraining on full history.
- Export a compact model artefact only if it beats live EWMA baseline by a defined margin.
- Deploy artefact to repo only after passing runtime and accuracy gates.

Minimum data capture needed for a good feedback loop:

- Store each generated prediction in a predictions log (for example `data/predictions.jsonl`).
- Store enough observations to resolve each forecast horizon reliably.
- Keep a small metrics log (for example `data/prediction_metrics.json`) with rolling MAE, hit-rate, and calibration notes.

Buy/sell signal policy (feedback-aware):

- Emit BUY/SELL flags only when both expected move and confidence pass thresholds.
- If recent forecast error is elevated, downgrade to WATCH instead of strong action flags.
- Include model confidence and recent score in webhook payload so decisions are transparent.

## Runtime budget estimate

Expected run-time impact for predictions:

- Parse history and compute rolling metrics: < 100 ms for thousands of rows.
- Build and send optional second webhook: network-dominated (typically < 2 s).

Total workflow expectation after adding predictions:

- Typical: 12-20 seconds.
- P95 with transient network slowness: well below 60 seconds.

This preserves 15-minute cadence viability while staying free-tier friendly.

## Data format and storage optimisation guidance

Current scale recommendation:

- Keep `data/history.jsonl` for simplicity and transparency.
- Continue appending one record per event.

When to optimise format:

- If history grows above ~100k rows or parse time becomes noticeable, move to a compact columnar file for local training exports.
- Keep CI inference input small by precomputing only the latest feature window.

Practical compromise:

- Source of truth remains JSONL in git for auditability.
- Optional local export step creates a fast training file (for example, parquet/csv snapshot) outside CI-critical path.

## Implementation plan

1. Add `predict_next_buy(history)` helper in `scrape.py`.
2. Add lightweight backtest scoring (MAE) for naive vs EWMA.
3. Add `post_prediction_webhook(prediction_record)` helper.
4. Wire prediction call after current fetch/build path.
5. Guard with env vars and sensible defaults (off until URL is set).
6. Extend README with prediction config and payload fields.

## Consequences

Positive:

- Adds useful forward signal without heavy ML ops.
- Minimal operational and maintenance overhead.
- Keeps core alerting path intact.

Trade-offs:

- Short-horizon only; not suitable for long-range forecasting.
- No explicit seasonality/calendar effects.
- Accuracy limited by coarse and event-driven history.

## Validation plan

- Unit-test prediction helpers with synthetic constant, step-change, and noisy series.
- Dry-run locally to inspect generated prediction payload.
- Roll out with webhook configured and monitor 1 week.
- Compare prediction error and tune `alpha`, `beta`, and threshold.
- For phase 2, require walk-forward backtests that beat EWMA before enabling compiled model in production.
- Validate feedback loop itself: ensure forecast logs are reconciled to realised outcomes and rolling metrics update correctly on each run.

## Rollback plan

- Set `PREDICTION_WEBHOOK_URL` unset/empty to disable prediction notifications.
- Keep rate-change webhook untouched.
- If needed, remove prediction helpers with no schema migration required.

## Update 2026-06-26 (supersedes parts of the above)

After implementing the proposal, we researched how often the rate actually
changes and reworked the design accordingly. This section is authoritative where
it conflicts with the original text.

**Finding — the rate is daily, not intraday.** HNB's API stamps all ~17
currencies within a ~90-second window each morning (~04:36 UTC), entered by a
human operator, and its `lastUpdatedDate` endpoint returns a *date* with no time.
CBSL's official TT rate is a 9:30 AM daily figure. Conclusion: HNB sets this rate
**once per business day in the morning**; intraday moves are rare exceptions.

**Consequences applied:**

1. **Cadence** cut from every-15-min-24x7 to **every 20 min, 03:00–13:59 UTC,
   Mon–Fri** (covers the morning change window + business day; nothing wasted on
   nights/weekends). Driven by cron-job.org; GitHub `schedule` is fallback only.
2. **Modelling frequency is now DAILY.** The original 15-minute horizon was
   mismatched to change-only, irregularly-spaced history. We now maintain
   `data/daily.csv` (one row per SL business day: open/close/high/low, sell,
   spread, num_changes) as the training series, and forecast the **next business
   day** (`PREDICTION_HORIZON_DAYS`, default 1). Predictions are reconciled by
   target date against the realised daily close.
3. **Data format:** CSV for the daily series (instant load, git-diffable, zero
   deps). Parquet is still deferred — its dependency cost outweighs any benefit at
   ~250 rows/year. The earlier "fast data types" idea is explicitly not adopted.
4. **Compiled inference (Go/C/C++)** remains Phase-2-only and is now backed by a
   benchmark: forecasting 5,001 rows takes ~4 ms in pure Python. Runtime is
   100% network/runner overhead, so native binaries would save nothing.
5. **Notifications:** two independently-toggleable channels —
   `NOTIFY_MACRODROID` (push) and `NOTIFY_EMAIL` (HTML email via stdlib smtplib +
   Gmail app password). See EMAIL_SETUP.md.
6. **Robustness:** the prediction/feedback path is wrapped so it can never break
   the core rate-change alert (fixes the original implementation, which ran it
   unguarded before the alert). Data writes are deduplicated and predictions are
   logged at most once per business day, eliminating per-run commit churn.

**Honest limitation:** because the rate is administratively set, the naive
baseline is hard to beat day-to-day. The forecaster is a drift/direction
indicator, not a precise oracle; its value grows as the daily series accumulates,
gating the Phase 2 work.
