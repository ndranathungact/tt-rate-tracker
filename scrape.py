#!/usr/bin/env python3
"""
TT Rate Tracker — fetches HNB's USD TT (Telegraphic Transfer) rate, records a
clean daily series, forecasts the next business day, and notifies via MacroDroid
push and/or a formatted HTML email.

Data source (official HNB API, no auth / no scraping):
    https://venus.hnb.lk/api/get_rates_contents_web

Evidence shows HNB sets this rate ~once per business day, in the morning
(~04:00-05:00 UTC = ~09:30-10:30 SL time). So the natural modelling frequency is
DAILY, and that is what this script stores and forecasts on.

Behaviour:
    * Fetch the latest USD buying/selling rate.
    * Maintain data/daily.csv (one row per SL business day: open/close/high/low,
      sell, spread, num_changes) — the model's training series.
    * Append enriched change events to data/history.jsonl (source of truth).
    * Forecast next business day's BUY rate (naive / EWMA / Holt, auto-selected
      by live error) with a self-correcting feedback loop. NEVER allowed to break
      the core alert (fully guarded).
    * On a BUY-rate change, notify the enabled channels (MacroDroid + email).
      Sell-only moves do not trigger, but the sell price is always shown.

Channels (each independently on/off):
    NOTIFY_MACRODROID   "true"/"false" (default "true"); needs MACRODROID_WEBHOOK_URL
    NOTIFY_EMAIL        "true"/"false" (default "false"); needs SMTP_* + EMAIL_TO

See README.md for the full environment-variable reference.

CLI flags:
    --dry-run     fetch + print, never post, never write state
    --force       notify even if the rate has not changed
    --email-test  build+send a test email now (respects --dry-run = build only)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- Configuration -----------------------------------------------------------

PRIMARY_URL = "https://venus.hnb.lk/api/get_rates_contents_web"
FALLBACK_URL = "https://venus.hnb.lk/api/get_exchange_rates_contents_web"

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_ROOT, "data")
LATEST_FILE = os.path.join(DATA_DIR, "latest.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
DAILY_FILE = os.path.join(DATA_DIR, "daily.csv")
PREDICTIONS_FILE = os.path.join(DATA_DIR, "predictions.jsonl")
METRICS_FILE = os.path.join(DATA_DIR, "prediction_metrics.json")
NOTIFY_STATE_FILE = os.path.join(DATA_DIR, "notify_state.json")

CURRENCY_CODE = os.environ.get("CURRENCY_CODE", "USD").upper()
USER_AGENT = "tt-rate-tracker/2.0 (+https://github.com/)"
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

# Sri Lanka is UTC+5:30 (no DST), so an SL "business day" is a stable UTC offset.
SL_OFFSET = dt.timedelta(hours=5, minutes=30)

DAILY_HEADER = ["date", "dow", "buy_open", "buy_close", "buy_high", "buy_low",
                "sell_close", "spread", "num_changes"]

# 80% one-sided z for the uncertainty band.
BAND_Z = 1.2816
# Rolling window (resolved predictions) for live error metrics.
METRIC_WINDOW = 50


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- Time helpers ------------------------------------------------------------

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def sl_date(when_utc: dt.datetime) -> dt.date:
    return (when_utc + SL_OFFSET).date()


def next_business_day(d: dt.date) -> dt.date:
    nd = d + dt.timedelta(days=1)
    while nd.weekday() >= 5:  # 5=Sat, 6=Sun
        nd += dt.timedelta(days=1)
    return nd


# --- HTTP helpers ------------------------------------------------------------

def _get_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as err:
            last_err = err
            print(f"[warn] fetch attempt {attempt}/{HTTP_RETRIES} failed: {err}",
                  file=sys.stderr)
            if attempt < HTTP_RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_err}")


def fetch_rate() -> dict:
    """Return {'currency_code', 'buy', 'sell', 'spread', 'updated_on'}."""
    for url in (PRIMARY_URL, FALLBACK_URL):
        try:
            data = _get_json(url)
        except RuntimeError as err:
            print(f"[warn] {err}", file=sys.stderr)
            continue

        rows = data.get("ex") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            continue

        for row in rows:
            if str(row.get("currencyCode", "")).upper() == CURRENCY_CODE:
                buy = row.get("buyingRate")
                sell = row.get("sellingRate")
                if buy is None or sell is None:
                    continue
                buy, sell = float(buy), float(sell)
                return {
                    "currency_code": CURRENCY_CODE,
                    "buy": buy,
                    "sell": sell,
                    "spread": round(sell - buy, 4),
                    "updated_on": row.get("updated_on"),
                    "source": url,
                }
    raise RuntimeError(f"{CURRENCY_CODE} rate not found in HNB API response.")


# --- State: latest + event log -----------------------------------------------

def load_latest() -> dict | None:
    try:
        with open(LATEST_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def has_changed(latest: dict | None, current: dict) -> bool:
    # Trigger ONLY on a change in the BUY rate. Sell-only moves don't fire an
    # event; the sell price is still shown in every notification.
    if latest is None:
        return True
    return latest.get("tt_buy") != current["buy"]


def build_record(rate: dict, changed: bool) -> dict:
    message = (f"HNB {rate['currency_code']} TT rate — "
               f"Buy {rate['buy']:.2f} / Sell {rate['sell']:.2f} LKR")
    return {
        "bank": "HNB",
        "currency": rate["currency_code"],
        "tt_buy": rate["buy"],
        "tt_sell": rate["sell"],
        "spread": rate["spread"],
        "updated_on": rate.get("updated_on"),
        "fetched_at": now_utc().isoformat(),
        "changed": changed,
        "message": message,
    }


def save_state(record: dict) -> None:
    """Persist latest.json and append an enriched change event to history.jsonl."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_FILE, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")
    with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# --- Daily series (the model's training data) --------------------------------

def load_daily() -> list[dict]:
    rows: list[dict] = []
    try:
        with open(DAILY_FILE, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                rows.append(row)
    except FileNotFoundError:
        pass
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def write_daily(rows: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DAILY_FILE, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=DAILY_HEADER)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in DAILY_HEADER})


def _fmt(x: float) -> str:
    # Compact, stable numeric strings so unchanged rows produce no git diff.
    return f"{x:.4f}".rstrip("0").rstrip(".")


def update_daily(rows: list[dict], rate: dict, when_utc: dt.datetime) -> tuple[list[dict], bool]:
    """Upsert today's SL business-day row. Returns (rows, new_day_created)."""
    today = sl_date(when_utc)
    today_s = today.isoformat()
    buy, sell, spread = rate["buy"], rate["sell"], rate["spread"]
    by_date = {r["date"]: r for r in rows}

    if today_s not in by_date:
        rows.append({
            "date": today_s,
            "dow": today.strftime("%a"),
            "buy_open": _fmt(buy),
            "buy_close": _fmt(buy),
            "buy_high": _fmt(buy),
            "buy_low": _fmt(buy),
            "sell_close": _fmt(sell),
            "spread": _fmt(spread),
            "num_changes": "0",
        })
        rows.sort(key=lambda r: r["date"])
        return rows, True

    row = by_date[today_s]
    prev_close = float(row["buy_close"])
    if buy != prev_close:
        row["num_changes"] = str(int(row.get("num_changes", "0") or "0") + 1)
    row["buy_close"] = _fmt(buy)
    row["buy_high"] = _fmt(max(float(row["buy_high"]), buy))
    row["buy_low"] = _fmt(min(float(row["buy_low"]), buy))
    row["sell_close"] = _fmt(sell)
    row["spread"] = _fmt(spread)
    return rows, False


def daily_closes(rows: list[dict]) -> list[float]:
    out: list[float] = []
    for r in rows:
        try:
            out.append(float(r["buy_close"]))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def prev_business_close(rows: list[dict]) -> float | None:
    """Close of the most recent day BEFORE the last row (for day-over-day delta)."""
    closes = daily_closes(rows)
    return closes[-2] if len(closes) >= 2 else None


# --- Forecaster (dependency-free; see ADR-0001) ------------------------------

def _pred_config() -> dict:
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, "").strip() or default)
        except ValueError:
            return default

    def _i(name: str, default: int) -> int:
        try:
            return int(float(os.environ.get(name, "").strip() or default))
        except ValueError:
            return default

    return {
        "url": os.environ.get("PREDICTION_WEBHOOK_URL", "").strip(),
        "method": os.environ.get("PREDICTION_WEBHOOK_METHOD", "GET").upper(),
        "horizon_days": _i("PREDICTION_HORIZON_DAYS", 1),
        "min_samples": _i("PREDICTION_MIN_SAMPLES", 10),
        "alpha": _f("PREDICTION_EWMA_ALPHA", 0.3),
        "beta": _f("PREDICTION_EWMA_BETA", 0.2),
        "gamma": _f("PREDICTION_HOLT_GAMMA", 0.1),
        "delta_threshold": _f("PREDICTION_DELTA_THRESHOLD", 0.10),
        "notify_every_run": _env_bool("PREDICTION_NOTIFY_EVERY_RUN", False),
    }


def _models_pass(values: list[float], alpha: float, beta: float,
                 gamma: float) -> tuple[dict, dict | None, dict, int]:
    """Single forward (no-lookahead) pass: naive / ewma / holt one-step forecasts,
    their MAE, and per-model EW residual variance. Returns (forecasts, maes, var, n)."""
    level = values[0]
    holt_level = values[0]
    trend = (values[1] - values[0]) if len(values) >= 2 else 0.0
    prev = values[0]
    n = 0
    var = {"naive": 0.0, "ewma": 0.0, "holt": 0.0}
    mae = {"naive": 0.0, "ewma": 0.0, "holt": 0.0}
    # Burn-in so model selection reflects steady-state, not cold-start.
    warm = min(5, max(0, (len(values) - 1) // 4))
    idx = 0
    for v in values[1:]:
        f_naive = prev
        f_ewma = level
        f_holt = holt_level + trend
        if idx >= warm:
            e_naive, e_ewma, e_holt = v - f_naive, v - f_ewma, v - f_holt
            mae["naive"] += abs(e_naive)
            mae["ewma"] += abs(e_ewma)
            mae["holt"] += abs(e_holt)
            var["naive"] = beta * e_naive * e_naive + (1 - beta) * var["naive"]
            var["ewma"] = beta * e_ewma * e_ewma + (1 - beta) * var["ewma"]
            var["holt"] = beta * e_holt * e_holt + (1 - beta) * var["holt"]
            n += 1
        level = alpha * v + (1 - alpha) * level
        prev_holt = holt_level
        holt_level = alpha * v + (1 - alpha) * (holt_level + trend)
        trend = gamma * (holt_level - prev_holt) + (1 - gamma) * trend
        prev = v
        idx += 1

    forecasts = {"naive": values[-1], "ewma": level, "holt": holt_level + trend}
    maes = {k: mae[k] / n for k in mae} if n else None
    return forecasts, maes, var, n


def predict_next_buy(closes: list[float], current_buy: float, cfg: dict) -> dict:
    """Forecast the next business day's BUY close, with band + chosen model."""
    values = list(closes)
    if not values or values[-1] != current_buy:
        values = values + [current_buy]

    if len(values) < cfg["min_samples"]:
        return {"model": "warmup", "predicted_buy": round(current_buy, 4),
                "sd": 0.0, "band_low": round(current_buy, 4),
                "band_high": round(current_buy, 4), "confidence": 0.0,
                "samples": len(values)}

    forecasts, maes, var, n = _models_pass(
        values, cfg["alpha"], cfg["beta"], cfg["gamma"])
    model = min(maes, key=lambda k: maes[k]) if (maes and n >= 2) else "naive"
    sd = math.sqrt(max(var.get(model, 0.0), 0.0))
    predicted = forecasts[model]
    delta = predicted - current_buy
    ratio = abs(delta) / (sd + 1e-9)
    confidence = round(ratio / (1.0 + ratio), 4)

    return {"model": model, "predicted_buy": round(predicted, 4), "sd": round(sd, 4),
            "band_low": round(predicted - BAND_Z * sd, 4),
            "band_high": round(predicted + BAND_Z * sd, 4),
            "confidence": confidence, "samples": len(values)}


# --- Feedback loop: log, reconcile, score ------------------------------------

def load_predictions() -> list[dict]:
    preds: list[dict] = []
    try:
        with open(PREDICTIONS_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        preds.append(json.loads(line))
                    except ValueError:
                        continue
    except FileNotFoundError:
        pass
    return preds


def write_predictions(preds: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PREDICTIONS_FILE, "w", encoding="utf-8") as fh:
        for p in preds:
            fh.write(json.dumps(p) + "\n")


def reconcile_predictions(preds: list[dict], daily_by_date: dict) -> bool:
    """Resolve predictions whose target business day now has a known close."""
    changed = False
    for p in preds:
        if p.get("resolved"):
            continue
        td = p.get("target_date")
        if td and td in daily_by_date:
            actual = float(daily_by_date[td]["buy_close"])
            base = float(p.get("current_buy", actual))
            predicted = float(p.get("predicted_buy", base))
            p["resolved"] = True
            p["actual_buy"] = actual
            p["abs_error"] = round(abs(actual - predicted), 4)
            p["naive_abs_error"] = round(abs(actual - base), 4)
            changed = True
    return changed


def compute_metrics(preds: list[dict]) -> dict:
    resolved = [p for p in preds if p.get("resolved")]
    window = resolved[-METRIC_WINDOW:]
    n = len(window)
    if n == 0:
        return {"resolved": 0, "window": 0, "mae": None, "naive_mae": None,
                "directional_hit_rate": None}
    mae = sum(float(p.get("abs_error", 0.0)) for p in window) / n
    naive_mae = sum(float(p.get("naive_abs_error", 0.0)) for p in window) / n
    directional = [p for p in window
                   if float(p.get("predicted_buy", 0)) != float(p.get("current_buy", 0))]
    hits = 0
    for p in directional:
        pd = float(p["predicted_buy"]) - float(p["current_buy"])
        ad = float(p.get("actual_buy", p["current_buy"])) - float(p["current_buy"])
        if (pd > 0 and ad > 0) or (pd < 0 and ad < 0):
            hits += 1
    return {"resolved": len(resolved), "window": n, "mae": round(mae, 4),
            "naive_mae": round(naive_mae, 4),
            "directional_hit_rate": round(hits / len(directional), 4) if directional else None}


def save_metrics(metrics: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(METRICS_FILE, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2)
        fh.write("\n")


def decide_signal(prediction: dict, current_buy: float, metrics: dict, cfg: dict) -> dict:
    """Map forecast + live error to an actionable, confidence-aware flag.

    USD/LKR (BUY rate = bank buys USD):
      rate predicted to RISE -> USD getting more expensive -> BUY soon
      rate predicted to FALL -> USD getting cheaper        -> SELL / wait
    A drift guard downgrades strong flags to WATCH when recent error is high.
    """
    if prediction["model"] == "warmup":
        return {"signal": "WARMUP",
                "reason": f"collecting data ({prediction['samples']} samples)"}
    delta = prediction["predicted_buy"] - current_buy
    sd = prediction["sd"]
    threshold = cfg["delta_threshold"]
    strong = abs(delta) >= threshold and abs(delta) >= sd
    mae = metrics.get("mae")
    drifting = mae is not None and mae > 2.0 * threshold
    if not strong:
        return {"signal": "HOLD", "reason": f"move {delta:+.2f} within noise/threshold"}
    if drifting:
        return {"signal": "WATCH", "reason": f"signal {delta:+.2f} but recent error high (mae={mae})"}
    if delta > 0:
        return {"signal": "BUY", "reason": f"USD likely to rise {delta:+.2f} LKR (conf {prediction['confidence']})"}
    return {"signal": "SELL", "reason": f"USD likely to fall {delta:+.2f} LKR (conf {prediction['confidence']})"}


def run_prediction_cycle(rate: dict, when_utc: dt.datetime, daily_rows: list[dict],
                         new_day: bool, dry_run: bool, cfg: dict) -> dict:
    """Reconcile due predictions, forecast next business day, emit a signal, and
    (optionally) post the prediction webhook. Fully self-contained; callers wrap
    this in try/except so it can never break the core alert."""
    current_buy = rate["buy"]
    by_date = {r["date"]: r for r in daily_rows}

    preds = load_predictions()
    resolved_changed = reconcile_predictions(preds, by_date)
    metrics = compute_metrics(preds)

    pred = predict_next_buy(daily_closes(daily_rows), current_buy, cfg)
    decision = decide_signal(pred, current_buy, metrics, cfg)
    target = next_business_day(sl_date(when_utc))

    record = {
        "currency": rate["currency_code"],
        "date_made": sl_date(when_utc).isoformat(),
        "target_date": target.isoformat(),
        "horizon_days": cfg["horizon_days"],
        "current_buy": round(current_buy, 4),
        "predicted_buy": pred["predicted_buy"],
        "model": pred["model"],
        "band_low": pred["band_low"],
        "band_high": pred["band_high"],
        "sd": pred["sd"],
        "confidence": pred["confidence"],
        "delta": round(pred["predicted_buy"] - current_buy, 4),
        "signal": decision["signal"],
        "reason": decision["reason"],
        "samples": pred["samples"],
        "recent_mae": metrics.get("mae"),
        "directional_hit_rate": metrics.get("directional_hit_rate"),
        "generated_at": when_utc.isoformat(),
        "resolved": False,
    }

    print(f"[predict] {record['signal']}: buy {current_buy:.2f} -> "
          f"{pred['predicted_buy']:.2f} (model={pred['model']}, "
          f"conf={pred['confidence']}, horizon={cfg['horizon_days']}d) — {decision['reason']}")

    if dry_run:
        return record

    # Persist feedback-loop state, but only when something actually changed, so we
    # don't create a git commit on every poll (Issue #2). A new prediction is
    # logged at most once per business day (the day's first run).
    if resolved_changed:
        write_predictions(preds)
        save_metrics(metrics)
    if new_day and pred["model"] != "warmup":
        with open(PREDICTIONS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        save_metrics(metrics)

    actionable = record["signal"] in ("BUY", "SELL")
    if cfg["url"] and pred["model"] != "warmup" and (cfg["notify_every_run"] or actionable):
        post_prediction_webhook(record, cfg)
    return record


# --- Notifications: MacroDroid -----------------------------------------------

def _post_url(url: str, params: dict, method: str, json_body: dict | None = None) -> bool:
    try:
        if method == "POST":
            body = json.dumps(json_body if json_body is not None else params).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"User-Agent": USER_AGENT,
                                                  "Content-Type": "application/json"})
        else:
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            req = urllib.request.Request(f"{url}{sep}{urllib.parse.urlencode(params)}",
                                         headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = resp.getcode()
        print(f"[ok] {method} -> HTTP {status} ({urllib.parse.urlparse(url).netloc})")
        return 200 <= status < 300
    except (urllib.error.URLError, urllib.error.HTTPError) as err:
        print(f"[error] request to {urllib.parse.urlparse(url).netloc} failed: {err}",
              file=sys.stderr)
        return False


def notify_macrodroid(record: dict, prediction: dict | None) -> bool:
    url = os.environ.get("MACRODROID_WEBHOOK_URL", "").strip()
    if not url:
        print("[warn] MACRODROID_WEBHOOK_URL not set — skipping MacroDroid.", file=sys.stderr)
        return False
    method = os.environ.get("WEBHOOK_METHOD", "GET").upper()
    params = {
        "buy": f"{record['tt_buy']:.2f}",
        "sell": f"{record['tt_sell']:.2f}",
        "spread": f"{record['spread']:.2f}",
        "currency": record["currency"],
        "updated_on": record["updated_on"] or "",
        "changed": str(record["changed"]).lower(),
        "message": record["message"],
    }
    if prediction and prediction.get("model") != "warmup":
        params["signal"] = prediction["signal"]
        params["predicted_buy"] = f"{prediction['predicted_buy']:.2f}"
    secret = os.environ.get("WEBHOOK_SHARED_SECRET", "").strip()
    if secret:
        params["token"] = secret
    return _post_url(url, params, method, json_body={**record, "prediction": prediction})


def post_prediction_webhook(record: dict, cfg: dict) -> bool:
    params = {
        "currency": record["currency"], "signal": record["signal"],
        "current_buy": f"{record['current_buy']:.2f}",
        "predicted_buy": f"{record['predicted_buy']:.2f}",
        "delta": f"{record['delta']:+.2f}", "confidence": f"{record['confidence']}",
        "horizon_days": str(record["horizon_days"]), "model": record["model"],
        "band_low": f"{record['band_low']:.2f}", "band_high": f"{record['band_high']:.2f}",
        "message": (f"USD {record['signal']} — buy {record['current_buy']:.2f} -> "
                    f"{record['predicted_buy']:.2f} ({record['delta']:+.2f}) "
                    f"conf {record['confidence']}"),
    }
    secret = os.environ.get("WEBHOOK_SHARED_SECRET", "").strip()
    if secret:
        params["token"] = secret
    return _post_url(cfg["url"], params, cfg["method"], json_body=record)


# --- Notifications: HTML email (stdlib smtplib) ------------------------------

_SIGNAL_COLOR = {"BUY": "#15803d", "SELL": "#b91c1c", "HOLD": "#475569",
                 "WATCH": "#b45309", "WARMUP": "#64748b"}
# kind -> (header subtitle, header background colour)
_KIND_META = {
    "change": ("Rate changed", "#0f172a"),
    "signal": ("Forecast signal", "#1e3a8a"),
    "digest": ("Daily summary", "#334155"),
}


def build_email(record: dict, prediction: dict | None, prev_close: float | None,
                kind: str = "change") -> tuple[str, str, str]:
    """Return (subject, html_body, text_body) for the given email kind:
    'change' (rate moved), 'signal' (actionable BUY/SELL), or 'digest' (daily)."""
    buy, sell, spread = record["tt_buy"], record["tt_sell"], record["spread"]
    cur = record["currency"]
    day_delta = (buy - prev_close) if prev_close is not None else None
    arrow = "" if day_delta is None else ("▲" if day_delta > 0 else ("▼" if day_delta < 0 else "→"))
    dcolor = "#475569" if not day_delta else ("#15803d" if day_delta > 0 else "#b91c1c")
    dtxt = "" if day_delta is None else f"{arrow} {day_delta:+.2f} vs prev close"

    sig = (prediction or {}).get("signal", "WARMUP")
    scolor = _SIGNAL_COLOR.get(sig, "#64748b")
    subtitle, accent = _KIND_META.get(kind, _KIND_META["change"])
    actionable = prediction and prediction.get("model") != "warmup"
    if kind == "signal" and actionable:
        subject = (f"USD {sig} signal — buy {buy:.2f} → "
                   f"{prediction['predicted_buy']:.2f} ({prediction['delta']:+.2f})")
    elif kind == "digest":
        subject = f"HNB {cur} TT daily — Buy {buy:.2f} · {sig}"
    else:
        subject = (f"HNB {cur} TT: Buy {buy:.2f}"
                   + (f" ({arrow}{abs(day_delta):.2f})" if day_delta else "") + f" · {sig}")

    banner_html = ""
    if kind == "signal" and actionable:
        verb = {"BUY": "Buy USD soon", "SELL": "Hold off / sell"}.get(sig, sig)
        banner_html = f"""
    <tr><td style="padding:20px 20px 0;">
      <div style="background:{scolor};color:#fff;border-radius:10px;padding:14px 16px;">
        <div style="font-size:22px;font-weight:800;">{sig} · {verb}</div>
        <div style="font-size:13px;opacity:.92;margin-top:2px;">next day {prediction['predicted_buy']:.2f} ({prediction['delta']:+.2f} LKR) · confidence {prediction['confidence']}</div>
      </div>
    </td></tr>"""

    pred_html = ""
    if prediction and prediction.get("model") != "warmup":
        pred_html = f"""
        <tr><td style="padding:16px 20px;">
          <div style="font-size:12px;letter-spacing:.06em;color:#64748b;text-transform:uppercase;margin-bottom:8px;">Next business day forecast</div>
          <div style="display:inline-block;background:{scolor};color:#fff;font-weight:700;border-radius:6px;padding:4px 12px;font-size:15px;">{sig}</div>
          <div style="margin-top:12px;font-size:14px;color:#334155;line-height:1.6;">
            Predicted buy: <b>{prediction['predicted_buy']:.2f}</b> ({prediction['delta']:+.2f}) ·
            band {prediction['band_low']:.2f}–{prediction['band_high']:.2f}<br>
            model <b>{prediction['model']}</b> · confidence {prediction['confidence']} ·
            samples {prediction['samples']}<br>
            <span style="color:#64748b;">{prediction['reason']}</span>
          </div>
        </td></tr>
        <tr><td style="padding:0 20px 4px;font-size:12px;color:#94a3b8;">
            live accuracy — MAE {prediction.get('recent_mae')} · hit-rate {prediction.get('directional_hit_rate')}
        </td></tr>"""
    elif prediction:
        pred_html = f"""
        <tr><td style="padding:16px 20px;font-size:14px;color:#64748b;">
          Forecast warming up — {prediction['reason']}.
        </td></tr>"""

    html = f"""\
<div style="background:#f1f5f9;padding:24px 0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
  <table role="presentation" width="440" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);">
    <tr><td style="background:{accent};padding:18px 20px;color:#fff;">
      <div style="font-size:18px;font-weight:700;">🇱🇰 HNB {cur} → LKR · TT Rate</div>
      <div style="font-size:12px;color:#cbd5e1;margin-top:2px;">{subtitle} · {sl_date(now_utc()).strftime('%A, %d %b %Y')}</div>
    </td></tr>
    {banner_html}
    <tr><td style="padding:20px;">
      <table role="presentation" width="100%"><tr>
        <td style="width:50%;">
          <div style="font-size:12px;color:#64748b;">BUY (bank buys USD)</div>
          <div style="font-size:30px;font-weight:800;color:#0f172a;">{buy:.2f}</div>
        </td>
        <td style="width:50%;">
          <div style="font-size:12px;color:#64748b;">SELL (bank sells USD)</div>
          <div style="font-size:30px;font-weight:800;color:#0f172a;">{sell:.2f}</div>
        </td>
      </tr></table>
      <div style="margin-top:10px;font-size:13px;color:{dcolor};font-weight:600;">{dtxt}</div>
      <div style="margin-top:4px;font-size:12px;color:#94a3b8;">spread {spread:.2f} LKR</div>
    </td></tr>
    {pred_html}
    <tr><td style="background:#f8fafc;padding:12px 20px;font-size:11px;color:#94a3b8;line-height:1.5;">
      Source: venus.hnb.lk · fetched {record['fetched_at']} · automated, not financial advice.
    </td></tr>
  </table>
  </td></tr></table>
</div>"""

    text_lines = [f"[{kind.upper()}] {record['message']}", f"spread {spread:.2f} LKR"]
    if dtxt:
        text_lines.append(dtxt)
    if prediction and prediction.get("model") != "warmup":
        text_lines.append(f"Forecast {sig}: next buy {prediction['predicted_buy']:.2f} "
                          f"({prediction['delta']:+.2f}), model {prediction['model']}, "
                          f"conf {prediction['confidence']} — {prediction['reason']}")
    text_lines.append("Source: venus.hnb.lk — automated, not financial advice.")
    return subject, html, "\n".join(text_lines)


def send_email(subject: str, html: str, text: str) -> bool:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT", "465") or "465")
    user = os.environ.get("SMTP_USERNAME", "").strip()
    pw = os.environ.get("SMTP_PASSWORD", "").strip()
    to = os.environ.get("EMAIL_TO", "").strip()
    frm = os.environ.get("EMAIL_FROM", user).strip()
    if not (user and pw and to):
        print("[warn] SMTP_USERNAME / SMTP_PASSWORD / EMAIL_TO not all set — skipping email.",
              file=sys.stderr)
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    try:
        ctx = ssl.create_default_context()
        if port == 465:  # implicit TLS (Gmail)
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=HTTP_TIMEOUT) as s:
                s.login(user, pw)
                s.sendmail(frm, recipients, msg.as_string())
        else:            # STARTTLS (587/2525/25) — Brevo, Mailjet, SMTP2GO, …
            with smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT) as s:
                s.ehlo(); s.starttls(context=ctx); s.ehlo()
                s.login(user, pw)
                s.sendmail(frm, recipients, msg.as_string())
        print(f"[ok] email sent to {len(recipients)} recipient(s) via {host}:{port}")
        return True
    except (smtplib.SMTPException, OSError) as err:
        print(f"[error] email send failed: {err}", file=sys.stderr)
        return False


def notify_email(kind: str, record: dict, prediction: dict | None,
                 prev_close: float | None) -> bool:
    subject, html, text = build_email(record, prediction, prev_close, kind)
    return send_email(subject, html, text)


# --- Email dispatch policy ---------------------------------------------------
# 33 runs/day must not become 33 emails. We send at most ONE email per run,
# deduped per SL day via data/notify_state.json:
#   change : the BUY rate moved today (or --force) — any time
#   signal : forecast turned actionable (BUY/SELL), once per distinct signal/day,
#            only after the morning "settle" time so it reflects today's rate
#   digest : opt-in (EMAIL_DAILY_DIGEST=true) once/day after the settle time

def _notify_state() -> dict:
    try:
        with open(NOTIFY_STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def _save_notify_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(NOTIFY_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


def _settle_time() -> dt.time:
    raw = os.environ.get("EMAIL_DAILY_AFTER_UTC", "05:30").strip()
    try:
        h, m = raw.split(":")
        return dt.time(int(h), int(m), tzinfo=dt.timezone.utc)
    except (ValueError, AttributeError):
        return dt.time(5, 30, tzinfo=dt.timezone.utc)


def select_email_kind(changed: bool, prediction: dict | None,
                      when_utc: dt.datetime, forced: bool) -> str | None:
    """Pick at most one email kind for this run (pure; reads state, no writes)."""
    today = sl_date(when_utc).isoformat()
    st = _notify_state()
    fresh = st.get("date") != today
    sig = (prediction or {}).get("signal")
    actionable = sig in ("BUY", "SELL")
    after_settle = when_utc.timetz() >= _settle_time()

    if (changed or forced) and not (not fresh and st.get("change")):
        return "change"
    if actionable and after_settle and (fresh or st.get("signal_sent") != sig):
        return "signal"
    if (_env_bool("EMAIL_DAILY_DIGEST", False) and after_settle
            and not (not fresh and st.get("digest"))):
        return "digest"
    return None


def mark_email_sent(kind: str, prediction: dict | None, when_utc: dt.datetime) -> None:
    today = sl_date(when_utc).isoformat()
    st = _notify_state()
    if st.get("date") != today:
        st = {"date": today}
    sig = (prediction or {}).get("signal")
    if kind == "change":
        st["change"] = True
        st["signal_sent"] = sig          # the change email already shows the signal
    elif kind == "signal":
        st["signal_sent"] = sig
    elif kind == "digest":
        st["digest"] = True
    _save_notify_state(st)


# --- Main --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="HNB USD TT rate -> notifications")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and print only; do not post or write state")
    parser.add_argument("--force", action="store_true",
                        help="notify even if the rate has not changed")
    parser.add_argument("--email-test", action="store_true",
                        help="build+send a test email now (build-only under --dry-run)")
    args = parser.parse_args()

    when = now_utc()
    rate = fetch_rate()
    latest = load_latest()
    changed = has_changed(latest, rate)
    record = build_record(rate, changed)
    print(f"[info] {record['message']} spread={rate['spread']:.2f} "
          f"(updated_on={record['updated_on']}, changed={changed}, source={rate['source']})")

    # Daily series — maintain in memory; persist only when not a dry-run.
    daily_rows = load_daily()
    prev_close = prev_business_close(daily_rows)
    daily_rows, new_day = update_daily(daily_rows, rate, when)
    if not args.dry_run:
        write_daily(daily_rows)  # identical content => no git diff => no churn
        if changed:
            save_state(record)

    # Prediction + feedback loop — GUARDED so it can never break the core alert.
    cfg = _pred_config()
    prediction = None
    try:
        prediction = run_prediction_cycle(rate, when, daily_rows, new_day, args.dry_run, cfg)
    except Exception as err:  # noqa: BLE001 — predictions must never break alerts
        print(f"[warn] prediction cycle failed (core alert unaffected): {err}", file=sys.stderr)

    # Email test path (setup verification).
    if args.email_test:
        if args.dry_run:
            subj, html, _ = build_email(record, prediction, prev_close, "change")
            print(f"[dry-run][email-test] subject: {subj}  (html {len(html)} bytes, not sent)")
        else:
            notify_email("change", record, prediction, prev_close)

    if args.dry_run:
        print("[dry-run] not posting, not writing state.")
        print(json.dumps(record, indent=2))
        if prediction:
            print(json.dumps(prediction, indent=2))
        return 0

    always = _env_bool("ALWAYS_POST", False)
    should_push = changed or always or args.force
    failures = []

    # MacroDroid push — instant alert on a rate change.
    if should_push and _env_bool("NOTIFY_MACRODROID", True):
        if not notify_macrodroid(record, prediction) and os.environ.get("MACRODROID_WEBHOOK_URL"):
            failures.append("macrodroid")

    # Email — its own policy (change / signal / digest), deduped per SL day, so
    # it can fire on a flat day (e.g. an actionable forecast) without spamming.
    if _env_bool("NOTIFY_EMAIL", False):
        kind = select_email_kind(changed, prediction, when, args.force)
        if kind:
            if notify_email(kind, record, prediction, prev_close):
                mark_email_sent(kind, prediction, when)
            else:
                failures.append("email")
        else:
            print("[info] no email to send this run.")
    elif not should_push:
        print("[info] rate unchanged — nothing to notify.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
