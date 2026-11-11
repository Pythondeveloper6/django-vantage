"""``scripts/smoke_release.py`` passes: the demo project works end to end.

The script runs the demo in a temporary directory against a throwaway SQLite database,
signs in through the real login form and walks pages, forms, exports, the JSON API, the
agent endpoints and the management commands. It takes a few seconds, so it runs in the
normal suite.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    os.environ.get("VANTAGE_TEST_DB", "sqlite") != "sqlite", reason="the demo is SQLite only"
)


def test_the_release_smoke_script_passes():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "smoke_release.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1500:]
    assert "FAIL" not in result.stdout
    assert result.stdout.count("PASS") >= 20
