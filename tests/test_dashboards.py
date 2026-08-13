"""The dashboard: widget cards declared in Python.

Two things carry most of the weight here. The first is that a project which declares
nothing still gets a dashboard worth opening — that is the package's whole thesis, so
the zero-config path is tested as hard as the configured one. The second is that a
widget is inert until rendered: it must not query at declaration time, must not query
for a user who may not see it, and must not take the page down when it fails.
"""

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Avg, Count, Sum
from django.test import override_settings
from django.urls import reverse

from tests.testapp.models import Author, Comment, Order, Post, Tag
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
            StatCard("Revenue", Order.objects.all(), aggregate=Sum("total"),
                     format="currency", tone="success"),
            request_from(boss),
        )
        assert card.stat["display"] == "$40.00"
        assert card.stat["tone"] == "success"

    def test_a_stat_card_costs_one_query(self, request_from, boss, content,
                                         django_assert_num_queries):
        with django_assert_num_queries(1):
            render_one(
                StatCard("Average", Order.objects.all(), aggregate=Avg("total"),
                         format="currency"),
                request_from(boss),
            )

    def test_a_stat_card_accepts_a_callable_source(self, request_from, boss, content):
        card = render_one(
            StatCard("Drafts", lambda request: Post.objects.filter(status="draft"),
                     model=Post),
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
                chart=Chart.breakdown("Posts", group_by="status", value=Count("pk"),
                                      kind="donut"),
            ),
            request_from(boss),
        )
        assert sorted(card.chart["labels"]) == ["Draft", "Published"]

    def test_a_table_card_lists_the_most_recent_records(self, request_from, boss, content):
        card = render_one(TableCard("Recent posts", Post.objects.all(), limit=2),
                          request_from(boss))
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
        card = render_one(StatCard("Nonsense", lambda request: "not a queryset"),
                          request_from(boss))
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
        first = render_one(TableCard("Recent", MODULE_LEVEL_POSTS, limit=10),
                           request_from(boss))
        assert len(first.rows) == 3

        Post.objects.create(title="Fourth", slug="fourth", body="b",
                            author=content[0].author)
        second = render_one(TableCard("Recent", MODULE_LEVEL_POSTS, limit=10),
                            request_from(boss))
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
