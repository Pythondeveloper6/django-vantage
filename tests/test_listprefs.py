"""Per-list customisation: the resolution order, and the boundary around it.

Two things are being asserted here, and only one of them is a feature.

The feature is the cascade: **user row > site row > panels.py > inference**, resolved
per attribute, so tuning one thing never freezes the rest. Most of this file is that
sentence, taken apart.

The other is the boundary. A stored preference contributes field names to a query on
every subsequent render of a list, so the POST that writes it is treated as hostile and
so is the row that comes back out. ``TestAHostilePost`` is named to be impossible to
delete by accident.
"""

from contextlib import contextmanager

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Count, Sum
from django.test import RequestFactory, override_settings

from tests.testapp.models import Author, Order
from vantage import listprefs
from vantage.conf import DEFAULTS
from vantage.models import SETTING_COLUMNS
from vantage.panels import ModelPanel
from vantage.sites import site
from vantage.stats import Chart, Stat

# ── fixtures ────────────────────────────────────────────────────────────────


class TunedOrderPanel(ModelPanel):
    """A panel that says what it wants, so "panels.py beats inference" is testable.

    Deliberately narrower than inference would choose: ``customer`` is dropped from the
    columns and from the filters, which is what makes "the UI cannot add it back" a
    meaningful assertion rather than a tautology.
    """

    list_display = ["number", "status", "total", "created_at"]
    list_filter = ["status", "created_at"]
    list_search = ["number"]
    list_per_page = 25
    list_stats = [
        Stat("Revenue", Sum("total"), format="currency"),
        Stat("Orders", Count("id")),
    ]
    list_charts = [
        Chart.breakdown("By status", group_by="status", value=Count("id"), kind="donut")
    ]


@pytest.fixture
def panel():
    return TunedOrderPanel(Order, site)


@pytest.fixture
def bare():
    """A panel that declares nothing: the inference floor of the cascade."""
    return ModelPanel(Order, site)


@pytest.fixture
def person(db):
    return User.objects.create_user("dana", password="x", is_staff=True)


@pytest.fixture
def other(db):
    return User.objects.create_user("robin", password="x", is_staff=True)


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("chief", "chief@example.com", "x")


@pytest.fixture
def rf():
    return RequestFactory()


def get_request(rf, user=None, path="/admin/testapp/order/"):
    request = rf.get(path)
    request.user = user or _Anonymous()
    return request


def post_request(rf, user, data, path="/admin/testapp/order/_prefs/"):
    """A POST the view can actually run against: session and messages included."""
    request = rf.post(path, data)
    request.user = user
    SessionMiddleware(lambda r: None).process_request(request)
    MessageMiddleware(lambda r: None).process_request(request)
    return request


class _Anonymous:
    is_authenticated = False
    is_staff = False
    is_superuser = False
    pk = None


def write(panel, user, **values):
    """A stored row, written straight to the database.

    Bypassing the view on purpose: the resolution tests are about what a row means, not
    about how it got there, and building one by hand is also how a row written by an
    older version of the panel is simulated.
    """
    from vantage.models import ListPreference

    row, _ = ListPreference.objects.get_or_create(
        resource_key=listprefs.resource_key(panel), user=user
    )
    for name, value in values.items():
        setattr(row, name, value)
    row.save()
    return row


@contextmanager
def hidden_table():
    """The pre-``migrate`` state, reproduced inside a test.

    Renamed rather than dropped, and by raw SQL rather than the schema editor, for the
    same two reasons ``test_siteconfig`` does it: the table comes back even if an
    assertion fails, and SQLite refuses schema edits inside the transaction every test
    already runs in.
    """
    from vantage.models import ListPreference

    quoted = connection.ops.quote_name(ListPreference._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {quoted} RENAME TO vantage_listpreference_hidden")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE vantage_listpreference_hidden RENAME TO {quoted}")


@pytest.fixture
def orders(db):
    author = Author.objects.create(name="Ada", email="ada@example.com")
    return [
        Order.objects.create(
            number=f"A-{i}", customer=author, total=10 * i, status=Order.Status.PAID
        )
        for i in range(1, 4)
    ]


# ── the cascade ─────────────────────────────────────────────────────────────


class TestResolutionOrder:
    """Four layers, narrowest first."""

    def test_inference_is_the_floor(self, bare, rf, person):
        spec = listprefs.resolve(bare, get_request(rf, person))
        assert spec.columns == list(listprefs.options(bare).columns)
        assert "customer" in spec.columns  # inference chose it; nothing overrode it

    def test_panels_py_beats_inference(self, panel, rf, person):
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.columns == ["number", "status", "total", "created_at"]
        assert spec.per_page == 25

    def test_the_site_row_beats_panels_py(self, panel, rf, person):
        write(panel, None, columns=["status", "number"], per_page=10)
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.columns == ["status", "number"]
        assert spec.per_page == 10
        assert spec.source("columns") == "site"

    def test_a_user_row_beats_the_site_row(self, panel, rf, person):
        write(panel, None, columns=["status", "number"])
        write(panel, person, columns=["total"])
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.columns == ["total"]
        assert spec.source("columns") == "user"

    def test_someone_elses_default_never_displaces_my_choice(self, panel, rf, person, other):
        """The rule the whole two-row design exists for.

        A colleague saving the site default afterwards must not change what I chose. If
        the merge were "last write wins" rather than "user row first", it would.
        """
        write(panel, person, columns=["total"])
        write(panel, None, columns=["status", "number"])

        mine = listprefs.resolve(panel, get_request(rf, person))
        theirs = listprefs.resolve(panel, get_request(rf, other))

        assert mine.columns == ["total"]
        assert theirs.columns == ["status", "number"]

    def test_an_anonymous_request_sees_only_the_site_row(self, panel, rf, person):
        write(panel, person, columns=["total"])
        write(panel, None, columns=["status"])
        spec = listprefs.resolve(panel, get_request(rf))
        assert spec.columns == ["status"]

    def test_no_request_means_no_database_layer(self, panel, person):
        """``check``, ``scaffold`` and the registry ask what the *code* says."""
        write(panel, None, columns=["status"])
        assert listprefs.resolve(panel).columns == ["number", "status", "total", "created_at"]


class TestPerAttributeFallthrough:
    """Setting one thing overrides that thing and nothing else — the panel's promise,
    extended to the row above it."""

    def test_changing_the_columns_leaves_the_filters_to_the_code(self, panel, rf, person):
        write(panel, person, columns=["total"])
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.columns == ["total"]
        assert spec.filter_fields == ["status", "created_at"]
        assert spec.search_fields == ["number"]
        assert spec.per_page == 25
        assert [stat.label for stat in spec.stats] == ["Revenue", "Orders"]

    def test_changing_the_filters_leaves_the_columns_to_the_code(self, panel, rf, person):
        write(panel, person, filter_fields=["created_at"])
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.filter_fields == ["created_at"]
        assert spec.columns == ["number", "status", "total", "created_at"]

    def test_a_user_row_falls_through_to_the_site_row_attribute_by_attribute(
        self, panel, rf, person
    ):
        write(panel, None, columns=["status"], per_page=10)
        write(panel, person, per_page=5)
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.per_page == 5
        assert spec.columns == ["status"]
        assert spec.source("per_page") == "user"
        assert spec.source("columns") == "site"

    def test_the_getter_override_is_none_when_nothing_is_stored(self, panel, rf, person):
        """``None``, not a default: it is what makes ``panels.py`` still decide."""
        request = get_request(rf, person)
        listprefs.resolve(panel, request)
        assert listprefs.override(panel, request, "list_display") is None
        assert listprefs.override(panel, request, "list_filter") is None
        assert listprefs.override(panel, request, "list_search") is None
        assert listprefs.override(panel, request, "per_page") is None

    def test_the_getter_override_carries_the_stored_value(self, panel, rf, person):
        write(panel, person, columns=["total"], per_page=7)
        request = get_request(rf, person)
        listprefs.resolve(panel, request)
        assert listprefs.override(panel, request, "list_display") == ["total"]
        assert listprefs.override(panel, request, "per_page") == 7
        assert listprefs.override(panel, request, "list_filter") is None

    def test_switching_a_section_off_overrides_with_an_empty_list(self, panel, rf, person):
        write(panel, person, filters_enabled=False, search_enabled=False)
        request = get_request(rf, person)
        listprefs.resolve(panel, request)
        assert listprefs.override(panel, request, "list_filter") == []
        assert listprefs.override(panel, request, "list_search") == []


class TestReset:
    def test_reset_restores_what_the_code_declares(self, panel, rf, person):
        write(panel, person, columns=["total"], per_page=5)
        request = post_request(rf, person, {})

        listprefs.reset_section(panel, request, "columns", user=person)

        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.columns == ["number", "status", "total", "created_at"]
        assert spec.per_page == 25

    def test_reset_clears_only_its_own_section(self, panel, rf, person):
        write(panel, person, columns=["total"], filter_fields=["created_at"])
        listprefs.reset_section(panel, post_request(rf, person, {}), "columns", user=person)

        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.columns == ["number", "status", "total", "created_at"]
        assert spec.filter_fields == ["created_at"]

    def test_reset_writes_null_rather_than_todays_value(self, panel, rf, person):
        """The point of the button is that the panel's Python becomes the answer *and
        stays* the answer when the panel changes."""
        from vantage.models import ListPreference

        write(panel, person, columns=["total"], filter_fields=["created_at"])
        listprefs.reset_section(panel, post_request(rf, person, {}), "columns", user=person)

        row = ListPreference.objects.get(user=person)
        assert row.columns is None

    def test_resetting_the_last_section_removes_the_row(self, panel, rf, person):
        from vantage.models import ListPreference

        write(panel, person, columns=["total"])
        listprefs.reset_section(panel, post_request(rf, person, {}), "columns", user=person)
        assert not ListPreference.objects.filter(user=person).exists()

    def test_resetting_my_row_does_not_touch_the_site_default(self, panel, rf, person):
        write(panel, None, columns=["status"])
        write(panel, person, columns=["total"])
        listprefs.reset_section(panel, post_request(rf, person, {}), "columns", user=person)

        assert listprefs.resolve(panel, get_request(rf, person)).columns == ["status"]

    def test_resetting_a_section_that_was_never_set_is_a_no_op(self, panel, rf, person):
        listprefs.reset_section(panel, post_request(rf, person, {}), "stats", user=person)
        assert listprefs.resolve(panel, get_request(rf, person)).columns == [
            "number", "status", "total", "created_at"
        ]

    def test_an_unknown_section_is_refused_outright(self, panel, rf, person):
        with pytest.raises(ValueError, match="not a section"):
            listprefs.reset_section(panel, post_request(rf, person, {}), "everything", user=person)


# ── the boundary ────────────────────────────────────────────────────────────


class TestAHostilePost:
    """The POST body is assumed to be written by an attacker, because it can be.

    Every name here goes on to become part of a query on every render of this list for
    whoever the row belongs to. A stored preference may hide, reorder and re-aggregate
    what the panel already offers; it may never widen it.
    """

    def test_a_column_the_panel_does_not_display_is_rejected(self, panel, rf, person):
        values = listprefs.clean_section(
            panel, get_request(rf, person), "columns", {"columns": ["customer", "total"]}
        )
        assert values["columns"] == ["total"]

    def test_a_column_that_is_not_a_field_at_all_is_rejected(self, panel, rf, person):
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "columns",
            {"columns": ["total; drop table", "customer__email", "__class__"]},
        )
        assert values["columns"] is None

    def test_a_filter_on_a_field_the_panel_does_not_expose_is_rejected(self, panel, rf, person):
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "filters",
            {"filters_enabled": "on", "filter_fields": ["customer", "status"]},
        )
        assert values["filter_fields"] == ["status"]

    def test_a_search_field_the_panel_does_not_expose_is_rejected(self, panel, rf, person):
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "search",
            {"search_enabled": "on", "search_fields": ["customer__email", "number"]},
        )
        assert values["search_fields"] == ["number"]

    def test_a_summary_aggregate_across_a_relation_is_rejected(self, panel, rf, person):
        """Aggregating through a join multiplies values by the fan-out, and a footer is
        the last place anyone would look for the cause."""
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "summary",
            {"summary_enabled": "on", "summary__customer__id": "sum", "summary__total": "avg"},
        )
        assert values["summary_aggregates"] == {"total": "avg"}

    def test_an_unknown_aggregate_is_rejected(self, panel, rf, person):
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "stats",
            {"stats_enabled": "on", "add_stat_aggregate": "exec", "add_stat_field": "total"},
        )
        assert values["extra_stats"] is None

    def test_a_stat_aggregating_a_field_the_panel_does_not_display_is_rejected(
        self, panel, rf, person
    ):
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "stats",
            {"stats_enabled": "on", "add_stat_aggregate": "sum", "add_stat_field": "customer_id"},
        )
        assert values["extra_stats"] is None

    def test_a_chart_grouped_by_a_field_the_panel_does_not_expose_is_rejected(
        self, panel, rf, person
    ):
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "charts",
            {
                "charts_enabled": "on",
                "add_chart_mode": "breakdown",
                "add_chart_group": "customer",
                "add_chart_aggregate": "count",
            },
        )
        assert values["extra_charts"] is None

    def test_a_timeseries_over_something_that_is_not_a_date_is_rejected(
        self, panel, rf, person
    ):
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "charts",
            {
                "charts_enabled": "on",
                "add_chart_mode": "timeseries",
                "add_chart_date": "total",
                "add_chart_aggregate": "count",
            },
        )
        assert values["extra_charts"] is None

    def test_a_stat_key_that_was_never_declared_is_rejected(self, panel, rf, person):
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "stats",
            {"stats_enabled": "on", "stat_keys": ["revenue", "everything"]},
        )
        assert values["stat_keys"] == ["revenue"]

    def test_an_absurd_page_size_is_rejected(self, panel, rf, person):
        request = get_request(rf, person)

        def per_page(value):
            return listprefs.clean_section(
                panel, request, "columns", {"per_page": value}
            )["per_page"]

        assert per_page("0") is None
        assert per_page("999999") is None
        assert per_page("nine") is None
        assert per_page("30") == 30

    def test_an_unknown_section_is_refused_outright(self, panel, rf, person):
        with pytest.raises(ValueError, match="not a section"):
            listprefs.clean_section(panel, get_request(rf, person), "sudo", {})


class TestAStoredRowIsValidatedOnTheWayOut:
    """The second half of the boundary. What a panel exposed when a row was written is
    not what it exposes today, and a row is not re-checked when the code changes."""

    def test_a_column_the_panel_has_since_dropped_is_ignored(self, panel, rf, person):
        write(panel, person, columns=["customer", "total"])
        assert listprefs.resolve(panel, get_request(rf, person)).columns == ["total"]

    def test_a_row_that_validates_down_to_nothing_falls_back_to_the_code(
        self, panel, rf, person
    ):
        write(panel, person, columns=["customer", "customer__email"])
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.columns == ["number", "status", "total", "created_at"]
        assert spec.source("columns") == "code"

    def test_a_row_of_the_wrong_shape_entirely_is_ignored(self, panel, rf, person):
        write(panel, person, columns={"not": "a list"}, stat_keys=17, summary_aggregates=[1, 2])
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.columns == ["number", "status", "total", "created_at"]
        assert spec.summary_aggregates == {"total": "sum"}
        assert [stat.label for stat in spec.stats] == ["Revenue", "Orders"]

    def test_a_stored_stat_over_a_dropped_column_is_ignored(self, panel, rf, person):
        write(
            panel,
            person,
            extra_stats=[{"aggregate": "sum", "field": "customer_id", "label": "Bad"}],
        )
        assert [s.label for s in listprefs.resolve(panel, get_request(rf, person)).stats] == [
            "Revenue",
            "Orders",
        ]

    def test_a_row_for_a_model_nobody_registered_is_simply_never_read(self, panel, rf, person):
        """The reason the key is text and not a ContentType: an inert row, not a cascade."""
        from vantage.models import ListPreference

        ListPreference.objects.create(resource_key="ghost.model", user=person, per_page=1)
        assert listprefs.resolve(panel, get_request(rf, person)).per_page == 25


# ── scope ───────────────────────────────────────────────────────────────────


class TestWhoMaySetTheDefaultForEveryone:
    def test_a_plain_staff_user_may_not(self, rf, person):
        assert listprefs.may_set_site_default(get_request(rf, person)) is False

    def test_a_superuser_may(self, rf, boss):
        assert listprefs.may_set_site_default(get_request(rf, boss)) is True

    def test_an_anonymous_request_may_not(self, rf):
        assert listprefs.may_set_site_default(get_request(rf)) is False

    def test_a_permission_string_widens_it(self, rf, person, monkeypatch):
        monkeypatch.setitem(DEFAULTS, listprefs.PERMISSION_SETTING, None)
        with override_settings(VANTAGE={listprefs.PERMISSION_SETTING: "auth.change_user"}):
            assert listprefs.may_set_site_default(get_request(rf, person)) is False
            person.user_permissions.add(Permission.objects.get(codename="change_user"))
            assert listprefs.may_set_site_default(get_request(rf, User.objects.get(pk=person.pk)))

    def test_a_callable_widens_it(self, rf, person, monkeypatch):
        monkeypatch.setitem(DEFAULTS, listprefs.PERMISSION_SETTING, None)
        with override_settings(VANTAGE={listprefs.PERMISSION_SETTING: lambda r: True}):
            assert listprefs.may_set_site_default(get_request(rf, person)) is True


class TestTheView:
    """The write path, exercised through the view rather than around it."""

    def view(self, panel):
        from vantage.listprefsview import ListPrefsView

        return ListPrefsView.as_view(site=site, panel=panel)

    def test_a_save_stores_a_row_for_the_person_who_saved_it(self, panel, rf, boss):
        request = post_request(
            rf, boss, {"section": "columns", "do": "save:user", "columns": ["total", "number"]}
        )
        response = self.view(panel)(request)

        assert response.status_code == 302
        assert listprefs.resolve(panel, get_request(rf, boss)).columns == ["total", "number"]
        assert listprefs.row_for(panel, None) is None

    def test_saving_for_everyone_stores_the_site_row(self, panel, rf, boss, person):
        request = post_request(
            rf, boss, {"section": "columns", "do": "save:site", "columns": ["status"]}
        )
        self.view(panel)(request)

        assert listprefs.row_for(panel, boss) is None
        assert listprefs.resolve(panel, get_request(rf, person)).columns == ["status"]

    def test_a_non_privileged_user_cannot_write_the_site_wide_default(self, panel, rf, person):
        request = post_request(
            rf, person, {"section": "columns", "do": "save:site", "columns": ["status"]}
        )
        with pytest.raises(PermissionDenied):
            self.view(panel)(request)
        assert listprefs.row_for(panel, None) is None

    def test_a_refused_site_save_is_not_quietly_downgraded_to_a_personal_one(
        self, panel, rf, person
    ):
        """A silent downgrade would look exactly like success."""
        request = post_request(
            rf, person, {"section": "columns", "do": "save:site", "columns": ["status"]}
        )
        with pytest.raises(PermissionDenied):
            self.view(panel)(request)
        assert listprefs.row_for(panel, person) is None

    def test_an_unknown_section_changes_nothing(self, panel, rf, boss):
        from vantage.models import ListPreference

        request = post_request(rf, boss, {"section": "everything", "columns": ["status"]})
        assert self.view(panel)(request).status_code == 302
        assert not ListPreference.objects.exists()

    def test_reset_through_the_view(self, panel, rf, boss):
        write(panel, boss, columns=["total"])
        request = post_request(rf, boss, {"section": "columns", "do": "reset:user"})
        self.view(panel)(request)
        assert listprefs.resolve(panel, get_request(rf, boss)).columns == [
            "number", "status", "total", "created_at"
        ]

    def test_it_returns_to_the_list_it_came_from(self, panel, rf, boss):
        request = post_request(
            rf,
            boss,
            {"section": "columns", "columns": ["status"], "next": "/admin/testapp/order/?q=x"},
        )
        assert self.view(panel)(request)["Location"] == "/admin/testapp/order/?q=x"

    def test_an_off_site_next_is_not_followed(self, panel, rf, boss):
        """An unchecked redirect on an authenticated page borrows the panel's credibility."""
        request = post_request(
            rf, boss, {"section": "columns", "columns": ["status"], "next": "https://evil.test/"}
        )
        assert self.view(panel)(request)["Location"] == "/admin/testapp/order/"

    def test_a_get_redirects_rather_than_erroring(self, panel, rf, boss):
        request = rf.get("/admin/testapp/order/_prefs/")
        request.user = boss
        assert self.view(panel)(request).status_code == 302

    def test_an_anonymous_post_never_reaches_the_handler(self, panel, rf, db):
        request = post_request(rf, _Anonymous(), {"section": "columns"})
        assert self.view(panel)(request).status_code == 302  # sent to the login page


# ── what the list page then computes ────────────────────────────────────────


class TestAnalytics:
    def test_the_declared_stats_are_used_when_nothing_is_stored(self, panel, rf, person, orders):
        spec = listprefs.resolve(panel, get_request(rf, person))
        result = listprefs.analytics(spec, Order.objects.all())
        assert [s["label"] for s in result["stats"]] == ["Revenue", "Orders"]

    def test_a_stat_can_be_switched_off_by_key(self, panel, rf, person, orders):
        write(panel, person, stat_keys=["orders"])
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert [s.label for s in spec.stats] == ["Orders"]

    def test_switching_the_stat_strip_off_costs_no_queries(
        self, panel, rf, person, orders, django_assert_num_queries
    ):
        """Off has to remove the *queries*, not the markup. A version that computed the
        panel and then hid it would leave the page as slow as the thing the user turned
        off to make it faster."""
        write(panel, person, stats_enabled=False, charts_enabled=False)
        spec = listprefs.resolve(panel, get_request(rf, person))

        with django_assert_num_queries(0):
            result = listprefs.analytics(spec, Order.objects.all())

        assert result == {"stats": [], "charts": [], "deferred": False, "reason": None}

    def test_the_summary_bar_switched_off_costs_no_queries(
        self, panel, rf, person, orders, django_assert_num_queries
    ):
        write(panel, person, summary_enabled=False)
        spec = listprefs.resolve(panel, get_request(rf, person))
        with django_assert_num_queries(0):
            assert listprefs.summary(spec, Order.objects.all()) == {}

    def test_a_stat_added_from_the_ui_is_an_ordinary_stat(self, panel, rf, person, orders):
        """Reusing ``Stat`` rather than inventing a second aggregation path is the whole
        design: a UI-built card is computed by the same code as a declared one."""
        write(
            panel,
            person,
            stat_keys=[],
            extra_stats=[{"aggregate": "avg", "field": "total", "label": "Typical order"}],
        )
        spec = listprefs.resolve(panel, get_request(rf, person))
        added = spec.stats[-1]
        assert isinstance(added, Stat)

        result = listprefs.analytics(spec, Order.objects.all())
        typical = next(s for s in result["stats"] if s["label"] == "Typical order")
        assert typical["value"] == pytest.approx(20.0)

    def test_a_chart_added_from_the_ui_is_an_ordinary_chart(self, panel, rf, person, orders):
        write(
            panel,
            person,
            chart_keys=[],
            extra_charts=[
                {"mode": "breakdown", "target": "status", "aggregate": "count", "field": ""}
            ],
        )
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert isinstance(spec.charts[-1], Chart)

        result = listprefs.analytics(spec, Order.objects.all())
        assert result["charts"][-1]["series"][0]["data"] == [3]

    def test_a_ui_stat_never_collides_with_a_declared_key(self, panel, rf, person):
        write(
            panel,
            person,
            extra_stats=[{"aggregate": "sum", "field": "total", "label": "Revenue"}],
        )
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert len({stat.key for stat in spec.stats}) == len(spec.stats)


class TestSummaryBar:
    def test_inference_still_decides_when_nothing_is_stored(self, panel, rf, person):
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.summary_aggregates == {"total": "sum"}

    def test_a_column_can_be_switched_to_another_aggregate(self, panel, rf, person):
        write(panel, person, summary_aggregates={"total": "avg"})
        assert listprefs.resolve(panel, get_request(rf, person)).summary_aggregates == {
            "total": "avg"
        }

    def test_a_column_can_be_told_not_to_total_at_all(self, panel, rf, person):
        write(panel, person, summary_aggregates={"total": listprefs.NO_AGGREGATE})
        assert listprefs.resolve(panel, get_request(rf, person)).summary_aggregates == {}

    def test_the_bar_is_computed_in_one_query(
        self, panel, rf, person, orders, django_assert_num_queries
    ):
        spec = listprefs.resolve(panel, get_request(rf, person))
        with django_assert_num_queries(1):
            cells = listprefs.summary(spec, Order.objects.all())
        assert cells["total"].value == 60


class TestFacetCounts:
    def test_unset_leaves_the_question_to_the_query_string(self, panel, rf, person):
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.facets_on(get_request(rf, person)) is False
        assert spec.facets_on(get_request(rf, person, "/x/?_facets=1")) is True

    def test_always_on_ignores_the_query_string(self, panel, rf, person):
        write(panel, person, facet_counts=True)
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.facets_on(get_request(rf, person)) is True

    def test_never_ignores_the_query_string(self, panel, rf, person):
        write(panel, person, facet_counts=False)
        spec = listprefs.resolve(panel, get_request(rf, person))
        assert spec.facets_on(get_request(rf, person, "/x/?_facets=1")) is False


class TestOrdering:
    def test_the_position_boxes_decide_the_order(self, panel, rf, person):
        values = listprefs.clean_section(
            panel,
            get_request(rf, person),
            "columns",
            {"columns": ["number", "total"], "order__total": "1", "order__number": "2"},
        )
        assert values["columns"] == ["total", "number"]

    def test_a_form_with_no_positions_keeps_the_order_on_screen(self, panel, rf, person):
        values = listprefs.clean_section(
            panel, get_request(rf, person), "columns", {"columns": ["total", "number"]}
        )
        assert values["columns"] == ["total", "number"]


# ── the properties copied from siteconfig ───────────────────────────────────


class TestDatabaseUnavailable:
    def test_a_missing_table_falls_through_to_the_code(self, panel, rf, person):
        """``migrate`` on an empty database renders nothing, but ``check`` and a
        half-migrated deploy both read this layer. Raising here would take every list
        page down over a preference nobody set."""
        write(panel, person, columns=["total"])
        with hidden_table():
            spec = listprefs.resolve(panel, get_request(rf, person))
            assert spec.columns == ["number", "status", "total", "created_at"]
            assert spec.stored == {}

    def test_the_getter_override_is_none_with_no_table(self, panel, rf, person):
        write(panel, person, columns=["total"])
        with hidden_table():
            request = get_request(rf, person)
            listprefs.resolve(panel, request)
            assert listprefs.override(panel, request, "list_display") is None

    def test_a_test_without_a_database_still_resolves(self, panel, rf):
        """No ``db`` fixture: pytest-django blocks the connection outright, and drawing
        a list page's configuration is not a reason to fail such a test."""
        user = User(username="nobody", pk=1)
        spec = listprefs.resolve(panel, get_request(rf, user))
        assert spec.columns == ["number", "status", "total", "created_at"]

    def test_the_miss_is_not_cached_beyond_the_request(self, panel, rf, person):
        with hidden_table():
            listprefs.resolve(panel, get_request(rf, person))
        write(panel, person, columns=["total"])
        assert listprefs.resolve(panel, get_request(rf, person)).columns == ["total"]


class TestCaching:
    def test_both_rows_are_read_in_one_query(
        self, panel, rf, person, django_assert_num_queries
    ):
        write(panel, None, columns=["status"])
        write(panel, person, per_page=5)
        with django_assert_num_queries(1):
            listprefs.resolve(panel, get_request(rf, person))

    def test_a_second_ask_on_the_same_request_is_free(
        self, panel, rf, person, django_assert_num_queries
    ):
        request = get_request(rf, person)
        with django_assert_num_queries(1):
            listprefs.resolve(panel, request)
            listprefs.resolve(panel, request)
            listprefs.override(panel, request, "list_display")

    def test_two_requests_do_not_share_a_cache(self, panel, rf, person):
        first = get_request(rf, person)
        assert listprefs.resolve(panel, first).columns == [
            "number", "status", "total", "created_at"
        ]
        write(panel, person, columns=["total"])
        assert listprefs.resolve(panel, get_request(rf, person)).columns == ["total"]

    def test_resolving_does_not_recurse_through_the_getters(self, panel, rf, person):
        """The options and the baseline both come from the very getters this layer
        overrides. Without the re-entrancy switch that is an infinite loop."""
        write(panel, person, columns=["total"])
        request = get_request(rf, person)
        spec = listprefs.resolve(panel, request)
        assert spec.options.columns == ("number", "status", "total", "created_at")
        assert spec.columns == ["total"]

    def test_the_menu_is_drawn_from_the_code_not_from_the_choice(self, panel, rf, person):
        """Hiding a column must not remove it from the menu that brings it back."""
        write(panel, person, columns=["total"])
        context = listprefs.panel_context(panel, get_request(rf, person))
        names = [column["name"] for column in context["columns"]]
        assert names == ["number", "status", "total", "created_at"]
        assert [c["name"] for c in context["columns"] if c["selected"]] == ["total"]


# ── the rail's own view model ───────────────────────────────────────────────


class TestPanelContext:
    def test_it_offers_only_what_the_panel_exposes(self, panel, rf, person):
        context = listprefs.panel_context(panel, get_request(rf, person))
        assert [o["name"] for o in context["filter_options"]] == ["status", "created_at"]
        assert [o["name"] for o in context["search_options"]] == ["number"]
        assert [m["name"] for m in context["measures"]] == ["total"]
        assert [g["name"] for g in context["groups"]] == ["status"]
        assert [d["name"] for d in context["dates"]] == ["created_at"]

    def test_every_declared_stat_and_chart_is_listed(self, panel, rf, person):
        context = listprefs.panel_context(panel, get_request(rf, person))
        assert [o["label"] for o in context["stat_options"]] == ["Revenue", "Orders"]
        assert [o["label"] for o in context["chart_options"]] == ["By status"]
        assert all(o["selected"] for o in context["stat_options"])

    def test_a_section_reports_whether_it_is_overridden(self, panel, rf, person):
        write(panel, person, columns=["total"])
        context = listprefs.panel_context(panel, get_request(rf, person))
        assert context["overridden"]["columns"] is True
        assert context["overridden"]["filters"] is False

    def test_the_ordinary_user_is_not_offered_the_site_default(self, panel, rf, person):
        assert listprefs.panel_context(panel, get_request(rf, person))["may_set_default"] is False

    def test_a_superuser_is(self, panel, rf, boss):
        assert listprefs.panel_context(panel, get_request(rf, boss))["may_set_default"] is True


class TestTheRailRenders:
    def test_it_renders_without_a_route_mounted(self, panel, rf, boss):
        """The partial is included by the list page; it must not explode before the
        route that receives its POST exists."""
        from django.template.loader import render_to_string

        request = get_request(rf, boss)
        html = render_to_string(
            "vantage/partials/list_prefs.html",
            {"list_prefs": listprefs.panel_context(panel, request), "request": request},
        )
        assert 'name="section" value="columns"' in html
        assert 'value="save:site"' in html

    def test_it_offers_no_way_to_save_for_everyone_to_someone_who_may_not(
        self, panel, rf, person
    ):
        from django.template.loader import render_to_string

        request = get_request(rf, person)
        html = render_to_string(
            "vantage/partials/list_prefs.html",
            {"list_prefs": listprefs.panel_context(panel, request), "request": request},
        )
        assert 'value="save:site"' not in html
        assert 'value="save:user"' in html


# ── the schema and the layer agree ──────────────────────────────────────────


class TestTheSchemaAndTheLayerAgree:
    def test_every_setting_column_belongs_to_exactly_one_section(self):
        assert set(SETTING_COLUMNS) == set(listprefs.SETTING_NAMES)
        assert len(listprefs.SETTING_NAMES) == len(set(listprefs.SETTING_NAMES))

    def test_every_setting_column_has_a_validator(self):
        assert set(SETTING_COLUMNS) == set(listprefs._CLEANERS)

    def test_every_section_is_reachable_from_the_view(self):
        from vantage.listprefsview import SECTION_LABELS

        assert set(SECTION_LABELS) == set(listprefs.SECTIONS)

    def test_the_aggregate_vocabulary_is_the_summary_bars(self):
        from vantage.summary import SUMMARY_FUNCTIONS

        assert set(listprefs.AGGREGATES) == set(SUMMARY_FUNCTIONS)


class TestPreferencesApplyWhereTheRailIs:
    """``override`` reads a spec somebody already resolved, and never resolves one.

    The list page calls :func:`~vantage.listprefs.resolve` once, early, and that is what
    arms the getters for the rest of the request. Everything else that asks a panel a
    question — ``check``, ``scaffold``, the registry, a dashboard's table card — gets
    the code's answer and pays no query for it.
    """

    def test_an_unprimed_getter_override_is_none(self, panel, rf, person):
        write(panel, person, columns=["total"])
        assert listprefs.override(panel, get_request(rf, person), "list_display") is None

    def test_an_unprimed_getter_override_costs_no_queries(
        self, panel, rf, person, django_assert_num_queries
    ):
        write(panel, person, columns=["total"])
        request = get_request(rf, person)
        with django_assert_num_queries(0):
            listprefs.override(panel, request, "list_display")
            listprefs.override(panel, request, "list_filter")

    def test_resolving_arms_it_for_the_rest_of_the_request(self, panel, rf, person):
        write(panel, person, columns=["total"])
        request = get_request(rf, person)
        listprefs.resolve(panel, request)
        assert listprefs.override(panel, request, "list_display") == ["total"]

    def test_priming_is_per_model_not_per_panel_object(self, panel, bare, rf, person):
        """The cache is keyed by the resource key, which is the model.

        Two panel objects for one model are the same list as far as a stored preference
        is concerned — the row names the model, so it has to be.
        """
        write(panel, person, columns=["total"])
        request = get_request(rf, person)
        listprefs.resolve(panel, request)
        assert listprefs.override(bare, request, "list_display") == ["total"]

    def test_another_models_panel_is_untouched(self, panel, rf, person):
        from tests.testapp.models import Post

        write(panel, person, columns=["total"])
        request = get_request(rf, person)
        listprefs.resolve(panel, request)
        assert listprefs.override(ModelPanel(Post, site), request, "list_display") is None
