#!/usr/bin/env python3
# build_repro_candidates.py — v1.0 — Last updated 2026-08-07
#
# Narrows the whole Jita book to the types that COULD be worth buying,
# reprocessing, and selling the materials into buy orders.
#
# Why this runs here rather than in the sheet: Apps Script gets 6 minutes per
# execution, and the reprocessing solve needs, per candidate, the input's ask
# ladder plus a bid ladder for each output material. Run over all ~18.8k types
# that does not fit. Run over a few hundred it fits comfortably.
#
# THE SPLIT IS DELIBERATE. This script computes only the part that does not
# depend on the player:
#
#     ceilingProfit = (SUM over materials of quantity x bestBid)
#                     - (bestAsk x portionSize)
#
# That is the profit assuming 100% reprocessing yield and zero taxes — which is
# impossible, so it is a true upper bound. A type whose ceiling is negative
# cannot be made profitable by any skill level, standing or tax rate, so
# dropping it is safe in a way that a tuned threshold never is. Everything that
# DOES depend on the player — real yield, the per-batch floor, sales tax, the
# station's reprocessing tax, order sizing — stays in the sheet where the Config
# knobs and the test suite live.
#
# The bound must use the most optimistic prices on both sides (best bid out,
# best ask in). Walking the ladders here would make it quantity-dependent and it
# would stop being a ceiling.
#
# Reads the snapshot this run just published plus the SDE slice from the sibling
# eve-pi-sde-data repo. Publishing the ceiling itself, not just a pass/fail,
# means a filter that is cutting too hard shows up as a distribution rather than
# as an empty sheet.

import json
import os
import sys
import time
import urllib.request

OUT_DIR = os.environ.get("OUT_DIR", "market")
SDE_BASE = os.environ.get(
    "SDE_BASE", "https://raw.githubusercontent.com/marks-lolcode/eve-pi-sde-data/master/sde/"
)
USER_AGENT = os.environ.get(
    "ESI_USER_AGENT", "eve-pi-market-data (+https://github.com/marks-lolcode/eve-pi-market-data)"
)
# Ore and ice reprocess through different skills and a different formula, and
# the sheet deliberately does not model that path. Category 25 is Asteroid.
EXCLUDED_CATEGORIES = {int(c) for c in os.environ.get("EXCLUDED_CATEGORIES", "25").split(",") if c}
# A ceiling this far below zero is not a near miss worth publishing. Zero keeps
# every type that is even theoretically break-even.
MIN_CEILING_ISK = float(os.environ.get("MIN_CEILING_ISK", "0"))

CANDIDATE_COLUMNS = ["typeID", "portionSize", "bestAsk", "materialValue",
                     "ceilingProfit", "materialCount"]


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_local(name):
    with open(os.path.join(OUT_DIR, name)) as f:
        return json.load(f)


def main():
    started = time.time()

    meta = load_local("meta.json")
    snapshot = load_local("jitaSnapshot.json")
    cols = meta["snapshotColumns"]
    ix = {name: i for i, name in enumerate(cols)}

    # typeID -> (bestBid, bestAsk). Zero on either side means that side of the
    # book is empty, which is different from "cheap" and must not be treated
    # as a price.
    bid = {}
    ask = {}
    for row in snapshot:
        t = row[ix["typeID"]]
        bid[t] = row[ix["maxBuy"]]
        ask[t] = row[ix["minSell"]]

    materials = fetch_json(SDE_BASE + "typeMaterials.json")
    types = fetch_json(SDE_BASE + "types.json")
    groups = fetch_json(SDE_BASE + "groups.json")

    category_of_group = {g["groupID"]: g["categoryID"] for g in groups}
    portion_of = {}
    category_of = {}
    for t in types:
        portion_of[t["typeID"]] = t.get("portionSize") or 1
        category_of[t["typeID"]] = category_of_group.get(t.get("groupID"))

    by_type = {}
    for m in materials:
        by_type.setdefault(m["typeID"], []).append((m["materialTypeID"], m["quantity"]))

    rows = []
    considered = 0
    no_ask = 0
    excluded_category = 0
    for type_id, mats in by_type.items():
        best_ask = ask.get(type_id, 0)
        if not best_ask:
            no_ask += 1          # nothing on sale: cannot buy it at any price
            continue
        if category_of.get(type_id) in EXCLUDED_CATEGORIES:
            excluded_category += 1
            continue
        considered += 1

        # Materials with no bid contribute nothing — you cannot sell into a book
        # that is not there. They are not an error and not a reason to drop the
        # type; the rest of its output may still carry it.
        material_value = 0.0
        for material_id, qty in mats:
            material_value += qty * bid.get(material_id, 0)

        portion = portion_of.get(type_id, 1)
        ceiling = material_value - best_ask * portion
        if ceiling <= MIN_CEILING_ISK:
            continue
        rows.append([type_id, portion, best_ask, round(material_value, 2),
                     round(ceiling, 2), len(mats)])

    # Best ceiling first: if the sheet ever has to truncate, it should truncate
    # the least promising end.
    rows.sort(key=lambda r: -r[4])

    with open(os.path.join(OUT_DIR, "reproCandidates.json"), "w") as f:
        json.dump(rows, f, separators=(",", ":"))

    meta.update({
        "candidateColumns": CANDIDATE_COLUMNS,
        "candidatesGeneratedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "candidatesConsidered": considered,
        "candidatesKept": len(rows),
    })
    with open(os.path.join(OUT_DIR, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    size = os.path.getsize(os.path.join(OUT_DIR, "reproCandidates.json"))
    print("considered=%d kept=%d (%.1f%%) noAsk=%d excludedCategory=%d bytes=%d seconds=%.1f" % (
        considered, len(rows), 100.0 * len(rows) / considered if considered else 0,
        no_ask, excluded_category, size, time.time() - started), flush=True)

    # The whole point of this file is to be much smaller than the book. If it
    # ever stops filtering, the sheet inherits a job it cannot finish inside its
    # 6 minutes — and it would fail as a timeout, which looks like nothing.
    if considered and len(rows) > considered * 0.5:
        print("::warning::candidate filter kept %.0f%% of considered types — "
              "the sheet-side scan may not fit in one Apps Script execution"
              % (100.0 * len(rows) / considered), flush=True)


if __name__ == "__main__":
    sys.exit(main())
