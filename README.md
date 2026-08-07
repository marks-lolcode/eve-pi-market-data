# eve-pi-market-data

Preprocessing layer that turns EVE Online's live Jita market into two small JSON
files the [EVE PI Manager v2](https://github.com/marks-lolcode/eve-pi-manager-v2)
Google Apps Script can fetch in one request each.

Sibling of [eve-pi-sde-data](https://github.com/marks-lolcode/eve-pi-sde-data),
which does the same job for CCP's static data export. Both exist for the same
reason: the raw upstream data is far too large for Apps Script to handle.

## Why this repo exists

A full sweep of The Forge's order book is **405 requests and ~93 MB**. Inside
Apps Script that means fighting the 6-minute execution cap, risking an
out-of-memory failure holding 400,000 order objects, and spending 405 of a
20,000/day `UrlFetchApp` quota per run.

Market *history* is worse still: one request per type, ~18,800 types. That is
impossible in Apps Script at any cadence — and it is not optional data. Ranking
bid/ask spreads without knowing what actually trades puts 50-billion-ISK officer
modules that sell once a fortnight at the top of the list.

Here, both sweeps are ordinary work: the order sweep runs in about 35 seconds.

| | In Apps Script | Here |
|---|---|---|
| Order sweep | 405 calls, 93 MB | 1 call, 0.84 MB |
| Whole-market history | not possible | 1 call, ~0.4 MB |

## Published files

Everything lives on the orphan **`data`** branch, force-pushed as a single
commit each run so the repository never accumulates history:

```
https://raw.githubusercontent.com/marks-lolcode/eve-pi-market-data/data/market/jitaSnapshot.json
https://raw.githubusercontent.com/marks-lolcode/eve-pi-market-data/data/market/jitaDepth.json
https://raw.githubusercontent.com/marks-lolcode/eve-pi-market-data/data/market/jitaHistory.json
https://raw.githubusercontent.com/marks-lolcode/eve-pi-market-data/data/market/reproCandidates.json
https://raw.githubusercontent.com/marks-lolcode/eve-pi-market-data/data/market/meta.json
```

`raw.githubusercontent.com` caches for about 5 minutes — append a cache-busting
query parameter if you need a just-published file immediately.

### `jitaSnapshot.json`

One row per type, **array of arrays** (0.84 MB, versus 2.2 MB for the same data
as objects). Column order is published in `meta.json` as `snapshotColumns`, so
consumers map positionally and appending a column stays backward-compatible:

```
[typeID, maxBuy, minSell, buyVolume, sellVolume, buyOrders, sellOrders, topBuyVolume, topSellVolume]
```

`topBuyVolume` / `topSellVolume` are the units available at the single best bid
and best ask. They are the cheapest reliable signal that an attractive-looking
spread is one player's one-unit order — something an order *count* cannot catch.

Only orders at **Jita IV-4** (station 60003760) are kept. That is 80.7% of all
Forge orders, and it is the book you actually trade against.

### `jitaDepth.json`

The best 20 price levels per side per type, one row per type:

```
[typeID, buyLadder, sellLadder]      # "price:qty|price:qty|…", best price first
```

The snapshot tells you the best price and how much sits *at* it. That is enough
to rank a spread and not enough to size a trade: buy 500 units of something
whose best ask covers 3 and your realised cost is nowhere near `minSell`. Any
consumer that recommends a **quantity** has to walk a ladder.

Twenty levels covers essentially every book here — 327k orders over 18.8k types
is ~17 orders per type across both sides, and levels aggregate below that. Only
mineral-scale books run deeper, and a truncated ladder leaves the consumer with
an unfilled remainder, i.e. pessimistic, which is the safe direction.

Prices are rounded to 2 dp and re-summed after rounding. `maxBuy`/`minSell` on
the snapshot are **not** rounded, so a sub-cent spread can show a ladder whose
best bid equals its best ask — read the snapshot, not the ladder, for a
crossed-book check.

Aggregating by price destroys `min_volume`, so buy volume is counted **only from
orders with `min_volume == 1`** and the discarded total is published as
`depthMinVolumeExcluded`. Treating a `min_volume: 5000` order as freely fillable
would be an optimistic error, and everything else here leans pessimistic.

A separate file rather than two more snapshot columns: every market feature
imports the snapshot and most never size a quantity, so the station-trading path
stays exactly as fast as it was.

### `reproCandidates.json`

The types that could possibly be worth buying, reprocessing, and selling the
materials into buy orders:

```
[typeID, portionSize, bestAsk, materialValue, ceilingProfit, materialCount]
```

`ceilingProfit` is `(Σ material quantity × best bid) − (best ask × portionSize)`
— the profit at **100% yield and zero taxes**, which is impossible and therefore
a true upper bound. A negative ceiling cannot be rescued by any skill level,
standing or tax rate, so dropping it is safe in a way no tuned threshold is.

This exists purely for Apps Script's 6-minute execution cap. The real solve
needs an ask ladder per candidate plus a bid ladder per output material; over
18.8k types that does not fit, over a few hundred it fits easily. Measured on
the live book: **6,854 considered → 715 kept (10.4%), 28 KB.**

The split is deliberate — this file holds only the arithmetic that does not
depend on the player. Real yield, the per-batch floor, taxes and order sizing
stay in the sheet, where the config knobs and the test suite are. Yield cannot
be applied afterwards anyway: output is `floor(quantity × yield)` *per batch*,
so the yield has to be inside the computation.

`candidatesConsidered` and `candidatesKept` go into `meta.json`. If kept ever
approaches considered, the filter has stopped filtering and the consumer is
about to run out of execution time — the build step warns when it exceeds 50%.

### `jitaHistory.json`

Same array-of-arrays shape, `historyColumns` in `meta.json`:

```
[typeID, avgDailyVolume, medianPrice]
```

Trailing 30 days. Types ESI has no history for are **omitted, not zeroed** —
"never traded" and "traded nothing today" are different facts.

### `meta.json`

Provenance and a freshness stamp for both files: `generatedAt`,
`historyGeneratedAt`, `types`, `ordersSeen`, `ordersKept`, `pages`, the column
orders, and sweep durations. Consumers should read this first and skip the
download when `generatedAt` has not moved.

### `noHistoryTypes.json`

Bookkeeping, not a consumer file: the types ESI had no history for last run.

ESI's error limiter counts 4xx, and "no history" is a **404** — so the ~1,400
untraded types on the book spend the 100-errors-per-60-seconds budget about
fourteen times over, and the sweep ends up parked in backoff it inflicted on
itself. The first full run took 21.7 minutes, most of it waiting. Remembering
them removes that cost.

A seventh of the list is rechecked each day, so an item that starts trading
reappears within a week instead of being written off permanently.

## Schedules

| Workflow | Cron | Why |
|---|---|---|
| `market-orders.yml` | hourly at :07 | ESI regenerates orders every 300s, but GitHub's `schedule` has a 5-minute floor and routinely runs 10–20 minutes late under load. Sub-15-minute cadence is unreliable regardless of the cron, and structural opportunities do not move within an hour. The `:07` offset avoids the heavily contended top of the hour. |
| `market-history.yml` | daily at 03:40 UTC | ESI history only changes daily. |

Both accept `workflow_dispatch` for an on-demand refresh.

The two workflows share a `market-data-publish` concurrency group. Each run
force-pushes the whole `market/` directory as it looked at *its* checkout, so
two overlapping runs would have the later one revert the earlier one's file —
serializing them is what prevents that. A queued run then starts fresh after the
first finishes, and picks up the newly published data.

The price is that GitHub keeps only one run queued per group: dispatch a third
while two are outstanding and the middle one is **cancelled before any step
runs**. That looks alarming in the Actions list but is harmless — the next
scheduled run covers it, and Apps Script warns separately when the published
data goes stale. In normal operation the two never overlap.

## Failure policy

**A partial order sweep is never published.** Any page that cannot be fetched
after retries fails the whole job, and a sweep yielding implausibly few types is
rejected outright.

This is stricter than it may look. ESI regenerates the book every 300 seconds,
so a sweep that silently dropped pages would splice two different market states
into one "snapshot" — producing crossed books and phantom spreads on exactly the
thin-market items a spread scanner surfaces. Stale data is recoverable; quietly
corrupt data is not.

History is more forgiving: a type that fails to fetch drops one row from a
ranking, so it degrades instead of aborting.

## Running locally

Python 3, standard library only:

```sh
python3 sweep_orders.py            # writes jitaSnapshot.json + jitaDepth.json + meta.json
python3 build_repro_candidates.py  # needs the snapshot; writes reproCandidates.json
python3 sweep_history.py           # needs the snapshot; writes jitaHistory.json
```

`build_repro_candidates.py` fetches the SDE slice from the sibling repo but
needs no ESI access, so it can be re-run against an already-published snapshot
without touching the market at all — useful for tuning without a full sweep.

Environment overrides: `REGION_ID`, `STATION_ID`, `OUT_DIR`, `CONCURRENCY`,
`MIN_TYPES`, `WINDOW_DAYS`, `DEPTH_LEVELS`, `SDE_BASE`, `EXCLUDED_CATEGORIES`,
`MIN_CEILING_ISK`, `ESI_USER_AGENT`.

## Data source

CCP's public ESI. No authentication, no API key, no third-party aggregator.

```
GET https://esi.evetech.net/latest/markets/10000002/orders/?order_type=all&page=N
GET https://esi.evetech.net/latest/markets/10000002/history/?type_id=T
```
