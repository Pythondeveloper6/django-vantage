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


def test_the_demo_shows_rich_text_and_the_new_export_formats(tmp_path):
    for step in (["migrate", "--noinput"], ["seed"]):
        result = manage(tmp_path, *step)
        assert result.returncode == 0, result.stderr[-2000:]
    probe = (
        "import django, os;"
        "os.environ['DJANGO_SETTINGS_MODULE']='config.settings';"
        "django.setup();"
        "from django.test import Client;"
        "from django.contrib.auth import get_user_model;"
        "from shop.models import Product;"
        "c=Client();"
        "c.force_login(get_user_model().objects.get(username='admin'));"
        "h={'HTTP_HOST':'127.0.0.1'};"
        "p=Product.objects.get(sku='NW-1000');"
        "stored=p.details_html;"
        "resp=c.get(f'/admin/shop/product/{p.pk}/', **h);"
        "page=resp.content.decode();"
        "edit=c.get(f'/admin/shop/product/{p.pk}/edit/', **h).content.decode();"
        "x=c.get('/admin/shop/order/?export=xlsx', **h);"
        "f=c.get('/admin/shop/order/?export=pdf', **h);"
        "print('STORED_SCRIPT', '<script>void(0)</script>' in stored);"
        "print('PAGE_OK', resp.status_code, 'Case of 24' in page);"
        "print('PAGE_SCRIPT', '<script>void(0)</script>' in page);"
        "print('PAGE_HANDLER', 'onclick' in page);"
        "print('EDIT_RICHTEXT', 'data-richtext' in edit);"
        "print('XLSX', x.status_code, x.headers['Content-Type']);"
        "print('PDF', f.status_code, b''.join(f.streaming_content)[:5])"
    )
    result = manage(tmp_path, code=probe)
    assert result.returncode == 0, result.stderr[-2000:]
    out = result.stdout
    assert "STORED_SCRIPT True" in out  # the seed really did store hostile markup
    assert "PAGE_OK 200 True" in out  # the page rendered, so the next line means something
    assert "PAGE_SCRIPT False" in out and "PAGE_HANDLER False" in out
    assert "EDIT_RICHTEXT True" in out
    assert "XLSX 200 application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in out
    assert "PDF 200 b'%PDF-'" in out
