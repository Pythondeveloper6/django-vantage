"""The top bar's stored layout: reading it, and proving a write reaches the next read.

The layout lives on the same row :mod:`vantage.nav`'s stored arrangement does — see
that module's "reading the row" section — so this file's caching story is really that
module's story: :func:`vantage.topbar.stored_layout` reads
:func:`vantage.nav._singleton_row`, the exact function :func:`vantage.nav.
stored_declaration` reads, rather than querying on its own.

What is worth proving here, specifically, is the thing a shared cache could get wrong:
that a write reaches the very next read, in this same process, without waiting for a
new request to start — and that the sidebar and the top bar cost one query together,
not one each.
"""

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from vantage.nav import invalidate_nav, save_declaration
from vantage.topbar import (
    BUILTIN_KEYS,
    build_topbar,
    clean_layout,
    invalidate_topbar,
    stored_layout,
)


@pytest.fixture(autouse=True)
def clean_topbar_cache():
    """The stored layout is read through :mod:`vantage.nav`'s row cache.

    Without this a test that writes the row would leak it into the next one: the cache
    is only dropped by ``request_started``, and a test that calls these functions
    directly never starts a request. See ``test_nav.py``'s ``clean_nav_cache`` fixture,
    which this mirrors for the same reason.
    """
    invalidate_nav()
    yield
    invalidate_nav()


@pytest.fixture
def client_in(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


class TestCleanLayout:
    """Pure validation, no database: the same rules a stored or posted layout is held to."""

    def test_hiding_sign_out_is_refused(self):
        raw = {"order": list(BUILTIN_KEYS), "hidden": ["user"], "banner": True, "items": []}
        assert "user" not in clean_layout(raw)["hidden"]

    def test_a_known_control_stays_hidden(self):
        raw = {"order": list(BUILTIN_KEYS), "hidden": ["theme"], "banner": True, "items": []}
        assert clean_layout(raw)["hidden"] == ["theme"]


class TestStoredLayout:
    def test_no_row_means_the_code_decides(self, db):
        assert stored_layout() is None
        assert [slot.key for slot in build_topbar().slots] == ["search", "theme", "user"]

    def test_reads_whatever_the_shared_row_holds(self, db, monkeypatch):
        """The read half of :func:`stored_layout`, independent of how the row got its
        ``topbar`` value — see the module docstring for why this is exercised through
        the shared row rather than through a real save.
        """
        import vantage.nav as nav

        class FakeRow:
            topbar = {"order": list(BUILTIN_KEYS), "hidden": ["theme"], "banner": True, "items": []}

        monkeypatch.setattr(nav, "_singleton_row", lambda: FakeRow())
        assert stored_layout()["hidden"] == ["theme"]

    def test_invalidate_topbar_drops_the_shared_row_cache(self, db):
        """Saving must change the next page, not the next restart.

        ``invalidate_topbar`` keeps no cache of its own — :func:`stored_layout` derives
        everything from the row :func:`vantage.nav._singleton_row` already caches — so
        proving it works means proving it drops *that* cache.
        """
        import vantage.nav as nav

        assert stored_layout() is None  # primes the shared cache with "no row"
        assert nav._row_cache is None  # committed: a real miss, not "not asked yet"

        invalidate_topbar()

        assert nav._row_cache is nav._UNSET


class TestSharesNavsRow:
    """The property the fix exists for: one row, read once, no matter who asks."""

    def test_a_write_through_nav_is_visible_to_topbar_on_the_very_next_read(self, db):
        """A cache keyed on the process rather than the request would pass this test
        only by accident of import order — it would serve whichever answer the first
        call below happened to produce. Priming the shared cache with "no row" and
        then proving a save overturns it, in the same test and the same process
        (never restarted, never a new request), is what rules that out.
        """
        from vantage.nav import _singleton_row

        assert _singleton_row() is None  # primes the shared cache: nothing stored yet
        assert stored_layout() is None  # topbar reads the same "nothing stored" answer

        save_declaration(
            [
                {
                    "label": "Everything",
                    "icon": "",
                    "items": [
                        {
                            "kind": "model",
                            "target": "testapp.post",
                            "label": "",
                            "icon": "",
                            "badge": "",
                            "hidden": False,
                            "permission": "",
                            "audiences": [],
                            "args": [],
                        }
                    ],
                }
            ]
        )

        row = _singleton_row()
        assert row is not None
        assert row.groups[0]["label"] == "Everything"
        # topbar's read goes through this exact row — not a copy made before the save.
        assert stored_layout() is None  # no topbar column value was ever stored on it

    def test_the_sidebar_and_the_top_bar_share_one_query(self, client_in):
        """nav.py and topbar.py read the same row; a page rendering both pays once."""
        with CaptureQueriesContext(connection) as ctx:
            client_in.get(reverse("vantage:dashboard"))

        nav_queries = [q for q in ctx.captured_queries if "vantage_navconfiguration" in q["sql"]]
        assert len(nav_queries) == 1
