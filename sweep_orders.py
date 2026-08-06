#!/usr/bin/env python3
# sweep_orders.py — v1.0 — Last updated 2026-08-06
#
# Sweeps ESI's region-wide market orders endpoint for The Forge, keeps only the
# orders sitting at one station (Jita 4-4 by default), and reduces them to one
# row per type.
#
# Why this runs here and not in Apps Script: a full sweep is 405 requests and
# ~93MB. Apps Script would fight its 6-minute cap, risk an OOM holding 400k
# order objects, and burn 405 of its 20,000 daily UrlFetchApp calls per run.
# Same reasoning as the sibling eve-pi-sde-data repo, which pre-filters CCP's
# SDE zip for the same reason.
#
# Output is an ARRAY OF ARRAYS, not array-of-objects: 0.73MB vs 2.2MB for
# 18,849 types, and the sheet reads it positionally anyway. The column order is
# published in meta.json so the consumer never has to guess it.
#
# Partial sweeps are NOT published. ESI regenerates this data every 300s, so a
# sweep that dropped pages would splice two different market states into one
# "snapshot" — producing crossed books and phantom spreads on exactly the
# thin-market items a spread scanner surfaces. Failing loudly is the safe move.

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REGION_ID = int(os.environ.get("REGION_ID", "10000002"))       # The Forge
STATION_ID = int(os.environ.get("STATION_ID", "60003760"))     # Jita IV-4
OUT_DIR = os.environ.get("OUT_DIR", "market")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "16"))
MIN_TYPES = int(os.environ.get("MIN_TYPES", "15000"))          # sanity floor

BASE = "https://esi.evetech.net/latest/markets/%d/orders/" % REGION_ID
USER_AGENT = os.environ.get(
    "ESI_USER_AGENT", "eve-pi-market-data (+https://github.com/marks-lolcode/eve-pi-market-data)"
)

# Column order for jitaSnapshot.json rows. Published in meta.json — the consumer
# maps by this list, so appending a column here is a non-breaking change.
SNAPSHOT_COLUMNS = [
    "typeID",
    "maxBuy",
    "minSell",
    "buyVolume",
    "sellVolume",
    "buyOrders",
    "sellOrders",
    "topBuyVolume",
    "topSellVolume",
]


def fetch_page(page, retries=4):
    """Fetch one page of orders. Returns (orders, x_pages).

    Raises on any page that could not be retrieved. That is deliberate: an
    unretrievable page must fail the whole job rather than quietly shrink the
    snapshot (see the partial-sweep note in the module docstring). The one
    exception is a 404, which is what ESI returns for a trailing page when
    X-Pages shrinks mid-sweep — that is an empty page, not a failure.
    """
    url = "%s?order_type=all&page=%d&datasource=tranquility" % (BASE, page)
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                pages = int(resp.headers.get("X-Pages", "1"))
                return json.loads(resp.read().decode("utf-8")), pages
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return [], None
            last = e
            if e.code not in (420, 429, 500, 502, 503, 504):
                break
        except Exception as e:  # timeouts, connection resets
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError("page %d failed: %s" % (page, last))


def accumulate(acc, orders):
    """Reduce one page into the per-type accumulator. Station filter applied here."""
    kept = 0
    for o in orders:
        if o["location_id"] != STATION_ID:
            continue
        kept += 1
        t = o["type_id"]
        e = acc.get(t)
        if e is None:
            # [maxBuy, minSell, buyVol, sellVol, buyCount, sellCount, topBuyVol, topSellVol]
            e = [0.0, 0.0, 0, 0, 0, 0, 0, 0]
            acc[t] = e
        price = o["price"]
        remain = o["volume_remain"]
        if o["is_buy_order"]:
            e[2] += remain
            e[4] += 1
            if price > e[0]:
                e[0] = price
                e[6] = remain      # new best bid — reset the top-of-book volume
            elif price == e[0]:
                e[6] += remain     # tied at best bid — accumulate
        else:
            e[3] += remain
            e[5] += 1
            if e[1] == 0.0 or price < e[1]:
                e[1] = price
                e[7] = remain
            elif price == e[1]:
                e[7] += remain
    return kept


def main():
    started = time.time()
    acc = {}
    orders_seen = 0
    orders_kept = 0

    first, pages = fetch_page(1)
    if not pages:
        raise SystemExit("no X-Pages on page 1 — aborting")
    orders_seen += len(first)
    orders_kept += accumulate(acc, first)
    print("X-Pages: %d" % pages, flush=True)

    # Any page that cannot be fetched raises out of pool.map and kills the job —
    # that IS the partial-sweep guard. pages_empty counts only the benign 404
    # case (X-Pages shrank mid-sweep), which is expected and not a failure.
    remaining = list(range(2, pages + 1))
    pages_empty = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for result in pool.map(lambda p: fetch_page(p)[0], remaining):
            if not result:
                pages_empty += 1
                continue
            orders_seen += len(result)
            orders_kept += accumulate(acc, result)

    if len(acc) < MIN_TYPES:
        raise SystemExit(
            "only %d types (floor %d) — refusing to publish a suspect sweep" % (len(acc), MIN_TYPES)
        )

    rows = [[t] + acc[t] for t in sorted(acc)]

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "jitaSnapshot.json"), "w") as f:
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
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "regionID": REGION_ID,
        "stationID": STATION_ID,
        "pages": pages,
        "pagesEmpty": pages_empty,
        "ordersSeen": orders_seen,
        "ordersKept": orders_kept,
        "types": len(rows),
        "snapshotColumns": SNAPSHOT_COLUMNS,
        "sweepSeconds": round(time.time() - started, 1),
    })
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    size = os.path.getsize(os.path.join(OUT_DIR, "jitaSnapshot.json"))
    print("types=%d ordersSeen=%d ordersKept=%d (%.1f%%) bytes=%d seconds=%.1f" % (
        len(rows), orders_seen, orders_kept,
        100.0 * orders_kept / orders_seen if orders_seen else 0,
        size, time.time() - started), flush=True)


if __name__ == "__main__":
    sys.exit(main())
