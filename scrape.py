#!/usr/bin/env python3
"""
TT Rate Tracker — fetches HNB's USD TT (Telegraphic Transfer) rate and posts it
to a MacroDroid webhook when it changes.

Data source (official HNB API, no auth / no scraping):
    https://venus.hnb.lk/api/get_rates_contents_web

This is the same endpoint HNB's own website calls to render the exchange-rate
table. It returns clean JSON including an `updated_on` timestamp.

Behaviour:
    * Fetch the latest USD buying/selling rate.
    * Compare against the last value we recorded (data/latest.json).
    * If it changed (or on the very first run), POST it to the MacroDroid webhook
      and append a row to data/history.jsonl (handy for future predictions).
    * If nothing changed, do nothing (so you are not spammed on every poll).

Environment variables:
    MACRODROID_WEBHOOK_URL   (required to actually post) e.g.
                             https://trigger.macrodroid.com/<device-id>/usd_rate
    WEBHOOK_METHOD           GET (default) or POST
    WEBHOOK_SHARED_SECRET    optional token echoed as ?token=... for the macro to verify
    ALWAYS_POST              "true" to post on every run even when unchanged (default: false)
    CURRENCY_CODE            currency to track (default: USD)

CLI flags:
    --dry-run   fetch + print, never post, never write state
    --force     post regardless of whether the rate changed
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration -----------------------------------------------------------

PRIMARY_URL = "https://venus.hnb.lk/api/get_rates_contents_web"
FALLBACK_URL = "https://venus.hnb.lk/api/get_exchange_rates_contents_web"

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_ROOT, "data")
LATEST_FILE = os.path.join(DATA_DIR, "latest.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")

CURRENCY_CODE = os.environ.get("CURRENCY_CODE", "USD").upper()
USER_AGENT = "tt-rate-tracker/1.0 (+https://github.com/)"
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3


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
    """Return {'currency_code', 'buy', 'sell', 'updated_on'} for CURRENCY_CODE."""
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
                return {
                    "currency_code": CURRENCY_CODE,
                    "buy": float(buy),
                    "sell": float(sell),
                    "updated_on": row.get("updated_on"),  # may be None on fallback
                    "source": url,
                }
    raise RuntimeError(f"{CURRENCY_CODE} rate not found in HNB API response.")


# --- State -------------------------------------------------------------------

def load_latest() -> dict | None:
    try:
        with open(LATEST_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def save_state(record: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LATEST_FILE, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")
    with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def has_changed(latest: dict | None, current: dict) -> bool:
    # `latest` is a previously-saved record (tt_buy/tt_sell keys); `current` is a
    # freshly-fetched rate dict (buy/sell keys).
    if latest is None:
        return True
    return (latest.get("tt_buy") != current["buy"]
            or latest.get("tt_sell") != current["sell"])


# --- Webhook -----------------------------------------------------------------

def build_record(rate: dict, changed: bool) -> dict:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    message = (f"HNB {rate['currency_code']} TT rate — "
               f"Buy {rate['buy']:.2f} / Sell {rate['sell']:.2f} LKR")
    return {
        "bank": "HNB",
        "currency": rate["currency_code"],
        "tt_buy": rate["buy"],
        "tt_sell": rate["sell"],
        "updated_on": rate.get("updated_on"),
        "fetched_at": now.isoformat(),
        "changed": changed,
        "message": message,
    }


def post_webhook(record: dict) -> bool:
    url = os.environ.get("MACRODROID_WEBHOOK_URL", "").strip()
    if not url:
        print("[warn] MACRODROID_WEBHOOK_URL not set — skipping webhook post.",
              file=sys.stderr)
        return False

    method = os.environ.get("WEBHOOK_METHOD", "GET").upper()
    params = {
        "buy": f"{record['tt_buy']:.2f}",
        "sell": f"{record['tt_sell']:.2f}",
        "currency": record["currency"],
        "updated_on": record["updated_on"] or "",
        "changed": str(record["changed"]).lower(),
        "message": record["message"],
    }
    secret = os.environ.get("WEBHOOK_SHARED_SECRET", "").strip()
    if secret:
        params["token"] = secret

    try:
        if method == "POST":
            body = json.dumps(record).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"User-Agent": USER_AGENT,
                         "Content-Type": "application/json"})
        else:  # GET with query params (easiest for MacroDroid magic variables)
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            full = f"{url}{sep}{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})

        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = resp.getcode()
        print(f"[ok] webhook {method} -> HTTP {status}")
        return 200 <= status < 300
    except (urllib.error.URLError, urllib.error.HTTPError) as err:
        print(f"[error] webhook post failed: {err}", file=sys.stderr)
        return False


# --- Main --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="HNB USD TT rate -> MacroDroid webhook")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and print only; do not post or write state")
    parser.add_argument("--force", action="store_true",
                        help="post even if the rate has not changed")
    args = parser.parse_args()

    rate = fetch_rate()
    latest = load_latest()
    changed = has_changed(latest, rate)
    record = build_record(rate, changed)

    print(f"[info] {record['message']} (updated_on={record['updated_on']}, "
          f"changed={changed}, source={rate['source']})")

    if args.dry_run:
        print("[dry-run] not posting, not writing state.")
        print(json.dumps(record, indent=2))
        return 0

    always = os.environ.get("ALWAYS_POST", "false").lower() == "true"
    should_post = changed or always or args.force

    if should_post:
        posted = post_webhook(record)
        # Record state on change regardless of webhook outcome, so a transient
        # webhook failure does not cause us to silently lose the change.
        if changed:
            save_state(record)
        if not posted and os.environ.get("MACRODROID_WEBHOOK_URL"):
            return 1  # surface webhook failure as a red workflow run
    else:
        print("[info] rate unchanged — nothing to post.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
