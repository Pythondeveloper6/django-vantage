"""The benchmark script runs end to end on a tiny table (in a subprocess: it configures
its own Django settings, which must not leak into this test process)."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "benchmark.py"


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=300
    )


def test_json_report_with_a_tiny_table():
    result = run("--rows", "30", "--repeat", "2", "--warmup", "1", "--format", "json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["rows"] == 30
    assert set(report["results"]) == {
        "list page",
        "filtered list",
        "search",
        "detail",
        "export (csv)",
        "JSON API list",
    }
    for row in report["results"].values():
        assert row["status"] == 200
        assert row["queries"] >= 1
        assert row["p95_ms"] >= row["median_ms"] >= 0


def test_markdown_table_is_the_default():
    result = run("--rows", "10", "--repeat", "1", "--warmup", "0")
    assert result.returncode == 0, result.stderr
    assert "| Scenario | Median (ms)" in result.stdout


def test_unsupported_database_url_is_refused():
    result = run("--rows", "10", "--database-url", "oracle://x/y")
    assert result.returncode != 0
    assert "Unsupported database scheme" in result.stderr
