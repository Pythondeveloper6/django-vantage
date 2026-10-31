#!/usr/bin/env python3
"""Time the main pages of the demo project against a table of N rows.

Seeds ``--rows`` orders into a throwaway database, then measures, through Django's test
client (no network, no browser): the list page, a filtered list, search, a detail page,
a CSV export and the JSON API list. Each request is warmed up first, then repeated;
the report is the median and 95th percentile in milliseconds plus the number of SQL
queries one request runs.

    python scripts/benchmark.py --rows 2000                     # quick look
    python scripts/benchmark.py --rows 100000 --format json
    python scripts/benchmark.py --database-url postgres://user:pw@localhost/bench

The database is a temporary SQLite file unless ``--database-url`` is given. With a URL
the script **creates and fills the tables it needs in that database**, so point it at a
scratch database, never at data you care about. ``--settings`` swaps the settings module
(it must install the ``shop`` demo app).

These are single-process, in-memory-client timings of one machine. They say how two
versions or two databases compare on the same hardware, not how a deployment will behave.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
BASE = "/admin/shop/order/"


def database_from_url(url: str) -> dict:
    """A Django ``DATABASES`` entry from a URL such as ``postgres://u:p@host:5432/db``."""
    parsed = urlparse(url)
    engines = {
        "sqlite": "django.db.backends.sqlite3",
        "postgres": "django.db.backends.postgresql",
        "postgresql": "django.db.backends.postgresql",
        "mysql": "django.db.backends.mysql",
    }
    if parsed.scheme not in engines:
        raise SystemExit(f"Unsupported database scheme {parsed.scheme!r}.")
    if parsed.scheme == "sqlite":
        return {"ENGINE": engines["sqlite"], "NAME": unquote(parsed.path.lstrip("/")) or ":memory:"}
    return {
        "ENGINE": engines[parsed.scheme],
        "NAME": parsed.path.lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
    }


def setup_django(settings_module: str, database: dict) -> None:
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "example")]
    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    from django.conf import settings

    settings.DATABASES = {"default": database}
    settings.DEBUG = False  # DEBUG keeps every query in memory; it would skew the timings.
    import django

    django.setup()


def seed(rows: int) -> int:
    """Create ``rows`` orders (and enough customers to spread them). Returns a sample pk."""
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.utils import timezone
    from shop.models import Customer, Order

    call_command("migrate", verbosity=0, interactive=False)
    get_user_model().objects.create_superuser("bench", "bench@example.com", "bench-pass")

    Customer.objects.bulk_create(
        [
            Customer(name=f"Customer {i}", email=f"c{i}@example.com")
            for i in range(max(1, rows // 20))
        ]
    )
    customers = list(Customer.objects.values_list("pk", flat=True))
    statuses = [s for s, _ in Order.Status.choices]
    now = timezone.now()
    batch: list = []
    for i in range(rows):
        batch.append(
            Order(
                number=f"B-{i:08d}",
                customer_id=customers[i % len(customers)],
                status=statuses[i % len(statuses)],
                total=(i % 9000) / 10,
                shipping_city=f"City {i % 50}",
                created_at=now - timezone.timedelta(minutes=i),
            )
        )
        if len(batch) >= 5000:
            Order.objects.bulk_create(batch)
            batch = []
    if batch:
        Order.objects.bulk_create(batch)
    return Order.objects.order_by("pk").values_list("pk", flat=True)[rows // 2]


def scenarios(sample_pk: int) -> list[tuple[str, str]]:
    return [
        ("list page", BASE),
        ("filtered list", f"{BASE}?status=paid"),
        ("search", f"{BASE}?q=B-0000123"),
        ("detail", f"{BASE}{sample_pk}/"),
        ("export (csv)", f"{BASE}?export=csv&status=paid"),
        ("JSON API list", "/admin/_/api/shop/order/"),
    ]


def measure(client, url: str, warmup: int, repeat: int) -> dict:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def once() -> tuple[float, int, int]:
        with CaptureQueriesContext(connection) as queries:
            start = time.perf_counter()
            response = client.get(url)
            body = b"".join(response.streaming_content) if response.streaming else response.content
            elapsed = (time.perf_counter() - start) * 1000
        return elapsed, len(queries), response.status_code if body is not None else 0

    for _ in range(warmup):
        once()
    samples = [once() for _ in range(repeat)]
    times = sorted(s[0] for s in samples)
    p95 = times[min(len(times) - 1, round(0.95 * (len(times) - 1)))]
    return {
        "median_ms": round(statistics.median(times), 2),
        "p95_ms": round(p95, 2),
        "queries": samples[-1][1],
        "status": samples[-1][2],
    }


def run(rows: int, warmup: int, repeat: int, settings_module: str, database_url: str | None):
    tmp = None
    if database_url:
        database = database_from_url(database_url)
    else:
        tmp = tempfile.TemporaryDirectory(prefix="vantage-bench-")
        database = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(Path(tmp.name) / "bench.db"),
        }
    try:
        setup_django(settings_module, database)
        from django.test import Client

        sample_pk = seed(rows)
        client = Client()
        client.login(username="bench", password="bench-pass")
        results = {name: measure(client, url, warmup, repeat) for name, url in scenarios(sample_pk)}
        from django.db import connection

        return {
            "rows": rows,
            "database": connection.vendor,
            "warmup": warmup,
            "repeat": repeat,
            "results": results,
        }
    finally:
        if tmp:
            tmp.cleanup()


def as_markdown(report: dict) -> str:
    lines = [
        f"Rows: {report['rows']} - database: {report['database']} - "
        f"{report['warmup']} warm-up, {report['repeat']} timed requests each.",
        "",
        "| Scenario | Median (ms) | p95 (ms) | Queries | HTTP |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, r in report["results"].items():
        lines.append(
            f"| {name} | {r['median_ms']} | {r['p95_ms']} | {r['queries']} | {r['status']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--settings", default="config.settings")
    parser.add_argument("--database-url", help="e.g. postgres://user:pw@host/dbname (scratch DB!)")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    if args.rows < 1 or args.repeat < 1:
        parser.error("--rows and --repeat must be at least 1")

    report = run(args.rows, args.warmup, args.repeat, args.settings, args.database_url)
    print(json.dumps(report, indent=2) if args.format == "json" else as_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
