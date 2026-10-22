#!/usr/bin/env python3
# pi_orders_archive.py — v1.0 — Last updated 2026-10-22
#
# Runs the EXISTING hourly sweep (sweep_orders.main, unchanged) and, from the
# SAME fetched pages, also writes an order-level dump of the PI types (P0-P4) at
# Jita 4-4 to  <ARCHIVE_DIR>/orders/YYYY/MM/DD/HH.json.gz .  The workflow then
# appends that file to the append-only `pi-orders-archive` branch.
#
# Why: jitaSnapshot/jitaDepth collapse the book and are overwritten every hour
# (orphan `data` branch, one commit), so nothing records WHICH orders filled or
# were repriced. Consecutive order-level snapshots do: same orderID with a lower
# volumeRemain = a fill; same orderID with a new `issued` = a reprice. That is
# the only way to measure the buy/sell split of traded volume and queue
# half-life for the PI profit program.
#
# NO new ESI calls. sweep_orders.accumulate() is wrapped (not edited) so each
# page it already receives is also filtered for PI orders. The one extra network
# read is the PI type list from the eve-pi-sde-data repo on GitHub (the same
# source build_repro_candidates.py already uses), fetched BEFORE the sweep.
#
# The archive is strictly best-effort: any failure here (SDE fetch, write) is
# logged and swallowed so it can never stop the snapshot from publishing. A
# failed sweep still fails the job exactly as before.
#
# File shape (contract with the analysis ingest):
#   {"generatedAt": ISO, "columns": ARCHIVE_COLUMNS, "rows": [[...], ...]}
# Rows are sorted by (typeID, orderID) so identical books give identical bytes.

import datetime
import gzip
import json
import os
import sys
import urllib.request

import sweep_orders

ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "archive")
SDE_BASE = os.environ.get(
    "SDE_BASE", "https://raw.githubusercontent.com/marks-lolcode/eve-pi-sde-data/master/sde/"
)
USER_AGENT = os.environ.get(
    "ESI_USER_AGENT", "eve-pi-market-data (+https://github.com/marks-lolcode/eve-pi-market-data)"
)
# Sanity band for the PI type set (83 today: P0 15, P1 15, P2 24, P3 21, P4 8).
# Outside it the SDE slice is suspect and the archive is skipped, not guessed.
MIN_PI_TYPES = int(os.environ.get("MIN_PI_TYPES", "60"))
MAX_PI_TYPES = int(os.environ.get("MAX_PI_TYPES", "200"))

ARCHIVE_COLUMNS = ["orderID", "typeID", "isBuy", "price", "volumeRemain",
                   "volumeTotal", "issued", "duration", "minVolume"]


def pi_type_ids(schematics):
    """Every typeID that appears in a planet schematic (inputs and outputs).

    That is exactly P0 (inputs of P1 schematics) through P4. Same definition the
    analysis pipeline uses for its tiers.
    """
    return {t["typeID"] for s in schematics for t in s["types"]}


def order_row(o):
    """One ESI order -> a row in ARCHIVE_COLUMNS order."""
    return [
        o["order_id"],
        o["type_id"],
        bool(o["is_buy_order"]),
        o["price"],
        o["volume_remain"],
        o["volume_total"],
        o["issued"],
        o["duration"],
        o.get("min_volume", 1),
    ]


def filter_pi_rows(orders, type_ids, station_id):
    """PI-only, station-only rows from a page of ESI orders."""
    return [order_row(o) for o in orders
            if o["location_id"] == station_id and o["type_id"] in type_ids]


def build_payload(rows, generated_at):
    return {
        "generatedAt": generated_at,
        "columns": ARCHIVE_COLUMNS,
        "rows": sorted(rows, key=lambda r: (r[1], r[0])),
    }


def archive_path(root, when):
    """<root>/orders/YYYY/MM/DD/HH.json.gz for a UTC datetime."""
    return os.path.join(root, "orders", when.strftime("%Y"), when.strftime("%m"),
                        when.strftime("%d"), when.strftime("%H") + ".json.gz")


def write_archive(root, payload, when):
    path = archive_path(root, when)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    # mtime=0 keeps the gzip header deterministic.
    with open(path, "wb") as f:
        f.write(gzip.compress(raw, 9, mtime=0))
    return path


def load_pi_types():
    url = SDE_BASE + "planetSchematics.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        ids = pi_type_ids(json.loads(resp.read().decode("utf-8")))
    if not (MIN_PI_TYPES <= len(ids) <= MAX_PI_TYPES):
        raise ValueError("PI type count %d outside [%d, %d]" % (len(ids), MIN_PI_TYPES, MAX_PI_TYPES))
    return ids


def main():
    try:
        type_ids = load_pi_types()
        print("pi-archive: %d PI type ids" % len(type_ids), flush=True)
    except Exception as e:  # best effort: never block the snapshot
        type_ids = None
        print("pi-archive: SKIPPED, PI type list unavailable: %s" % e, flush=True)

    collected = []
    original = sweep_orders.accumulate

    failed = []

    def accumulate(acc, orders, now_ts):
        # accumulate() is called from the main thread only (pool.map results are
        # consumed there), so a plain list is safe.
        # Archive collection is best effort: any error here is logged and
        # swallowed so the original sweep is never affected. After a failure
        # collection stops and no (partial) archive is written.
        if type_ids is not None and not failed:
            try:
                collected.extend(filter_pi_rows(orders, type_ids, sweep_orders.STATION_ID))
            except Exception as e:
                failed.append(e)
                print("pi-archive: collection FAILED, archive skipped (snapshot unaffected): %r" % (e,),
                      file=sys.stderr, flush=True)
        return original(acc, orders, now_ts)

    sweep_orders.accumulate = accumulate
    rc = sweep_orders.main()  # raises on any failed page, exactly as before
    if type_ids is None or failed:
        return rc

    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        payload = build_payload(collected, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        path = write_archive(ARCHIVE_DIR, payload, now)
        print("pi-archive: wrote %s rows=%d bytes=%d" % (
            path, len(payload["rows"]), os.path.getsize(path)), flush=True)
    except Exception as e:
        print("pi-archive: write FAILED (snapshot unaffected): %s" % e, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
