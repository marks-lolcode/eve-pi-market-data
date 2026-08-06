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
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REGION_ID = int(os.environ.get("REGION_ID", "10000002"))       # The Forge
OUT_DIR = os.environ.get("OUT_DIR", "market")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "16"))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "30"))
MIN_TYPES = int(os.environ.get("MIN_TYPES", "5000"))           # sanity floor

BASE = "https://esi.evetech.net/latest/markets/%d/history/" % REGION_ID
USER_AGENT = os.environ.get(
    "ESI_USER_AGENT", "eve-pi-market-data (+https://github.com/marks-lolcode/eve-pi-market-data)"
)

HISTORY_COLUMNS = ["typeID", "avgDailyVolume", "medianPrice"]


def fetch_history(type_id, retries=4):
    """Trailing-window {avgDailyVolume, medianPrice} for one type, or None.

    None means "ESI has no history for this type" — a 404, or an empty series.
    Unlike the order sweep, a hard failure here degrades rather than aborts:
    one missing type drops one row from a ranking, where a missing ORDER page
    would silently corrupt every spread in the snapshot.
    """
    url = "%s?type_id=%d&datasource=tranquility" % (BASE, type_id)
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
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
            last = e
            if e.code not in (420, 429, 500, 502, 503, 504):
                break
        except Exception as e:
            last = e
        time.sleep(2 ** attempt)
    print("  type %d failed: %s" % (type_id, last), flush=True)
    return None


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

    rows = [r for r in results if r is not None]
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
