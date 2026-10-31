"""The demo project in ``example/`` boots, migrates, seeds and serves its panel.

Run as a subprocess against a throwaway database and media directory, so it exercises
the project exactly as ``example/run.sh`` does without touching a developer's demo data.
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


def manage(tmp_path, *args, code=None):
    env = {
        **os.environ,
        "VANTAGE_DEMO_DB": str(tmp_path / "demo.sqlite3"),
        "VANTAGE_DEMO_MEDIA": str(tmp_path / "media"),
        "PYTHONPATH": str(ROOT / "src"),
    }
    env.pop("DJANGO_SETTINGS_MODULE", None)
    command = [sys.executable, str(ROOT / "example" / "manage.py"), *args]
    if code is not None:
        command = [sys.executable, "-c", code]
    return subprocess.run(
        command, cwd=ROOT / "example", env=env, capture_output=True, text=True, timeout=600
    )


def test_the_demo_migrates_seeds_and_serves_the_panel(tmp_path):
    for step in (["check"], ["migrate", "--noinput"], ["seed"]):
        result = manage(tmp_path, *step)
        assert result.returncode == 0, result.stderr[-2000:]
    probe = (
        "import django, os;"
        "os.environ['DJANGO_SETTINGS_MODULE']='config.settings';"
        "django.setup();"
        "from django.test import Client;"
        "from django.contrib.auth import get_user_model;"
        "c=Client();"
        "c.force_login(get_user_model().objects.get(username='admin'));"
        "r=c.get('/admin/', HTTP_HOST='127.0.0.1');"
        "print(r.status_code)"
    )
    result = manage(tmp_path, code=probe)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("200")
