"""The dashboard: widget cards declared in Python.

Two things carry most of the weight here. The first is that a project which declares
nothing still gets a dashboard worth opening — that is the package's whole thesis, so
the zero-config path is tested as hard as the configured one. The second is that a
widget is inert until rendered: it must not query at declaration time, must not query
for a user who may not see it, and must not take the page down when it fails.
"""

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db.models import Avg, Count, Sum
from django.test import override_settings
from django.urls import reverse

from tests.testapp.models import Author, Comment, Order, Post, Tag
from vantage import dashboards, site
from vantage.dashboards import (
    ChartCard,
    Dashboard,
    DefaultDashboard,
    ListCard,
    StatCard,
    TableCard,
    TemplateCard,
    Widget,
    creation_field,
    dashboard_context,
    get_dashboard,
)
from vantage.stats import Chart

#: Declared at module scope on purpose: this is how a real dashboard is written, and
#: the queryset it holds must never carry one request's rows into the next.
MODULE_LEVEL_POSTS = Post.objects.all()


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def request_from(rf):
    def build(user):
        request = rf.get("/")
        request.user = user
        return request

    return build


@pytest.fixture
def content(db):
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    posts = [
        Post.objects.create(
            title=f"Post {i}",
            slug=f"post-{i}",
            body="Body",
            author=author,
            view_count=i * 10,
            status=Post.Status.PUBLISHED if i % 2 else Post.Status.DRAFT,
        )
        for i in range(1, 4)
    ]
    Comment.objects.create(post=posts[0], author_name="Grace", body="Nice")
    Order.objects.create(number="A-1", customer=author, total="10.00")
    Order.objects.create(number="A-2", customer=author, total="30.00")
    return posts


@pytest.fixture
def wired(monkeypatch):
    """``DashboardView``, carrying the wiring it is about to be given.

    ``views.py`` belongs to another change; this applies the exact three lines it gains
    so the integration tests below exercise the real request path. It is a no-op once
    the view supplies ``widgets`` itself, so these tests keep passing unchanged.
    """
    from vantage import views

    original = views.DashboardView.get_context_data

    def get_context_data(self, **kwargs):
        context = original(self, **kwargs)
        if "widgets" not in context:
            context.update(dashboard_context(self.site, self.request, self.permissions))
        return context

    monkeypatch.setattr(views.DashboardView, "get_context_data", get_context_data)


@pytest.fixture
def client_in(client, boss, wired):
    client.force_login(boss)
    return client


# ── declaration ─────────────────────────────────────────────────────────────


class TestDeclaration:
    def test_declaring_a_widget_runs_no_queries(self, db, django_assert_num_queries):
        """A dashboard module is imported once, at startup. It must cost nothing."""
        with django_assert_num_queries(0):
            StatCard("Posts", Post.objects.all(), aggregate=Count("pk"))
            ChartCard(
                "Over time",
                Post.objects.all(),
                chart=Chart.timeseries("Posts", date="created_at", value=Count("pk")),
            )
            TableCard("Recent", Post.objects.all())
            ListCard("Facts", lambda request: [("a", 1)])
            TemplateCard("Custom", "vantage/partials/stat_card.html")

    def test_widths_default_to_the_shape_of_the_card(self):
        assert StatCard("Posts", Post.objects.all()).width == 1
        assert TableCard("Recent", Post.objects.all()).width == 2
        assert ListCard("Facts", []).width == 1

    def test_an_impossible_width_is_refused_at_declaration(self):
        with pytest.raises(ValueError, match="4 columns wide"):
            StatCard("Posts", Post.objects.all(), width=5)
        with pytest.raises(ValueError, match="4 columns wide"):
            StatCard("Posts", Post.objects.all(), width=0)

    def test_a_widget_knows_the_model_it_is_about(self):
        assert StatCard("Posts", Post.objects.all()).get_model() is Post
        assert StatCard("Posts", Post.objects).get_model() is Post
        assert TableCard("Recent", Post.objects.all()).get_model() is Post

    def test_a_callable_source_declares_its_model_explicitly(self):
        """A callable cannot be inspected without running it, so it is asked instead."""
        card = StatCard("Posts", lambda request: Post.objects.all())
        assert card.get_model() is None
        assert StatCard("Posts", lambda r: Post.objects.all(), model=Post).get_model() is Post

    def test_the_base_widget_has_no_opinion_about_rendering(self, request_from, boss):
        with pytest.raises(NotImplementedError):
            Widget("Bare").render(request_from(boss))


# ── resolution from settings ────────────────────────────────────────────────


class CustomDashboard(Dashboard):
    """Resolved by dotted path from ``VANTAGE["DASHBOARD"]`` in the tests below."""

    title = "Mission Control"
    description = "Everything, one screen."

    def get_widgets(self, request):
        return [StatCard("Published posts", Post.objects.filter(status="published"))]


class NotADashboard:
    pass


class TestResolution:
    def test_no_setting_means_the_default_dashboard(self):
        assert isinstance(get_dashboard(), DefaultDashboard)

    @override_settings(VANTAGE={"DASHBOARD": "tests.test_dashboards.CustomDashboard"})
    def test_a_dotted_path_is_resolved(self):
        dashboard = get_dashboard()
        assert isinstance(dashboard, CustomDashboard)
        assert dashboard.get_title() == "Mission Control"

    @override_settings(VANTAGE={"DASHBOARD": "tests.test_dashboards.NotADashboard"})
    def test_something_that_is_not_a_dashboard_says_so(self):
        with pytest.raises(ImproperlyConfigured, match="not a Dashboard subclass"):
            get_dashboard()

    def test_a_title_falls_back_to_the_site_title(self):
        assert Dashboard().get_title() == "Django Vantage"

    @override_settings(VANTAGE={"DASHBOARD": "tests.test_dashboards.CustomDashboard"})
    def test_the_custom_dashboard_reaches_the_page(self, client_in, content):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "Mission Control" in body
        assert "Published posts" in body
        assert "Everything, one screen." in body


# ── the widget types ────────────────────────────────────────────────────────


def render_one(widget, request):
    """One widget through the dashboard, exactly as a request would."""

    class OneCard(Dashboard):
        def get_widgets(self, _request):
            return [widget]

    cards = OneCard().render(request)
    assert len(cards) == 1
    return cards[0]


class TestWidgetTypes:
    def test_a_stat_card_counts_by_default(self, request_from, boss, content):
        card = render_one(StatCard("Posts", Post.objects.all()), request_from(boss))
        assert card.kind == "stat"
        assert card.stat["display"] == "3"

    def test_a_stat_card_takes_any_aggregate_and_format(self, request_from, boss, content):
        card = render_one(
            StatCard(
                "Revenue",
                Order.objects.all(),
                aggregate=Sum("total"),
                format="currency",
                tone="success",
            ),
            request_from(boss),
        )
        assert card.stat["display"] == "$40.00"
        assert card.stat["tone"] == "success"

    def test_a_stat_card_costs_one_query(
        self, request_from, boss, content, django_assert_num_queries
    ):
        with django_assert_num_queries(1):
            render_one(
                StatCard("Average", Order.objects.all(), aggregate=Avg("total"), format="currency"),
                request_from(boss),
            )

    def test_a_stat_card_accepts_a_callable_source(self, request_from, boss, content):
        card = render_one(
            StatCard("Drafts", lambda request: Post.objects.filter(status="draft"), model=Post),
            request_from(boss),
        )
        assert card.stat["display"] == "1"

    def test_a_stat_card_accepts_a_zero_argument_callable(self, request_from, boss, content):
        card = render_one(StatCard("Posts", Post.objects.all, model=Post), request_from(boss))
        assert card.stat["display"] == "3"

    def test_a_chart_card_renders_svg(self, request_from, boss, content):
        card = render_one(
            ChartCard(
                "Posts over time",
                Post.objects.all(),
                chart=Chart.timeseries("Posts", date="created_at", value=Count("pk")),
            ),
            request_from(boss),
        )
        assert card.kind == "chart"
        assert card.chart["empty"] is False
        assert card.chart["bucket"] == "day"

    def test_a_chart_card_can_break_down_by_a_field(self, request_from, boss, content):
        card = render_one(
            ChartCard(
                "By status",
                Post.objects.all(),
                chart=Chart.breakdown("Posts", group_by="status", value=Count("pk"), kind="donut"),
            ),
            request_from(boss),
        )
        assert sorted(card.chart["labels"]) == ["Draft", "Published"]

    def test_a_table_card_lists_the_most_recent_records(self, request_from, boss, content):
        card = render_one(
            TableCard("Recent posts", Post.objects.all(), limit=2), request_from(boss)
        )
        assert card.kind == "table"
        assert len(card.rows) == 2
        assert card.columns  # inferred from the model when not declared
        assert card.rows[0]["url"].endswith(f"/{content[-1].pk}/")

    def test_a_table_card_honours_declared_columns_and_links_the_first(
        self, request_from, boss, content
    ):
        card = render_one(
            TableCard("Recent", Post.objects.all(), columns=["title", "status"]),
            request_from(boss),
        )
        assert card.columns == ("Title", "Status")
        assert card.rows[0]["cells"][0]["is_link"] is True

    def test_a_table_card_does_not_n_plus_one_on_a_relation_column(
        self, request_from, boss, content, django_assert_num_queries
    ):
        """Five rows with an FK column is five extra queries unless the join is planned."""
        with django_assert_num_queries(1):
            card = render_one(
                TableCard("Recent", Post.objects.all(), columns=["title", "author"]),
                request_from(boss),
            )
            assert len(card.rows) == 3

    def test_a_table_card_offers_a_way_through_to_the_list(self, request_from, boss, content):
        card = render_one(TableCard("Recent", Post.objects.all()), request_from(boss))
        assert card.url == reverse("vantage:testapp-post-list")

    def test_an_empty_table_card_says_so_rather_than_rendering_a_bare_frame(
        self, request_from, boss, db
    ):
        card = render_one(TableCard("Recent", Post.objects.all()), request_from(boss))
        assert card.empty is True
        assert card.rows == ()

    def test_a_list_card_takes_pairs_or_dicts(self, request_from, boss, content):
        card = render_one(
            ListCard("Facts", [("Posts", 3), {"label": "Tags", "value": 0, "tone": "warning"}]),
            request_from(boss),
        )
        assert card.kind == "list"
        assert card.items[0] == {"label": "Posts", "value": 3, "url": None, "tone": None}
        assert card.items[1]["tone"] == "warning"

    def test_a_list_card_resolves_its_callable_per_request(self, request_from, boss, content):
        card = ListCard("Facts", lambda request: [("Posts", Post.objects.count())])
        assert render_one(card, request_from(boss)).items[0]["value"] == 3
        Post.objects.create(title="Extra", slug="extra", body="b", author=content[0].author)
        assert render_one(card, request_from(boss)).items[0]["value"] == 4

    def test_a_template_card_renders_its_own_template(self, request_from, boss, content):
        card = render_one(
            TemplateCard(
                "Anything",
                "vantage/partials/stat_card.html",
                context={"stat": {"label": "Hand rolled", "display": "42"}},
            ),
            request_from(boss),
        )
        assert card.kind == "template"
        assert "Hand rolled" in card.html
        assert "42" in card.html


# ── permissions ─────────────────────────────────────────────────────────────


class TestPermissions:
    def test_a_forbidden_model_neither_renders_nor_queries(
        self, db, request_from, django_assert_num_queries
    ):
        """The gate is before the query, not after it.

        A card that computes a number and then declines to show it has already paid for
        it and already put it in the query log.
        """
        user = User.objects.create_user("plain", password="x", is_staff=True)
        request = request_from(user)
        user.has_perm("testapp.view_post")  # warm the permission cache

        class Board(Dashboard):
            def get_widgets(self, _request):
                return [StatCard("Posts", Post.objects.all())]

        with django_assert_num_queries(0):
            assert Board().render(request) == []

    def test_the_same_widget_renders_once_the_permission_is_granted(self, db, request_from):
        user = User.objects.create_user("plain", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_post"))
        user = User.objects.get(pk=user.pk)  # drop the permission cache

        class Board(Dashboard):
            def get_widgets(self, _request):
                return [StatCard("Posts", Post.objects.all())]

        assert len(Board().render(request_from(user))) == 1

    def test_an_explicit_permission_string_gates_a_modelless_widget(self, db, request_from):
        user = User.objects.create_user("plain", password="x", is_staff=True)
        card = ListCard("Secrets", [("a", 1)], permission="testapp.view_post")
        assert card.has_permission(request_from(user)) is False

    def test_a_callable_permission_can_gate_on_anything(self, db, request_from, boss):
        card = ListCard("Beta", [("a", 1)], permission=lambda request: request.user.is_superuser)
        assert card.has_permission(request_from(boss)) is True
        plain = User.objects.create_user("plain", password="x", is_staff=True)
        assert card.has_permission(request_from(plain)) is False

    def test_the_default_dashboard_shows_only_what_the_user_may_see(self, db, request_from):
        user = User.objects.create_user("plain", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_post"))
        user = User.objects.get(pk=user.pk)

        labels = {card.label for card in DefaultDashboard().render(request_from(user))}
        assert "Posts" in labels
        assert "Authors" not in labels

    def test_the_card_cap_never_becomes_an_access_rule(self, db, request_from):
        """Capping before permission-filtering emptied the dashboard for a user whose
        one visible model happened to sort outside the first N."""
        from vantage.dashboards import MAX_DEFAULT_STATS
        from vantage.sites import site

        assert len(list(site.panels())) > MAX_DEFAULT_STATS, "fixture no longer exercises this"

        user = User.objects.create_user("capped", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_post"))
        user = User.objects.get(pk=user.pk)

        cards = DefaultDashboard().render(request_from(user))
        assert cards, "a restricted user got an empty dashboard"
        assert {c.label for c in cards if c.kind == "stat"} == {"Posts"}


# ── failure isolation ───────────────────────────────────────────────────────


class ExplodingCard(Widget):
    kind = "stat"

    def render(self, request):
        raise RuntimeError("the aggregate is nonsense")


class TestFailureIsolation:
    def test_a_raising_widget_becomes_an_error_card(self, request_from, boss, content):
        cards = _render_all(
            [
                StatCard("Posts", Post.objects.all()),
                ExplodingCard("Broken"),
                StatCard("Authors", Author.objects.all()),
            ],
            request_from(boss),
        )
        assert [c.error for c in cards] == [None, "the aggregate is nonsense", None]
        assert cards[0].stat["display"] == "3"
        assert cards[2].stat["display"] == "1"

    def test_an_error_card_keeps_its_identity(self, request_from, boss, db):
        card = render_one(ExplodingCard("Broken", width=2), request_from(boss))
        assert (card.label, card.width, card.empty) == ("Broken", 2, True)

    def test_a_broken_template_card_fails_alone(self, request_from, boss, content):
        cards = _render_all(
            [
                TemplateCard("Missing", "vantage/does-not-exist.html"),
                StatCard("Posts", Post.objects.all()),
            ],
            request_from(boss),
        )
        assert cards[0].error is not None
        assert cards[1].stat["display"] == "3"

    def test_a_bad_source_is_an_error_card_not_a_crash(self, request_from, boss, db):
        card = render_one(
            StatCard("Nonsense", lambda request: "not a queryset"), request_from(boss)
        )
        assert "needs a queryset" in card.error

    def test_the_page_still_renders_with_a_broken_widget(self, client_in, content, settings):
        settings.VANTAGE = {"DASHBOARD": "tests.test_dashboards.HalfBrokenDashboard"}
        response = client_in.get(reverse("vantage:dashboard"))
        body = response.content.decode()
        assert response.status_code == 200
        assert "could not be computed" in body
        assert "v-widget-failed" in body
        assert "Posts" in body


class HalfBrokenDashboard(Dashboard):
    def get_widgets(self, request):
        return [ExplodingCard("Broken"), StatCard("Posts", Post.objects.all())]


def _render_all(widgets, request):
    class Board(Dashboard):
        def get_widgets(self, _request):
            return widgets

    return Board().render(request)


# ── laziness ────────────────────────────────────────────────────────────────


class TestLaziness:
    def test_a_module_level_queryset_is_recloned_per_request(self, request_from, boss, content):
        """A queryset caches its rows once iterated. Serving that cache to the next
        request is the classic module-level-queryset bug, and a dashboard is exactly
        where it is written."""
        first = render_one(TableCard("Recent", MODULE_LEVEL_POSTS, limit=10), request_from(boss))
        assert len(first.rows) == 3

        Post.objects.create(title="Fourth", slug="fourth", body="b", author=content[0].author)
        second = render_one(TableCard("Recent", MODULE_LEVEL_POSTS, limit=10), request_from(boss))
        assert len(second.rows) == 4

    def test_get_widgets_is_not_called_until_render(self, db, request_from, boss):
        calls = []

        class Board(Dashboard):
            def get_widgets(self, request):
                calls.append(request)
                return [StatCard("Posts", Post.objects.all())]

        board = Board()
        assert calls == []
        board.render(request_from(boss))
        assert len(calls) == 1


# ── the zero-config dashboard ───────────────────────────────────────────────


class TestDefaultDashboard:
    def test_a_stat_card_per_registered_model(self, request_from, boss, content):
        """Counts, not names.

        This used to assert on specific models and broke the moment the fixture app
        grew past MAX_DEFAULT_STATS: the cap takes the first N in registry order, so
        which models appear is a function of how many exist.
        """
        from vantage.dashboards import MAX_DEFAULT_STATS
        from vantage.sites import site

        cards = DefaultDashboard().render(request_from(boss))
        stats = [card for card in cards if card.kind == "stat"]
        assert stats
        assert len(stats) == min(len(list(site.panels())), MAX_DEFAULT_STATS)
        assert all(card.stat["display"].isdigit() for card in stats)

    def test_stat_cards_link_to_their_list_page(self, request_from, boss, content):
        cards = DefaultDashboard().render(request_from(boss))
        card = next(c for c in cards if c.kind == "stat" and c.url)
        assert card.url.startswith("/admin/")

    def test_a_recent_table_for_models_that_record_when_rows_were_created(
        self, request_from, boss, content
    ):
        cards = DefaultDashboard().render(request_from(boss))
        tables = {card.label for card in cards if card.kind == "table"}
        assert tables
        assert all(label.startswith("Recent ") for label in tables)

    def test_a_model_with_no_timestamp_gets_no_table(self, request_from, boss, content):
        cards = DefaultDashboard().render(request_from(boss))
        assert "Recent tags" not in {c.label for c in cards if c.kind == "table"}

    def test_it_charts_new_records_over_time(self, request_from, boss, content):
        cards = DefaultDashboard().render(request_from(boss))
        assert any(card.kind == "chart" for card in cards)

    def test_it_renders_with_zero_configuration(self, client_in, content):
        """The package's thesis, as one assertion."""
        response = client_in.get(reverse("vantage:dashboard"))
        body = response.content.decode()
        assert response.status_code == 200
        assert "v-dashboard" in body
        assert "Posts" in body
        assert "Recent posts" in body or "Recent comments" in body

    def test_it_survives_an_empty_database(self, client_in, db):
        response = client_in.get(reverse("vantage:dashboard"))
        assert response.status_code == 200
        assert "Nothing here yet." in response.content.decode()

    def test_the_grid_declares_each_card_width(self, client_in, content):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert 'data-width="1"' in body
        assert 'data-width="2"' in body

    def test_the_dashboard_stylesheet_is_loaded(self, client_in, content):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "vantage-dashboard.css" in body


class TestCreationField:
    def test_auto_now_add_is_proof(self):
        assert creation_field(Post) == "created_at"
        assert creation_field(Comment) == "created_at"

    def test_a_conventional_name_is_enough(self):
        assert creation_field(User) == "date_joined"

    def test_a_model_without_one_says_none(self):
        assert creation_field(Tag) is None


# ── arranging the dashboard from the UI ─────────────────────────────────────
#
# The layer under everything above. Its rules are the ones that go wrong quietly, so
# they are the ones tested hardest: the resolution order, the permission gate landing
# before the query rather than after it, and the query cost staying a function of how
# many cards there are rather than how many rows they cover.


class DeclaredDashboard(Dashboard):
    """Three declared cards with stable keys, for the reorder/hide/resize tests."""

    title = "Declared"

    def get_widgets(self, request):
        return [
            StatCard("Posts", Post.objects.all()),
            StatCard("Authors", Author.objects.all()),
            StatCard("Orders", Order.objects.all()),
        ]


declared_settings = override_settings(
    VANTAGE={"DASHBOARD": "tests.test_dashboards.DeclaredDashboard"}
)


class GrowableDashboard(Dashboard):
    """One declared card to start with; more can be added in Python later, exactly
    like editing a real dashboard module between two requests."""

    title = "Growable"
    widgets: list = [StatCard("Posts", Post.objects.all())]

    def get_widgets(self, request):
        return list(GrowableDashboard.widgets)


growable_settings = override_settings(
    VANTAGE={"DASHBOARD": "tests.test_dashboards.GrowableDashboard"}
)


def stat_card(**overrides):
    """A UI-built stat entry over Orders, as the picker would produce it."""
    return {
        "card": "stat",
        "model": "testapp.order",
        "aggregate": "sum",
        "field": "total",
        "label": "Takings",
        **overrides,
    }


def chart_card(**overrides):
    return {
        "card": "chart",
        "model": "testapp.order",
        "mode": "timeseries",
        "target": "created_at",
        "aggregate": "count",
        "field": "",
        "kind": "line",
        "label": "Orders over time",
        **overrides,
    }


def save(site, request, cards, *, user):
    dashboards.store_layout(site, request, dashboards.clean_cards(cards, site, request), user=user)


@pytest.fixture
def orders(db):
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    Order.objects.create(number="A-1", customer=author, total="10.00", status="paid")
    Order.objects.create(number="A-2", customer=author, total="30.00", status="pending")
    return author


class TestValidation:
    """Everything arriving from a form or from a row written months ago is hostile."""

    def test_a_well_formed_card_survives_and_is_normalised(self, db, request_from, boss):
        entry = dashboards.clean_card(stat_card(), site, request_from(boss))
        assert entry["card"] == "stat"
        assert entry["aggregate"] == "sum"
        assert entry["field"] == "total"
        # Width and filter are filled in rather than left to the template to guess.
        assert entry["width"] == 1
        assert entry["filter"] == ""
        assert entry["show"] is True

    def test_a_model_that_is_not_registered_is_dropped(self, db, request_from, boss):
        assert (
            dashboards.clean_card(stat_card(model="auth.permission"), site, request_from(boss))
            is None
        )
        assert dashboards.clean_card(stat_card(model="nope.nope"), site, request_from(boss)) is None

    def test_a_column_the_panel_does_not_expose_is_dropped(self, db, request_from, boss):
        """The panel is the ceiling, and it is checked on the way out as well as in."""
        assert dashboards.clean_card(stat_card(field="id"), site, request_from(boss)) is None
        assert dashboards.clean_card(stat_card(field="number"), site, request_from(boss)) is None

    def test_an_invented_aggregate_is_dropped(self, db, request_from, boss):
        assert dashboards.clean_card(stat_card(aggregate="drop"), site, request_from(boss)) is None

    def test_counting_rows_needs_no_column(self, db, request_from, boss):
        entry = dashboards.clean_card(
            stat_card(aggregate="count", field="", label=""), site, request_from(boss)
        )
        assert entry["field"] == ""
        assert entry["label"] == "Count"

    def test_a_chart_must_group_by_something_the_panel_offers(self, db, request_from, boss):
        request = request_from(boss)
        assert dashboards.clean_card(chart_card(), site, request)["target"] == "created_at"
        # `total` is a measure, not a date, so it cannot be a timeseries axis.
        assert dashboards.clean_card(chart_card(target="total"), site, request) is None
        # `status` is low-cardinality and labelled, so it can be a breakdown.
        broken_down = chart_card(mode="breakdown", target="status", kind="donut")
        assert dashboards.clean_card(broken_down, site, request)["kind"] == "donut"
        # `number` is unique free text: one segment per row is not a chart.
        assert (
            dashboards.clean_card(chart_card(mode="breakdown", target="number"), site, request)
            is None
        )

    def test_an_impossible_width_falls_back_rather_than_raising(self, db, request_from, boss):
        request = request_from(boss)
        assert dashboards.clean_card(stat_card(width=9), site, request)["width"] == 1
        assert dashboards.clean_card(stat_card(width="two"), site, request)["width"] == 1
        assert dashboards.clean_card(stat_card(width=3), site, request)["width"] == 3

    def test_a_filter_is_re_expressed_through_the_list_pages_own_machinery(
        self, db, request_from, boss
    ):
        request = request_from(boss)
        entry = dashboards.clean_card(stat_card(filter="status=paid"), site, request)
        assert entry["filter"] == "status=paid"
        # A parameter the panel does not offer as a filter is not a filter.
        assert dashboards.clean_card(stat_card(filter="body=secret"), site, request)["filter"] == ""
        # And neither is a value the filter itself refuses.
        assert (
            dashboards.clean_card(stat_card(filter="status=nonsense"), site, request)["filter"]
            == ""
        )

    def test_a_filter_across_a_fan_out_join_is_refused(self, db, request_from, boss):
        """Django #10060, one layer up.

        A to-many join multiplies the rows an aggregate sees. A list page hides that
        with DISTINCT; a sum cannot, and would quietly report a number nobody can
        reproduce.
        """
        entry = dashboards.clean_card(
            {**stat_card(model="testapp.post", field="view_count"), "filter": "tags=1"},
            site,
            request_from(boss),
        )
        assert entry["filter"] == ""

    def test_junk_is_dropped_rather_than_raising(self, db, request_from, boss):
        request = request_from(boss)
        for junk in (None, "card", 7, [], {"card": "wat"}, {"card": "stat"}):
            assert dashboards.clean_card(junk, site, request) is None

    def test_an_arrangement_that_has_entirely_stopped_making_sense_falls_back(
        self, db, request_from, boss
    ):
        request = request_from(boss)
        assert dashboards.clean_cards([stat_card(model="gone.gone")], site, request) is None
        # But one somebody deliberately emptied stays empty.
        assert dashboards.clean_cards([], site, request) == []

    def test_only_so_many_cards_may_be_stored(self, db, request_from, boss):
        request = request_from(boss)
        cards = [stat_card(label=f"Card {i}") for i in range(dashboards.MAX_BUILT_CARDS + 5)]
        assert len(dashboards.clean_cards(cards, site, request)) == dashboards.MAX_BUILT_CARDS

    def test_one_entry_per_declared_card(self, db, request_from, boss):
        cards = [{"card": "declared", "key": "posts"}] * 3
        assert len(dashboards.clean_cards(cards, site, request_from(boss))) == 1


class TestResolutionOrder:
    """UI override → VANTAGE["DASHBOARD"] → the default dashboard."""

    def test_without_a_request_the_code_decides(self, db):
        assert isinstance(get_dashboard(), DefaultDashboard)
        assert isinstance(get_dashboard(site), DefaultDashboard)

    @declared_settings
    def test_nothing_stored_means_the_declared_dashboard(self, db, request_from, boss):
        assert isinstance(get_dashboard(site, request_from(boss)), DeclaredDashboard)

    @declared_settings
    def test_a_stored_arrangement_sits_above_the_declaration(self, db, request_from, boss, orders):
        request = request_from(boss)
        save(site, request, [stat_card()], user=boss)

        dashboard = get_dashboard(site, request_from(boss))
        assert isinstance(dashboard, dashboards.ArrangedDashboard)
        # The declaration is still what says what each declared card *is*.
        assert isinstance(dashboard.base, DeclaredDashboard)
        labels = [card.label for card in dashboard.render(request_from(boss))]
        assert labels[0] == "Takings"

    @declared_settings
    def test_a_personal_arrangement_beats_the_site_wide_one(self, db, request_from, boss):
        """Each arrangement is built entirely of cards somebody chose, so nothing
        declared and never mentioned rides along with it — see ``TestArranging`` for
        the cards that were mentioned or are new since the save."""
        other = User.objects.create_user("other", password="x", is_staff=True)
        other.user_permissions.add(Permission.objects.get(codename="view_order"))
        other = User.objects.get(pk=other.pk)  # drop the permission cache
        request = request_from(boss)
        save(site, request, [stat_card(label="Everyone's")], user=None)
        save(site, request, [stat_card(label="Mine")], user=boss)

        mine = get_dashboard(site, request_from(boss)).render(request_from(boss))
        theirs = get_dashboard(site, request_from(other)).render(request_from(other))
        assert [c.label for c in mine] == ["Mine"]
        assert [c.label for c in theirs] == ["Everyone's"]

    @declared_settings
    def test_reset_hands_the_page_back_to_the_code(self, db, request_from, boss, orders):
        request = request_from(boss)
        save(site, request, [stat_card()], user=boss)
        assert isinstance(get_dashboard(site, request_from(boss)), dashboards.ArrangedDashboard)

        dashboards.reset_layout(site, request_from(boss), user=boss)
        assert isinstance(get_dashboard(site, request_from(boss)), DeclaredDashboard)
        # And the row is gone rather than left as a tombstone of nulls.
        assert dashboards.row_for(site, boss) is None

    @declared_settings
    def test_reset_restores_what_the_code_declares_now_not_what_it_declared_then(
        self, db, request_from, boss
    ):
        """The point of Reset is that Python becomes the answer *and stays* it."""
        request = request_from(boss)
        save(site, request, [{"card": "declared", "key": "posts"}], user=boss)
        dashboards.reset_layout(site, request_from(boss), user=boss)

        labels = [c.label for c in get_dashboard(site, request_from(boss)).render(request)]
        assert labels == ["Posts", "Authors", "Orders"]

    def test_the_layout_is_read_once_per_request(
        self, db, request_from, boss, django_assert_num_queries
    ):
        request = request_from(boss)
        save(site, request, [stat_card()], user=boss)

        fresh = request_from(boss)
        with django_assert_num_queries(1):
            dashboards.stored_layout(site, fresh)
            dashboards.stored_layout(site, fresh)
            dashboards.stored_layout(site, fresh)


class TestArranging:
    """Reorder, resize and hide — over the cards the code declares as well as the
    cards somebody built."""

    @declared_settings
    def test_a_declared_card_can_be_reordered(self, db, request_from, boss, content):
        request = request_from(boss)
        save(
            site,
            request,
            [{"card": "declared", "key": key} for key in ("orders", "authors", "posts")],
            user=boss,
        )
        labels = [c.label for c in get_dashboard(site, request_from(boss)).render(request)]
        assert labels == ["Orders", "Authors", "Posts"]

    @declared_settings
    def test_a_declared_card_can_be_switched_off_and_back_on(self, db, request_from, boss, content):
        request = request_from(boss)
        save(
            site,
            request,
            [
                {"card": "declared", "key": "posts", "show": False},
                {"card": "declared", "key": "authors"},
                {"card": "declared", "key": "orders"},
            ],
            user=boss,
        )
        assert [c.label for c in get_dashboard(site, request_from(boss)).render(request)] == [
            "Authors",
            "Orders",
        ]

        save(site, request_from(boss), [{"card": "declared", "key": "posts"}], user=boss)
        assert "Posts" in {c.label for c in get_dashboard(site, request_from(boss)).render(request)}

    @declared_settings
    def test_a_declared_card_can_be_resized(self, db, request_from, boss, content):
        request = request_from(boss)
        save(site, request, [{"card": "declared", "key": "posts", "width": 3}], user=boss)
        card = get_dashboard(site, request_from(boss)).render(request)[0]
        assert (card.label, card.width) == ("Posts", 3)

    @growable_settings
    def test_a_card_added_in_python_later_is_appended_rather_than_swallowed(
        self, db, request_from, boss, content, monkeypatch
    ):
        """A developer adding a card must not have it hidden by an arrangement saved
        before it existed — the same choice NAV_UNLISTED = "append" makes.

        A key already declared when the arrangement was saved and then left unmentioned
        is a removal (see ``test_a_personal_arrangement_beats_the_site_wide_one`` and
        ``test_a_narrower_colleague_rearranging_cannot_delete_what_they_never_saw``); a
        key that did not exist yet is not, which is the distinction this test needs a
        real temporal gap to exercise: the dashboard only declares "Posts" at save time,
        and gains "Authors" afterwards, in Python, exactly as a developer would.
        """
        request = request_from(boss)
        save(site, request, [{"card": "declared", "key": "posts"}], user=boss)

        monkeypatch.setattr(
            GrowableDashboard,
            "widgets",
            [StatCard("Posts", Post.objects.all()), StatCard("Authors", Author.objects.all())],
        )

        labels = [c.label for c in get_dashboard(site, request_from(boss)).render(request)]
        assert labels[0] == "Posts"
        assert set(labels) == {"Posts", "Authors"}

    @declared_settings
    def test_an_entry_for_a_card_the_code_no_longer_declares_is_inert(
        self, db, request_from, boss, content
    ):
        """A stale reference — a key from a card the code has since renamed or
        removed — is skipped rather than crashing the page. The arrangement also
        names every card still declared, so this is purely about the dead entry: it
        does not lean on "unmentioned declared cards are appended", which is a
        separate, narrower rule now that an unmentioned card can just as easily mean
        it was deliberately removed."""
        request = request_from(boss)
        save(
            site,
            request,
            [
                {"card": "declared", "key": "revenue-2019"},
                {"card": "declared", "key": "posts"},
                {"card": "declared", "key": "authors"},
                {"card": "declared", "key": "orders"},
            ],
            user=boss,
        )
        labels = {c.label for c in get_dashboard(site, request_from(boss)).render(request)}
        assert labels == {"Posts", "Authors", "Orders"}

    def test_two_cards_with_one_label_get_distinct_keys(self):
        widgets = [
            StatCard("Revenue", Order.objects.all()),
            StatCard("Revenue", Post.objects.all()),
        ]
        assert list(dashboards.keyed_widgets(widgets)) == ["revenue", "revenue-2"]

    def test_a_declared_key_survives_a_reworded_label(self):
        assert StatCard("Anything", Post.objects.all(), key="pinned").get_key() == "pinned"


class TestBuiltCards:
    def test_a_stat_card_built_in_the_ui_computes_the_aggregate(self, request_from, boss, orders):
        request = request_from(boss)
        save(site, request, [stat_card()], user=boss)
        card = get_dashboard(site, request_from(boss)).render(request)[0]
        assert card.kind == "stat"
        assert card.stat["display"] == "40"

    def test_a_filter_narrows_the_card_and_is_described_on_it(self, request_from, boss, orders):
        request = request_from(boss)
        save(site, request, [stat_card(filter="status=paid")], user=boss)
        card = get_dashboard(site, request_from(boss)).render(request)[0]
        assert card.stat["display"] == "10"
        assert card.description == "Status: Paid"
        # And the card leads to the same rows on the list page.
        assert card.url.endswith("?status=paid")

    def test_a_chart_card_built_in_the_ui_renders(self, request_from, boss, orders):
        request = request_from(boss)
        save(site, request, [chart_card()], user=boss)
        card = get_dashboard(site, request_from(boss)).render(request)[0]
        assert card.kind == "chart"
        assert card.chart["empty"] is False

    def test_a_breakdown_card_groups_by_the_chosen_column(self, request_from, boss, orders):
        request = request_from(boss)
        save(
            site, request, [chart_card(mode="breakdown", target="status", kind="donut")], user=boss
        )
        card = get_dashboard(site, request_from(boss)).render(request)[0]
        assert sorted(card.chart["labels"]) == ["Paid", "Pending"]

    def test_every_aggregate_the_picker_offers_actually_computes(self, request_from, boss, orders):
        request = request_from(boss)
        displays = {}
        for aggregate in ("count", "sum", "avg", "min", "max"):
            field = "" if aggregate == "count" else "total"
            save(site, request_from(boss), [stat_card(aggregate=aggregate, field=field)], user=boss)
            card = get_dashboard(site, request_from(boss)).render(request)[0]
            displays[aggregate] = card.stat["display"]
        assert displays == {"count": "2", "sum": "40", "avg": "20", "min": "10", "max": "30"}


class TestArrangedPermissions:
    def test_a_card_over_a_forbidden_model_neither_renders_nor_queries(self, db, request_from):
        """The gate is before the query, exactly as it is for a declared card.

        A card that computes a number and then declines to show it has already paid for
        it and already put it in the query log. The render is not free — Django's own
        permission check has to read the permission tables the first time it is asked —
        but that cost is two fixed queries, not one that grows if the arrangement grows,
        and neither of them is the card's own aggregate: nothing here may touch the
        orders table.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        user = User.objects.create_user("plain", password="x", is_staff=True)
        save(site, request_from(user), [stat_card()], user=user)
        user = User.objects.get(pk=user.pk)  # drop the permission cache

        request = request_from(user)
        dashboard = get_dashboard(site, request)
        with CaptureQueriesContext(connection) as queries:
            assert dashboard.render(request) == []

        # Permission lookups, not the card: the aggregate never ran.
        assert not any("testapp_order" in q["sql"] for q in queries.captured_queries)
        assert len(queries) <= 2, [q["sql"] for q in queries.captured_queries]

    def test_the_same_card_renders_once_the_permission_is_granted(self, db, request_from, orders):
        user = User.objects.create_user("plain", password="x", is_staff=True)
        save(site, request_from(user), [stat_card()], user=user)
        user.user_permissions.add(Permission.objects.get(codename="view_order"))
        user = User.objects.get(pk=user.pk)

        request = request_from(user)
        assert [c.label for c in get_dashboard(site, request).render(request)] == ["Takings"]

    @declared_settings
    def test_a_narrower_colleague_rearranging_cannot_delete_what_they_never_saw(
        self, db, request_from, boss, orders
    ):
        """The slots a viewer may not see are carried through the save as bare indexes.

        Without that, somebody with narrower access dragging two cards would store an
        arrangement missing every card they were never shown.
        """
        user = User.objects.create_user("plain", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_order"))
        user = User.objects.get(pk=user.pk)

        save(
            site, request_from(boss), [stat_card(), {"card": "declared", "key": "posts"}], user=None
        )

        request = request_from(user)
        arrangement = get_dashboard(site, request).arrange(request)
        assert [c.label for c in arrangement.cards] == ["Takings"]
        # The Posts card is not drawn, and not described — only its slot number travels.
        assert [item["index"] for item in arrangement.unseen] == [1]

    def test_saving_for_everyone_is_a_separate_permission(self, db, rf, boss):
        request = rf.post("/")
        request.user = User.objects.create_user("plain", password="x", is_staff=True)
        with pytest.raises(PermissionDenied):
            dashboards.ArrangeDashboardView.scope_user(request, "site")

        request.user = boss
        assert dashboards.ArrangeDashboardView.scope_user(request, "site") is None

    def test_saving_for_yourself_needs_nothing_extra(self, db, rf):
        request = rf.post("/")
        request.user = User.objects.create_user("plain", password="x", is_staff=True)
        assert dashboards.ArrangeDashboardView.scope_user(request, "user") is request.user

    def test_the_picker_only_offers_models_the_person_may_open(self, db, request_from):
        user = User.objects.create_user("plain", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_post"))
        user = User.objects.get(pk=user.pk)

        context = dashboards.editor_context(site, request_from(user), None, stored=False)
        assert [model["value"] for model in context["models"]] == ["testapp.post"]


class TestQueryCost:
    """One aggregate per card. Twelve cards is twelve queries, not twelve times N."""

    def test_a_dashboard_of_built_cards_does_not_scale_with_the_rows(self, db, request_from, boss):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        author = Author.objects.create(name="Ada", email="ada@example.com")

        def make(count):
            for i in range(count):
                Order.objects.create(
                    number=f"N-{Order.objects.count()}-{i}", customer=author, total="5.00"
                )

        cards = [
            stat_card(
                aggregate=aggregate,
                field="" if aggregate == "count" else "total",
                label=f"{aggregate} {n}",
            )
            for n in range(3)
            for aggregate in ("count", "sum", "avg", "min")
        ]
        assert len(cards) == 12
        save(site, request_from(boss), cards, user=boss)

        make(3)
        request = request_from(boss)
        with CaptureQueriesContext(connection) as few:
            assert len(get_dashboard(site, request).render(request)) == 12

        make(60)
        request = request_from(boss)
        with CaptureQueriesContext(connection) as many:
            assert len(get_dashboard(site, request).render(request)) == 12

        # Invariance plus a ceiling, rather than an exact number: exact counts flap on
        # anything that touches session or permission caching, and the property that
        # matters is that the cost is a function of the cards, not of the rows.
        assert len(many) == len(few)
        assert len(many) <= 14, [q["sql"] for q in many.captured_queries]

    def test_a_chart_card_costs_a_bounded_number_of_queries(self, request_from, boss, orders):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        request = request_from(boss)
        save(site, request, [chart_card()], user=boss)
        fresh = request_from(boss)
        with CaptureQueriesContext(connection) as queries:
            get_dashboard(site, fresh).render(fresh)
        # The layout row, the span probe that chooses the bucket, and the grouped query.
        assert len(queries) <= 3

    def test_a_card_that_is_switched_off_costs_nothing(
        self, request_from, boss, orders, django_assert_num_queries
    ):
        request = request_from(boss)
        save(site, request, [stat_card(show=False)], user=boss)
        fresh = request_from(boss)
        with django_assert_num_queries(1):  # the layout row, and nothing else
            assert get_dashboard(site, fresh).render(fresh) == []


class TestTheForm:
    """The POST the editor sends, treated as hostile from end to end."""

    @declared_settings
    def test_adding_a_card_through_the_form(self, client_in, boss, orders):
        response = client_in.post(
            reverse("vantage:dashboard-arrange"),
            {
                "do": "save:user",
                "keep": ["0", "1", "2"],
                "order__0": "1",
                "order__1": "2",
                "order__2": "3",
                "add_model": "testapp.order",
                "add_shape": "stat",
                "add_aggregate": "sum",
                "add_field": "testapp.order:total",
                "add_width": "2",
                "add_label": "Takings",
            },
        )
        assert response.status_code == 302

        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "Takings" in body
        # The declared cards are still there: adding one card is not a replacement.
        assert "Posts" in body and "Authors" in body

    @declared_settings
    def test_the_form_reorders_by_the_number_boxes(self, client_in, boss, content):
        client_in.post(
            reverse("vantage:dashboard-arrange"),
            {
                "do": "save:user",
                "keep": ["0", "1", "2"],
                "order__0": "3",
                "order__1": "1",
                "order__2": "2",
            },
        )
        request = _request_for(boss)
        labels = [c.label for c in get_dashboard(site, request).render(request)]
        assert labels == ["Authors", "Orders", "Posts"]

    @declared_settings
    def test_unticking_a_declared_card_switches_it_off_rather_than_deleting_it(
        self, client_in, boss, content
    ):
        client_in.post(
            reverse("vantage:dashboard-arrange"),
            {
                "do": "save:user",
                "keep": ["1", "2"],
            },
        )
        stored = dashboards.row_for(site, boss).cards
        posts = next(entry for entry in stored if entry.get("key") == "posts")
        assert posts["show"] is False

        request = _request_for(boss)
        assert "Posts" not in {c.label for c in get_dashboard(site, request).render(request)}

    @declared_settings
    def test_unticking_a_card_somebody_built_deletes_it(self, client_in, boss, orders):
        request = _request_for(boss)
        save(site, request, [stat_card()], user=boss)

        # Slot 0 is the built card; the declared three follow it.
        client_in.post(
            reverse("vantage:dashboard-arrange"),
            {
                "do": "save:user",
                "keep": ["1", "2", "3"],
            },
        )
        stored = dashboards.row_for(site, boss).cards
        assert all(entry["card"] == "declared" for entry in stored)

    @declared_settings
    def test_the_width_select_resizes_a_card(self, client_in, boss, content):
        client_in.post(
            reverse("vantage:dashboard-arrange"),
            {
                "do": "save:user",
                "keep": ["0", "1", "2"],
                "width__0": "4",
            },
        )
        request = _request_for(boss)
        card = next(c for c in get_dashboard(site, request).render(request) if c.label == "Posts")
        assert card.width == 4

    def test_a_field_belonging_to_another_model_produces_no_card(self, client_in, boss, orders):
        client_in.post(
            reverse("vantage:dashboard-arrange"),
            {
                "do": "save:user",
                "add_model": "testapp.order",
                "add_shape": "stat",
                "add_aggregate": "sum",
                # Picked from the Posts optgroup while Orders was the chosen model.
                "add_field": "testapp.post:view_count",
            },
        )
        stored = dashboards.row_for(site, boss).cards
        assert not [entry for entry in stored if entry["card"] != "declared"]

    def test_reset_through_the_form_removes_the_row(self, client_in, boss, orders):
        request = _request_for(boss)
        save(site, request, [stat_card()], user=boss)
        client_in.post(reverse("vantage:dashboard-arrange"), {"do": "reset:user"})
        assert dashboards.row_for(site, boss) is None

    def test_a_plain_person_cannot_save_for_everyone(self, client, wired, db, orders):
        user = User.objects.create_user("plain", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_order"))
        client.force_login(user)
        response = client.post(reverse("vantage:dashboard-arrange"), {"do": "save:site"})
        assert response.status_code == 403
        assert dashboards.row_for(site, None) is None

    def test_a_get_redirects_rather_than_erroring(self, client_in):
        response = client_in.get(reverse("vantage:dashboard-arrange"))
        assert response.status_code == 302
        assert response["Location"] == reverse("vantage:dashboard")

    def test_an_off_site_next_is_not_followed(self, client_in):
        response = client_in.post(
            reverse("vantage:dashboard-arrange"),
            {"do": "save:user", "next": "https://evil.example/"},
        )
        assert response["Location"] == reverse("vantage:dashboard")

    def test_the_editor_returns_you_to_where_you_were(self, client_in):
        response = client_in.post(
            reverse("vantage:dashboard-arrange"), {"do": "save:user", "next": "/admin/?arrange=1"}
        )
        assert response["Location"] == "/admin/?arrange=1"


class TestTheEditorPage:
    def test_the_dashboard_is_unchanged_until_you_ask_to_arrange_it(self, client_in, content):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "v-arrange-panel" not in body
        assert "arrange=1" in body  # but the way in is offered

    def test_the_editor_draws_a_control_for_every_card(self, client_in, content):
        body = client_in.get(reverse("vantage:dashboard") + "?arrange=1").content.decode()
        assert "v-arrange-panel" in body
        assert 'name="keep"' in body
        assert 'name="order__' in body
        assert 'name="width__' in body

    def test_reordering_works_without_javascript(self, client_in, content):
        """The number boxes are the accessible, scriptless truth; dragging rewrites
        them. A rearrangement you can only do with a mouse is one half the people
        using the panel cannot do at all."""
        body = client_in.get(reverse("vantage:dashboard") + "?arrange=1").content.decode()
        assert 'type="number"' in body
        assert "Position of" in body
        # And the enhancement layer is loaded, not required.
        assert "vantage-dashboard.js" in body

    def test_the_picker_offers_the_aggregates_and_the_shapes(self, client_in, content):
        body = client_in.get(reverse("vantage:dashboard") + "?arrange=1").content.decode()
        for name in ("count", "sum", "avg", "min", "max"):
            assert f'value="{name}"' in body
        assert 'name="add_shape"' in body
        assert 'value="testapp.order:total"' in body
        assert 'value="testapp.order:created_at"' in body

    def test_a_switched_off_card_can_be_brought_back_from_the_editor(
        self, client_in, boss, content
    ):
        request = _request_for(boss)
        save(site, request, [{"card": "declared", "key": "posts", "show": False}], user=boss)
        body = client_in.get(reverse("vantage:dashboard") + "?arrange=1").content.decode()
        assert "Switched off" in body

    def test_an_arranged_dashboard_says_so(self, client_in, boss, orders):
        save(site, _request_for(boss), [stat_card()], user=boss)
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "Arranged" in body

    def test_save_for_everyone_is_only_offered_to_those_who_may(self, client, wired, db):
        user = User.objects.create_user("plain", password="x", is_staff=True)
        client.force_login(user)
        body = client.get(reverse("vantage:dashboard") + "?arrange=1").content.decode()
        assert "Save for everyone" not in body
        assert 'value="save:user"' in body


def _request_for(user):
    """A bare request carrying a user, for the assertions that resolve outside a view."""
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = user
    return request


@pytest.mark.django_db
def test_a_table_card_leaves_out_a_column_hidden_from_the_viewer(client):
    """Field permissions reach the dashboard's recent-records tables too."""
    from decimal import Decimal

    from django.contrib.auth.models import Group, Permission, User
    from django.contrib.contenttypes.models import ContentType

    from tests.testapp.models import Author, Order
    from vantage.models import FieldPermission

    group = Group.objects.create(name="Support")
    group.permissions.add(
        Permission.objects.get(codename="view_order", content_type__app_label="testapp")
    )
    FieldPermission.objects.create(
        group=group,
        content_type=ContentType.objects.get_for_model(Order),
        field_name="total",
        level="hidden",
    )
    user = User.objects.create_user("agent", "a@example.com", "x", is_staff=True)
    user.groups.add(group)
    client.force_login(user)
    customer = Author.objects.create(name="Grace", email="g@example.com")
    Order.objects.create(
        number="A-1001", customer=customer, status="pending", total=Decimal("4321.77")
    )

    body = client.get(reverse("vantage:dashboard")).content.decode()

    assert "A-1001" in body  # the card is there
    assert "4321.77" not in body and "4,321.77" not in body
