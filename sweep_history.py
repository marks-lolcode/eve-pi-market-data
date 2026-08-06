#!/usr/bin/env python3
# sweep_history.py — v1.0 — Last updated 2026-08-06
#
# Trailing-30-day traded volume and median price per type, from ESI's public
# market history endpoint. One request per type — ~18,800 of them — which is
# precisely why this cannot live in Apps Script (its whole daily UrlFetchApp
# quota is 20,000 calls).
#
# Why it matters enough to be worth 18,800 requests: a bid/ask spread is
# meaningless without knowing whether the item actually trades. Ranking Jita
# spreads WITHOUT this data puts 50B ISK officer modules that sell once a
# fortnight at the top of the list. Real daily volume is the filter that makes
# the ranking usable.
#
# History only changes once a day, so this runs daily while the order sweep
# runs hourly.
#
# Types with no ESI history are OMITTED from the output rather than written as
# zero. "Never traded" and "traded zero today" are different facts, and the
# consumer drops absent types rather than treating them as dead.

import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REGION_ID = int(os.environ.get("REGION_ID", "10000002"))       # The Forge
OUT_DIR = os.environ.get("OUT_DIR", "market")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "8"))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "30"))
MIN_TYPES = int(os.environ.get("MIN_TYPES", "5000"))           # sanity floor
# Fraction of requested types allowed to hard-fail before the run is rejected.
MAX_FAIL_RATIO = float(os.environ.get("MAX_FAIL_RATIO", "0.01"))

BASE = "https://esi.evetech.net/latest/markets/%d/history/" % REGION_ID
USER_AGENT = os.environ.get(
    "ESI_USER_AGENT", "eve-pi-market-data (+https://github.com/marks-lolcode/eve-pi-market-data)"
)

HISTORY_COLUMNS = ["typeID", "avgDailyVolume", "medianPrice"]

# ---------------------------------------------------------------------------
# ESI error limiting.
#
# ESI allows ~100 errors per 60s window across a client and answers with HTTP
# 420 once that is spent. At this volume (~19k requests) a naive sweep trips it
# and then keeps hammering, so retries expire and types drop out silently —
# which is worse than it sounds, because a dropped type is indistinguishable
# from "never traded" downstream, and it quietly removes real items from the
# ranking.
#
# Every response carries X-ESI-Error-Limit-Remain / -Reset. The gate below is
# process-wide: when any thread sees the budget running low (or gets a 420),
# ALL threads park until the window resets. Backing off together is the only
# thing that actually works — per-request retries just re-spend the same budget.
# ---------------------------------------------------------------------------

_gate_lock = threading.Lock()
_resume_at = 0.0
_throttle_events = 0


def _park_if_throttled():
    """Block while a global backoff window is open."""
    while True:
        with _gate_lock:
            wait = _resume_at - time.time()
        if wait <= 0:
            return
        time.sleep(min(wait, 5))


def _note_limit(headers, throttled=False):
    """Open a global backoff window when ESI's error budget is nearly spent."""
    global _resume_at, _throttle_events
    try:
        remain = int(headers.get("X-ESI-Error-Limit-Remain", "100"))
        reset = int(headers.get("X-ESI-Error-Limit-Reset", "60"))
    except (TypeError, ValueError):
        remain, reset = (0, 60) if throttled else (100, 60)
    if throttled or remain < 20:
        with _gate_lock:
            _resume_at = max(_resume_at, time.time() + reset + 1)
            _throttle_events += 1
        print("  error budget low (remain=%s) — pausing %ss" % (remain, reset + 1), flush=True)


def fetch_history(type_id, retries=5):
    """Trailing-window [typeID, avgDailyVolume, medianPrice], or a failure marker.

    Returns:
      list  -> usable history
      None  -> ESI genuinely has no history for this type (404 or empty series)
      False -> could not be fetched. Counted separately and, past a threshold,
               fails the run. Conflating this with None would let an ESI outage
               masquerade as "the market stopped trading these items".
    """
    url = "%s?type_id=%d&datasource=tranquility" % (BASE, type_id)
    last = None
    for attempt in range(retries):
        _park_if_throttled()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                _note_limit(resp.headers)
                data = json.loads(resp.read().decode("utf-8"))
            if not data:
                return None
            window = data[-WINDOW_DAYS:]
            avg_volume = sum(d["volume"] for d in window) / float(len(window))
            median_price = statistics.median(d["average"] for d in window)
            return [type_id, round(avg_volume, 1), round(median_price, 2)]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            _note_limit(e.headers, throttled=(e.code == 420))
            last = e
            if e.code not in (420, 429, 500, 502, 503, 504):
                break
        except Exception as e:
            last = e
        time.sleep(2 ** attempt)
    print("  type %d FAILED: %s" % (type_id, last), flush=True)
    return False


def main():
    started = time.time()
    snapshot_path = os.path.join(OUT_DIR, "jitaSnapshot.json")
    if not os.path.exists(snapshot_path):
        raise SystemExit("%s missing — run the order sweep first" % snapshot_path)
    with open(snapshot_path) as f:
        type_ids = [row[0] for row in json.load(f)]
    print("types to fetch: %d" % len(type_ids), flush=True)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(fetch_history, type_ids))

    rows = [r for r in results if isinstance(r, list)]
    no_history = sum(1 for r in results if r is None)
    failed = sum(1 for r in results if r is False)

    print("usable=%d noHistory=%d failed=%d throttleEvents=%d" % (
        len(rows), no_history, failed, _throttle_events), flush=True)

    # A failure is not the same as "no history": failures are ESI refusing us,
    # and downstream they are indistinguishable from an untraded item. Past a
    # small threshold that quietly deletes real items from the ranking, so the
    # run is rejected rather than published.
    if failed > MAX_FAIL_RATIO * len(type_ids):
        raise SystemExit(
            "%d of %d types failed to fetch (>%.0f%%) — refusing to publish a holed dataset"
            % (failed, len(type_ids), MAX_FAIL_RATIO * 100)
        )
    if len(rows) < MIN_TYPES:
        raise SystemExit(
            "only %d types with history (floor %d) — refusing to publish" % (len(rows), MIN_TYPES)
        )

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "jitaHistory.json"), "w") as f:
        json.dump(rows, f, separators=(",", ":"))

    meta_path = os.path.join(OUT_DIR, "meta.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except ValueError:
            meta = {}
    meta.update({
        "historyGeneratedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "historyTypes": len(rows),
        "historyRequested": len(type_ids),
        "historyNoData": no_history,
        "historyFailed": failed,
        "historyThrottleEvents": _throttle_events,
        "historyWindowDays": WINDOW_DAYS,
        "historyColumns": HISTORY_COLUMNS,
        "historySeconds": round(time.time() - started, 1),
    })
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    size = os.path.getsize(os.path.join(OUT_DIR, "jitaHistory.json"))
    print("history types=%d of %d requested bytes=%d seconds=%.1f" % (
        len(rows), len(type_ids), size, time.time() - started), flush=True)


if __name__ == "__main__":
    sys.exit(main())
