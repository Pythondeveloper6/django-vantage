#!/usr/bin/env python3
"""Regenerate the README and docs screenshots from the demo project.

The pictures have to show what the package does out of the box, so they are taken from
a *copy* of ``example/db.sqlite3`` with every UI override cleared (a site title someone
typed into the settings screen, a tuned column list, a rearranged sidebar), and with
the demo's "nothing here is real" environment banner switched off. The working demo
database is never touched.

Each page is captured in light and dark at 2x, then written as WebP into
``docs/assets/screenshots/`` — small enough for the README, which PyPI renders from
raw GitHub URLs, to load quickly.

    uv sync --group dev --group browser
    uv run playwright install chromium
    uv run python scripts/screenshots.py
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "assets" / "screenshots"
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

#: name -> path. The orders list is filtered to a date window so its trend chip
#: compares two real periods instead of the data against nothing.
PAGES = {
    "dashboard": "/admin/",
    "orders": "/admin/shop/order/?created_at=P90D",
    "order-form": "/admin/shop/order/127/edit/",
    "products": "/admin/shop/product/",
    "reviews": "/admin/shop/review/",
    "schema": "/admin/_/schema/",
    # The alternative list views. The board is grouped by tier so its picker shows a
    # choice other than the default.
    "kanban": "/admin/shop/customer/?view=kanban&by=tier",
    # The month of the newest order: the demo's dates are fixed when it is seeded, so
    # "this month" may be empty by the time anyone runs this.
    "calendar": "/admin/shop/order/?view=calendar&month={month}",
    "gallery": "/admin/shop/product/?view=gallery",
    "map": "/admin/shop/customer/?view=map",
    "data-quality": "/admin/_/data-quality/",
    "roles": "/admin/_/roles/",
    "role-matrix": "/admin/_/roles/{role}/",
    # A customer's API token, masked.
    "masked": "/admin/shop/customer/1/",
    "themes": "/admin/_/account/",
}
UI_OVERRIDES = ("vantage_siteconfiguration", "vantage_listpreference", "vantage_navconfiguration")


def prepare(workdir: Path) -> dict[str, str]:
    """A pristine, migrated and seeded copy of the demo database, and settings for it.

    Migrated and re-seeded because the demo database may predate the release being
    pictured: a new migration, the map's coordinates, the roles. Both are idempotent.
    """
    source = REPO / "example" / "db.sqlite3"
    if not source.exists():
        sys.exit("No example/db.sqlite3 yet. Run ./example/run.sh once to create it.")
    db = workdir / "db.sqlite3"
    shutil.copy(source, db)
    with sqlite3.connect(db) as conn:
        for table in UI_OVERRIDES:
            conn.execute(f"DELETE FROM {table}")
    (workdir / "screenshot_settings.py").write_text(
        "from config.settings import *  # noqa\n"
        f"DATABASES['default']['NAME'] = {str(db)!r}\n"
        "VANTAGE = {**VANTAGE, 'ENVIRONMENT': None}\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(workdir),
        "DJANGO_SETTINGS_MODULE": "screenshot_settings",
    }
    for command in (["migrate", "-v0"], ["seed"]):
        subprocess.run(
            [sys.executable, "example/manage.py", *command],
            cwd=REPO,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
    return env


def lookup(sql: str, db: Path):
    with sqlite3.connect(db) as conn:
        row = conn.execute(sql).fetchone()
    return row[0] if row else None


def wait_for_port(port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    sys.exit(f"The demo server did not come up on :{port}.")


def capture(db: Path) -> None:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)

    def save(page, name: str) -> None:
        image = Image.open(io.BytesIO(page.screenshot()))
        image = image.resize((image.width * 5 // 8, image.height * 5 // 8), Image.LANCZOS)
        image.save(OUT / f"{name}.webp", "WEBP", quality=82, method=6)
        print(f"  {name}.webp")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark"):
            context = browser.new_context(
                viewport={"width": 1600, "height": 1000},
                device_scale_factor=2,
                color_scheme=scheme,
            )
            page = context.new_page()
            page.goto(f"{BASE}/admin/")
            page.fill("input[name=username]", "admin")
            page.fill("input[name=password]", "admin12345")
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            role = lookup("SELECT id FROM auth_group WHERE name = 'Catalogue editors'", db)
            month = lookup("SELECT substr(MAX(created_at), 1, 7) FROM shop_order", db)
            for name, path in PAGES.items():
                page.goto(BASE + path.format(role=role, month=month))
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(600)  # let the charts' entry transition settle
                save(page, f"{name}-{scheme}")
            page.goto(f"{BASE}/admin/shop/order/")
            page.wait_for_load_state("networkidle")
            page.keyboard.press("Meta+k" if sys.platform == "darwin" else "Control+k")
            page.wait_for_timeout(400)
            page.keyboard.type("ord")
            page.wait_for_timeout(900)
            save(page, f"palette-{scheme}")
            undo_toast(page, db, scheme, save)
            context.close()
            phone(browser, scheme, save)
        browser.close()


def phone(browser, scheme: str, save) -> None:
    """The phone layout: a list as cards, then the bulk-action sheet with two ticked."""
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        color_scheme=scheme,
        is_mobile=True,
        has_touch=True,
    )
    page = context.new_page()
    page.goto(f"{BASE}/admin/")
    page.fill("input[name=username]", "admin")
    page.fill("input[name=password]", "admin12345")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(f"{BASE}/admin/shop/order/?view=table")
    page.wait_for_load_state("networkidle")
    # Past the title and the toolbar, so the picture is of the cards themselves.
    page.evaluate("document.querySelector('table').scrollIntoView()")
    save(page, f"phone-orders-{scheme}")
    # Reviews carry bulk actions; orders are read-only in the demo.
    page.goto(f"{BASE}/admin/shop/review/")
    page.wait_for_load_state("networkidle")
    page.evaluate("document.querySelector('table').scrollIntoView()")
    page.locator("[data-select-row]").nth(0).check()
    page.locator("[data-select-row]").nth(1).check()
    page.wait_for_timeout(400)  # the sheet's slide
    save(page, f"phone-sheet-{scheme}")
    context.close()


def undo_toast(page, db: Path, scheme: str, save) -> None:
    """Delete one review, picture the toast that offers it back, then take the offer.

    Last, because it changes the data the other pictures show, and undone straight
    away so the dark pass finds the same review to delete.
    """
    review = lookup("SELECT id FROM shop_review ORDER BY id DESC LIMIT 1", db)
    url = f"{BASE}/admin/shop/review/"
    token = next(c["value"] for c in page.context.cookies() if c["name"] == "csrftoken")
    page.request.post(
        url,
        form={
            "action": "delete_selected",
            "selected": str(review),
            "confirmed": "1",
            "csrfmiddlewaretoken": token,
        },
        headers={"Referer": url},
        max_redirects=0,
    )
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)
    save(page, f"undo-{scheme}")
    undo = page.locator(".v-toast [data-undo-form] button")
    if undo.count():
        undo.first.click()
        page.wait_for_load_state("networkidle")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = prepare(Path(tmp))
        server = subprocess.Popen(
            [sys.executable, "example/manage.py", "runserver", str(PORT), "--noreload"],
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for_port(PORT)
            print(f"Writing to {OUT.relative_to(REPO)}/")
            capture(Path(tmp) / "db.sqlite3")
        finally:
            server.terminate()
            server.wait()


if __name__ == "__main__":
    main()
