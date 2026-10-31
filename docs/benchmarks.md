# Benchmarks

[← Index](index.md)

**Published numbers are pending.** This page explains how to measure; it deliberately
contains no results, because numbers from one machine say little about yours.

## What the script measures

`scripts/benchmark.py` fills the demo project's `Order` table with `--rows` rows in a
throwaway database, then times, through Django's test client (no network, no browser):

| Scenario | Request |
|---|---|
| list page | `/admin/shop/order/` |
| filtered list | `?status=paid` |
| search | `?q=...` |
| detail | one record in the middle of the table |
| export (csv) | the filtered list as CSV, fully consumed |
| JSON API list | `/admin/_/api/shop/order/` |

Each URL is requested a few times to warm up, then `--repeat` times for the measurement.
The report gives the median and 95th percentile in milliseconds, and the number of SQL
queries one request runs (the query count is usually the more stable number).

## Running it

From a checkout of the repository, with the development environment installed:

```bash
python scripts/benchmark.py --rows 2000          # a few seconds
python scripts/benchmark.py                       # 100,000 rows
python scripts/benchmark.py --format json > bench.json
```

By default the database is a temporary SQLite file that is deleted afterwards. For
PostgreSQL or MySQL, pass a URL for a **scratch database**; the script runs
`migrate` and inserts data there:

```bash
python scripts/benchmark.py --rows 100000 \
  --database-url postgres://user:password@localhost:5432/vantage_bench
```

`--settings` selects another settings module that installs the `shop` demo app.

## Reading the result

- Compare runs on the same machine, the same database and the same row count.
- A single process serving one client at a time; it says nothing about concurrency.
- The test client skips the web server, static files and the network.
- Your own models, filters and permission rules will differ from the demo's.
