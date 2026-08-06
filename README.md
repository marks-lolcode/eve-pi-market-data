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
https://raw.githubusercontent.com/marks-lolcode/eve-pi-market-data/data/market/jitaHistory.json
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

## Schedules

| Workflow | Cron | Why |
|---|---|---|
| `market-orders.yml` | hourly at :07 | ESI regenerates orders every 300s, but GitHub's `schedule` has a 5-minute floor and routinely runs 10–20 minutes late under load. Sub-15-minute cadence is unreliable regardless of the cron, and structural opportunities do not move within an hour. The `:07` offset avoids the heavily contended top of the hour. |
| `market-history.yml` | daily at 03:40 UTC | ESI history only changes daily. |

Both accept `workflow_dispatch` for an on-demand refresh.

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
python3 sweep_orders.py     # writes market/jitaSnapshot.json + meta.json
python3 sweep_history.py    # needs the snapshot; writes market/jitaHistory.json
```

Environment overrides: `REGION_ID`, `STATION_ID`, `OUT_DIR`, `CONCURRENCY`,
`MIN_TYPES`, `WINDOW_DAYS`, `ESI_USER_AGENT`.

## Data source

CCP's public ESI. No authentication, no API key, no third-party aggregator.

```
GET https://esi.evetech.net/latest/markets/10000002/orders/?order_type=all&page=N
GET https://esi.evetech.net/latest/markets/10000002/history/?type_id=T
```
