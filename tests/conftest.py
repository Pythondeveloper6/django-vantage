"""Shared test configuration.

``sqlite_only`` marks a test that asserts SQLite's own behaviour — "this path is a no-op
off PostgreSQL", a table dropped mid-transaction, a query count that differs by backend,
fixture values SQLite does not length-check. The database CI jobs run everything else
against PostgreSQL and MySQL.
"""

import os

import pytest

ON_SQLITE = os.environ.get("VANTAGE_TEST_DB", "sqlite") == "sqlite"

sqlite_only = pytest.mark.skipif(
    not ON_SQLITE, reason="asserts SQLite-specific behaviour; covered by the SQLite run"
)
