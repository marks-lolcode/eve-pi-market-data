#!/usr/bin/env python3
# sweep_orders.py — v1.2 — Last updated 2026-08-07
#
# v1.2: also emits jitaDepth.json — the best DEPTH_LEVELS price levels per side
# per type. The collapsed snapshot says what the best price is and how much
# sits AT it, which is enough to rank a spread but not to size a trade: buy 500
# units of something whose best ask covers 3 and the realised cost is nowhere
# near minSell. Consumers that size quantities have to walk a ladder.
#
# The ladder aggregates by PRICE, not by order, so `min_volume` cannot survive
# it. Rather than pretend a min_volume:5000 order is freely fillable — an
# OPTIMISTIC error, and everything else here leans pessimistic — buy volume is
# counted only from orders with min_volume == 1, and the discarded total is
# published as depthMinVolumeExcluded so the loss is visible. Sell orders are
# effectively always min_volume 1.
#
# v1.1: emits topBuyAgeDays / topSellAgeDays — how long the best prices have
# stood. Every order carries an exact `issued` timestamp, and EVE re-stamps it
# when an order is REPRICED, so the age of the top orders measures undercut
# activity rather than just new listings. That is what tells you whether a large
# order will fill before somebody outbids you: measured across the real book,
# Tritanium's top bids are ~2 hours old while the median type's are 16 days.
# Computable from a single snapshot, so no cross-run state.
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

import datetime
import heapq
import json
import os
import statistics
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
# Orders per side whose ages are averaged into the "how long has the top of book
# stood" figure. Five rather than one: a single re-listed order at the top would
# otherwise make an otherwise dead book look churny.
TOP_ORDERS_FOR_AGE = int(os.environ.get("TOP_ORDERS_FOR_AGE", "5"))
# Price levels published per side. Twenty covers essentially every book here:
# ~326k orders spread over ~18.8k types is ~17 orders per type across BOTH
# sides, and levels aggregate below that. Only mineral-scale books run deeper,
# and a truncated ladder makes the consumer pessimistic (unfilled remainder),
# which is the safe direction.
DEPTH_LEVELS = int(os.environ.get("DEPTH_LEVELS", "20"))

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
    "topBuyAgeDays",
    "topSellAgeDays",
]

# Column order for jitaDepth.json. A SEPARATE file rather than two more columns
# on the snapshot: every market feature imports the snapshot, and most of them
# never size a quantity. Keeping the ladders out means the station-trading path
# stays exactly as fast as it is today, and a consumer that has no use for depth
# can ignore the file entirely — the same graceful-degrade shape jitaHistory
# already has.
DEPTH_COLUMNS = ["typeID", "buyLadder", "sellLadder"]


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


def order_age_days(issued, now_ts):
    """Age of an order in days, from its ISO `issued` stamp."""
    # fromisoformat does not accept a trailing Z before 3.11; strip it either way.
    return (now_ts - datetime.datetime.fromisoformat(
        issued.replace("Z", "+00:00")).timestamp()) / 86400.0


def accumulate(acc, orders, now_ts):
    """Reduce one page into the per-type accumulator. Station filter applied here.

    Top-of-book ages are kept in bounded heaps rather than by retaining the
    orders: `buyAges` is a MIN-heap holding the TOP_ORDERS_FOR_AGE highest bids,
    `sellAges` a max-heap (negated) holding the lowest asks. Memory stays flat at
    a handful of tuples per type no matter how deep the book is.

    The v1.2 ladders are the one place that does retain something per price —
    but they aggregate INTO A DICT KEYED BY PRICE, so the memory is proportional
    to distinct price levels, not to orders. Across the whole book that is
    bounded by the ~326k orders we keep, and in practice far below it.

    @return (kept, minVolumeExcluded) — units of buy volume dropped from the
        ladder because the order carried a min_volume above 1. Those orders
        still count toward buyVolume/buyOrders/maxBuy: they are real orders and
        the collapsed aggregates never claimed to be fillable in any quantity.
    """
    kept = 0
    min_vol_excluded = 0
    for o in orders:
        if o["location_id"] != STATION_ID:
            continue
        kept += 1
        t = o["type_id"]
        e = acc.get(t)
        if e is None:
            # [maxBuy, minSell, buyVol, sellVol, buyCount, sellCount,
            #  topBuyVol, topSellVol, buyAgeHeap, sellAgeHeap,
            #  buyLevels{price: vol}, sellLevels{price: vol}]
            e = [0.0, 0.0, 0, 0, 0, 0, 0, 0, [], [], {}, {}]
            acc[t] = e
        price = o["price"]
        remain = o["volume_remain"]
        age = order_age_days(o["issued"], now_ts)
        if o["is_buy_order"]:
            e[2] += remain
            e[4] += 1
            if price > e[0]:
                e[0] = price
                e[6] = remain      # new best bid — reset the top-of-book volume
            elif price == e[0]:
                e[6] += remain     # tied at best bid — accumulate
            heap = e[8]
            if len(heap) < TOP_ORDERS_FOR_AGE:
                heapq.heappush(heap, (price, age))
            elif price > heap[0][0]:
                heapq.heapreplace(heap, (price, age))
            # A min_volume above 1 cannot survive aggregation by price, so the
            # order is left out of the ladder rather than overstating what a
            # seller could actually dump into it.
            if o.get("min_volume", 1) > 1:
                min_vol_excluded += remain
            else:
                e[10][price] = e[10].get(price, 0) + remain
        else:
            e[3] += remain
            e[5] += 1
            if e[1] == 0.0 or price < e[1]:
                e[1] = price
                e[7] = remain
            elif price == e[1]:
                e[7] += remain
            heap = e[9]
            if len(heap) < TOP_ORDERS_FOR_AGE:
                heapq.heappush(heap, (-price, age))
            elif -price > heap[0][0]:
                heapq.heapreplace(heap, (-price, age))
            e[11][price] = e[11].get(price, 0) + remain
    return kept, min_vol_excluded


def pack_ladder(levels, best_first_desc):
    """Best DEPTH_LEVELS price levels as "price:qty|price:qty|…", best first.

    Prices are rounded to 2dp before packing — ESI carries more precision than
    an order can be placed at, and the extra digits are pure payload. Rounding
    can collide two neighbouring levels, so volumes are re-summed AFTER
    rounding: otherwise 4.5100001 and 4.51 would publish as two entries at the
    same printed price, which is not wrong but wastes one of the 20 slots.

    One consequence worth knowing: maxBuy/minSell on the snapshot are NOT
    rounded, so a type whose real spread is sub-cent can show a ladder whose
    best bid equals its best ask. Nothing economic turns on a hundredth of an
    ISK, and the crossed-book check consumers run reads the snapshot's exact
    figures, not the ladder — but do not write a crossed-book assertion against
    ladder prices.
    """
    if not levels:
        return ""
    merged = {}
    for price, vol in levels.items():
        key = round(price, 2)
        merged[key] = merged.get(key, 0) + vol
    top = sorted(merged.items(), key=lambda kv: kv[0], reverse=best_first_desc)[:DEPTH_LEVELS]
    # %g would render 4.51 as 4.51 but 1e-05 as 1e-05; the consumer parses with
    # a plain float(), so keep it in plain decimal.
    return "|".join("%s:%d" % (("%.2f" % p).rstrip("0").rstrip("."), v) for p, v in top)


def median_age(heap):
    """Median age of the orders held in a top-of-book heap; 0 when empty."""
    if not heap:
        return 0.0
    return round(statistics.median(age for _price, age in heap), 3)


def main():
    started = time.time()
    acc = {}
    orders_seen = 0
    orders_kept = 0
    # One reference instant for every age, so a 40-second sweep does not make
    # page 1's orders look fractionally older than page 405's.
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    min_vol_excluded = 0

    first, pages = fetch_page(1)
    if not pages:
        raise SystemExit("no X-Pages on page 1 — aborting")
    orders_seen += len(first)
    kept, excluded = accumulate(acc, first, now_ts)
    orders_kept += kept
    min_vol_excluded += excluded
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
            kept, excluded = accumulate(acc, result, now_ts)
            orders_kept += kept
            min_vol_excluded += excluded

    if len(acc) < MIN_TYPES:
        raise SystemExit(
            "only %d types (floor %d) — refusing to publish a suspect sweep" % (len(acc), MIN_TYPES)
        )

    rows = [[t] + acc[t][:8] + [median_age(acc[t][8]), median_age(acc[t][9])]
            for t in sorted(acc)]

    # Ladders come out of the SAME accumulator as the snapshot, so they inherit
    # the partial-sweep guard above rather than needing one of their own. There
    # is deliberately no "publish the snapshot but skip the ladders" path: a
    # snapshot and a ladder file describing different market states is exactly
    # the quietly-corrupt failure this script refuses to produce.
    depth_rows = [[t, pack_ladder(acc[t][10], True), pack_ladder(acc[t][11], False)]
                  for t in sorted(acc)]

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "jitaSnapshot.json"), "w") as f:
        json.dump(rows, f, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "jitaDepth.json"), "w") as f:
        json.dump(depth_rows, f, separators=(",", ":"))

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
        "depthColumns": DEPTH_COLUMNS,
        "depthLevels": DEPTH_LEVELS,
        "depthGeneratedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "depthMinVolumeExcluded": min_vol_excluded,
        "sweepSeconds": round(time.time() - started, 1),
    })
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    size = os.path.getsize(os.path.join(OUT_DIR, "jitaSnapshot.json"))
    depth_size = os.path.getsize(os.path.join(OUT_DIR, "jitaDepth.json"))
    print("types=%d ordersSeen=%d ordersKept=%d (%.1f%%) bytes=%d depthBytes=%d "
          "minVolExcluded=%d seconds=%.1f" % (
              len(rows), orders_seen, orders_kept,
              100.0 * orders_kept / orders_seen if orders_seen else 0,
              size, depth_size, min_vol_excluded, time.time() - started), flush=True)


if __name__ == "__main__":
    sys.exit(main())
