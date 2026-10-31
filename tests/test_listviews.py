"""Alternative list views: Kanban, calendar, gallery and map, and the switcher.

Three groups of promises, tested in that order.

**Inference is conservative.** A view is a button on every list page that has one, so
the cases that matter most are the refusals: no board over ``is_superuser`` or a
soft-delete flag, no board over forty choices, no calendar over ``updated_at``.

**Every view is the same rows.** A mode is handed the queryset the table would have
paged through — scoped, searched, filtered — and may only narrow it. The tests filter
and search on a board, a calendar and a map and assert the rows follow.

**Moving a card is an inline edit.** Same permission answers (403, 404), same
validation, same optimistic lock (409), same audit entry, and a read-only field or a
missing change permission means no moving at all — in the markup *and* at the endpoint.
"""

import datetime
import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from tests.testapp.models import (
    Author,
    Category,
    Comment,
    LedgerEntry,
    Order,
    Post,
    Promo,
    Ticket,
    Venue,
)
from vantage import listviews
from vantage.listviews import inference
from vantage.listviews.calendar import month_grid, parse_month
from vantage.listviews.map import fit_equirectangular, fit_mercator, osm_url
from vantage.models import ListPreference, VantageLogEntry
from vantage.panels import ModelPanel
from vantage.sites import site

POST_LIST = "vantage:testapp-post-list"
MOVE_URL = "vantage:testapp-post-kanban-move"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def viewer(db):
    """Staff who may look at posts and may not change one."""
    user = User.objects.create_user("looker", password="x", is_staff=True)
    user.user_permissions.add(
        Permission.objects.get(
            content_type=ContentType.objects.get_for_model(Post), codename="view_post"
        )
    )
    return User.objects.get(pk=user.pk)


@pytest.fixture
def client_in(client, boss):
    client.force_login(boss)
    return client


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada Lovelace", email="ada@example.com")


def make_post(author, title, status=Post.Status.DRAFT, **extra):
    from django.utils.text import slugify

    return Post.objects.create(
        title=title, slug=slugify(title), body="b", author=author, status=status, **extra
    )


@pytest.fixture
def posts(author):
    return [
        make_post(author, "Draft one"),
        make_post(author, "Draft two"),
        make_post(author, "Under review", Post.Status.REVIEW),
        make_post(author, "Out now", Post.Status.PUBLISHED),
    ]


def token_of(obj):
    from vantage import concurrency

    obj.refresh_from_db()
    return concurrency.token_for(obj)


def day_of(response, iso):
    weeks = response.context["calendar_weeks"]
    return next(day for week in weeks for day in week if day["iso"] == iso)


def move(client, pk, value, version=None, **headers):
    body = {"pk": str(pk), "value": value}
    if version is not None:
        body["version"] = version
    return client.post(
        reverse(MOVE_URL), data=json.dumps(body), content_type="application/json", **headers
    )


# ── inference ───────────────────────────────────────────────────────────────


class TestInference:
    def test_a_status_choices_field_is_the_board(self):
        assert inference.infer_kanban_field(Post) == "status"
        assert inference.infer_kanban_field(Order) == "status"
        assert inference.infer_kanban_field(Venue) == "stage"

    def test_a_boolean_board_needs_a_name_that_reads_as_a_stage(self):
        assert inference.infer_kanban_field(Promo) == "is_published"
        assert inference.infer_kanban_field(Comment) == "approved"

    def test_a_soft_delete_flag_is_never_a_board(self):
        assert inference.infer_kanban_field(Ticket) is None

    def test_a_permission_flag_is_never_a_board(self):
        # is_active is a fair board for users; is_superuser and is_staff never are.
        assert inference.infer_kanban_field(User) == "is_active"

    def test_too_many_choices_is_a_filter_not_a_board(self):
        from django.db import models

        many = models.CharField(choices=[(str(i), str(i)) for i in range(20)], max_length=4)
        many.name = "status"
        assert not inference.kanban_field_ok(many)

    def test_a_scheduling_date_beats_the_audit_stamp(self):
        assert inference.infer_calendar_field(Venue) == "starts_on"
        assert inference.infer_calendar_field(Post) == "published_at"
        # settled_on matches no preferred name but is still not an insertion stamp.
        assert inference.infer_calendar_field(LedgerEntry) == "settled_on"

    def test_updated_at_is_never_a_calendar(self):
        # Post has updated_at (auto_now) and must not fall back to it.
        assert inference.infer_calendar_field(Post) != "updated_at"
        assert inference.infer_calendar_field(Category) is None

    def test_an_image_field_is_the_gallery(self):
        assert inference.infer_gallery_field(Author) == "avatar"

    def test_a_file_field_named_like_a_picture_is_the_gallery(self):
        assert inference.infer_gallery_field(Venue) == "photo"

    def test_a_latitude_longitude_pair_is_the_map(self):
        assert inference.infer_map_fields(Venue) == ("latitude", "longitude")
        assert inference.infer_map_fields(Post) is None

    def test_the_table_always_comes_first(self):
        assert inference.infer_list_views(Venue) == [
            "table",
            "kanban",
            "calendar",
            "gallery",
            "map",
        ]
        assert inference.infer_list_views(Category) == ["table"]

    def test_card_fields_skip_the_title_the_view_field_and_relations_to_many(self):
        cards = inference.infer_card_fields(Post, exclude=("status",))
        assert "title" not in cards
        assert "status" not in cards
        assert "tags" not in cards
        assert 1 <= len(cards) <= inference.MAX_CARD_FIELDS


class TestPanelResolution:
    def test_declaring_only_the_table_switches_the_others_off(self, monkeypatch):
        panel = site.get_panel(Post)
        monkeypatch.setattr(panel, "list_views", ["table"])
        assert panel.get_list_views() == ["table"]

    def test_a_declared_view_the_model_cannot_draw_is_dropped(self, monkeypatch):
        panel = site.get_panel(Post)
        monkeypatch.setattr(panel, "list_views", ["kanban", "map", "table"])
        assert panel.get_list_views() == ["kanban", "table"]

    def test_one_line_overrides_one_field_and_nothing_else(self, monkeypatch):
        panel = site.get_panel(Post)
        monkeypatch.setattr(panel, "calendar_field", "created_at")
        assert panel.get_calendar_field() == "created_at"
        assert panel.get_kanban_field() == "status"

    def test_map_fields_accept_a_list_as_well_as_a_tuple(self, monkeypatch):
        panel = site.get_panel(Venue)
        monkeypatch.setattr(panel, "map_fields", ["latitude", "longitude"])
        assert panel.get_map_fields() == ("latitude", "longitude")


# ── the switcher and the URL ────────────────────────────────────────────────


class TestSwitcher:
    def test_only_supported_views_are_offered(self, client_in, posts):
        html = client_in.get(reverse(POST_LIST)).content.decode()
        assert 'class="v-viewswitch"' in html
        assert "view=kanban" in html
        assert "view=calendar" in html
        assert "view=map" not in html
        assert "view=gallery" not in html

    def test_a_model_with_only_the_table_has_no_switcher(self, client_in):
        html = client_in.get(reverse("vantage:testapp-category-list")).content.decode()
        assert "v-viewswitch" not in html

    def test_the_current_view_is_marked(self, client_in, posts):
        html = client_in.get(reverse(POST_LIST) + "?view=kanban").content.decode()
        assert 'href="?view=kanban" aria-current="page"' in " ".join(html.split())

    def test_switching_keeps_the_filters_and_drops_the_page(self, rf, boss):
        request = rf.get("/", {"status": "draft", "page": "3", "month": "2026-01"})
        request.user = boss
        url = listviews.mode_url(request, "kanban")
        assert "status=draft" in url and "view=kanban" in url
        assert "page=" not in url and "month=" not in url

    def test_switching_keeps_the_saved_view_beside_the_mode(self, rf, boss):
        request = rf.get("/?view=42&view=table")
        request.user = boss
        url = listviews.mode_url(request, "calendar")
        assert "view=42" in url and "view=calendar" in url and "view=table" not in url

    def test_an_unknown_view_falls_back_to_the_default(self, client_in, posts):
        response = client_in.get(reverse(POST_LIST) + "?view=spreadsheet")
        assert response.status_code == 200
        assert response.context["list_mode"].name == "table"

    def test_a_digit_view_is_a_saved_view_not_a_mode(self, rf):
        assert listviews.requested(rf.get("/?view=42")) is None
        assert listviews.requested(rf.get("/?view=42&view=map")) == "map"


class TestRemembering:
    def test_the_choice_is_remembered_per_user_per_model(self, client_in, boss, posts):
        client_in.get(reverse(POST_LIST) + "?view=kanban")
        row = ListPreference.objects.get(user=boss, resource_key="testapp.post")
        assert row.list_view == "kanban"
        response = client_in.get(reverse(POST_LIST))
        assert response.context["list_mode"].name == "kanban"

    def test_another_user_still_sees_the_default(self, client, client_in, posts, db):
        client_in.get(reverse(POST_LIST) + "?view=kanban")
        other = User.objects.create_superuser("other", "o@example.com", "x")
        client.force_login(other)
        assert client.get(reverse(POST_LIST)).context["list_mode"].name == "table"

    def test_another_model_is_not_affected(self, client_in, boss, posts):
        client_in.get(reverse(POST_LIST) + "?view=kanban")
        response = client_in.get(reverse("vantage:testapp-order-list"))
        assert response.context["list_mode"].name == "table"

    def test_picking_the_default_with_nothing_stored_writes_nothing(self, client_in, boss, posts):
        client_in.get(reverse(POST_LIST) + "?view=table")
        assert not ListPreference.objects.filter(user=boss).exists()

    def test_browsing_without_a_choice_never_writes(self, client_in, boss, posts):
        client_in.get(reverse(POST_LIST) + "?view=kanban")
        with CaptureQueriesContext(connection) as queries:
            client_in.get(reverse(POST_LIST))
        assert not [
            q for q in queries if q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT"))
        ]

    def test_resetting_a_rail_section_keeps_the_remembered_view(self, client_in, boss, posts):
        client_in.get(reverse(POST_LIST) + "?view=calendar")
        row = ListPreference.objects.get(user=boss)
        assert not row.is_empty()

    def test_a_remembered_view_the_panel_no_longer_offers_is_ignored(
        self, client_in, boss, posts, monkeypatch
    ):
        client_in.get(reverse(POST_LIST) + "?view=kanban")
        monkeypatch.setattr(site.get_panel(Post), "list_views", ["table", "calendar"])
        assert client_in.get(reverse(POST_LIST)).context["list_mode"].name == "table"


class TestSavedViews:
    def test_the_list_view_is_captured(self):
        from django.http import QueryDict

        from vantage import savedviews

        panel = site.get_panel(Post)
        state = savedviews.capture(panel, QueryDict("status=draft&view=kanban&view=12"))
        assert state["list_view"] == "kanban"

    def test_an_unoffered_list_view_is_not_captured(self):
        from django.http import QueryDict

        from vantage import savedviews

        state = savedviews.capture(site.get_panel(Post), QueryDict("view=map"))
        assert "list_view" not in state

    def test_hydrating_puts_the_mode_back_in_the_url(self):
        from vantage import savedviews

        hydrated = savedviews.hydrate(site.get_panel(Post), {"list_view": "calendar"})
        assert "view=calendar" in hydrated.query
        assert not hydrated.broken

    def test_a_mode_since_withdrawn_is_reported_but_does_not_widen(self, monkeypatch):
        from vantage import savedviews

        panel = site.get_panel(Post)
        monkeypatch.setattr(panel, "list_views", ["table"])
        hydrated = savedviews.hydrate(panel, {"list_view": "kanban"})
        assert hydrated.broken and not hydrated.widened

    def test_applying_a_saved_view_opens_it_in_its_mode(self, client_in, boss, posts):
        from vantage.models import SavedView

        view = SavedView.objects.create(
            owner=boss,
            model_label="testapp.post",
            name="Board",
            state={"version": 1, "filters": {}, "q": "", "sort": "", "list_view": "kanban"},
        )
        response = client_in.get(reverse("vantage:saved-view-apply", args=[view.pk]))
        assert response.status_code == 302
        landed = client_in.get(response["Location"])
        assert landed.context["list_mode"].name == "kanban"
        assert landed.context["saved_view"] == view

    def test_saving_from_the_board_lands_back_on_the_board(self):
        from vantage.savedviews import _with_view, _without_view

        assert _with_view("/x/?view=kanban&view=3", 9) == "/x/?view=9&view=kanban"
        assert _without_view("/x/?view=9&view=kanban") == "/x/?view=kanban"


# ── kanban ──────────────────────────────────────────────────────────────────


class TestKanbanPage:
    def test_one_column_per_choice_with_its_count(self, client_in, posts):
        response = client_in.get(reverse(POST_LIST) + "?view=kanban")
        columns = {c["value"]: c for c in response.context["kanban_columns"]}
        assert list(columns) == ["draft", "review", "published"]
        assert columns["draft"]["count"] == 2
        assert [c["title"] for c in columns["draft"]["cards"]] == ["Draft two", "Draft one"]
        html = response.content.decode()
        assert "Draft one" in html and "data-kanban-col" in html

    def test_the_board_honours_search_and_filters(self, client_in, posts):
        response = client_in.get(reverse(POST_LIST) + "?view=kanban&q=two")
        counts = {c["value"]: c["count"] for c in response.context["kanban_columns"]}
        assert counts == {"draft": 1, "review": 0, "published": 0}

    def test_a_column_is_capped_and_offers_more(self, client_in, author, monkeypatch):
        from vantage.listviews.kanban import KanbanMode

        monkeypatch.setattr(KanbanMode, "column_limit", 2)
        for i in range(5):
            make_post(author, f"Draft {i}")
        response = client_in.get(reverse(POST_LIST) + "?view=kanban")
        draft = response.context["kanban_columns"][0]
        assert draft["count"] == 5 and len(draft["cards"]) == 2
        assert "more=draft" in draft["more_url"]
        more = client_in.get(reverse(POST_LIST) + draft["more_url"])
        assert len(more.context["kanban_columns"][0]["cards"]) == 4

    def test_a_retired_value_still_gets_a_column(self, client_in, posts):
        Post.objects.filter(pk=posts[0].pk).update(status="archived")
        response = client_in.get(reverse(POST_LIST) + "?view=kanban")
        assert "archived" in [c["value"] for c in response.context["kanban_columns"]]

    def test_a_boolean_board_has_yes_and_no(self, client_in, db):
        Promo.objects.create(headline="On", is_published=True)
        Promo.objects.create(headline="Off", is_published=False)
        response = client_in.get(reverse("vantage:testapp-promo-list") + "?view=kanban")
        columns = response.context["kanban_columns"]
        assert [c["value"] for c in columns] == [True, False]
        assert [c["key"] for c in columns] == ["true", "false"]

    def test_the_page_cost_does_not_grow_with_the_cards(self, client_in, author, posts):
        url = reverse(POST_LIST) + "?view=kanban"
        client_in.get(url)  # store the preference once, so both runs read it
        with CaptureQueriesContext(connection) as small:
            client_in.get(url)
        for i in range(60):
            make_post(author, f"Bulk {i}", Post.Status.REVIEW)
        with CaptureQueriesContext(connection) as large:
            client_in.get(url)
        assert len(large) == len(small)

    def test_move_controls_are_drawn_for_someone_who_may_change(self, client_in, posts):
        html = client_in.get(reverse(POST_LIST) + "?view=kanban").content.decode()
        assert 'draggable="true"' in html
        assert "data-kanban-move-url" in html
        assert "Move to…" in html

    def test_no_move_controls_without_change_permission(self, client, viewer, posts):
        client.force_login(viewer)
        response = client.get(reverse(POST_LIST) + "?view=kanban")
        html = response.content.decode()
        assert response.context["kanban_can_move"] is False
        assert 'draggable="true"' not in html and "Move to…" not in html

    def test_no_move_controls_on_a_readonly_field(self, client_in, posts, monkeypatch):
        monkeypatch.setattr(site.get_panel(Post), "readonly_fields", ["status"])
        response = client_in.get(reverse(POST_LIST) + "?view=kanban")
        assert response.context["kanban_can_move"] is False

    def test_no_move_controls_in_read_only_mode(self, client_in, posts):
        with override_settings(VANTAGE={"READ_ONLY": True}):
            response = client_in.get(reverse(POST_LIST) + "?view=kanban")
        assert response.context["kanban_can_move"] is False


class TestKanbanMove:
    def test_a_move_writes_the_field_and_answers_the_new_version(self, client_in, posts):
        post = posts[0]
        response = move(client_in, post.pk, "published", token_of(post))
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["ok"] and body["value"] == "published"
        post.refresh_from_db()
        assert post.status == "published"
        assert body["version"] == token_of(post)

    def test_a_move_is_audited(self, client_in, posts):
        post = posts[0]
        move(client_in, post.pk, "review", token_of(post))
        entry = VantageLogEntry.objects.filter(object_id=str(post.pk)).latest("pk")
        assert "status" in json.dumps(entry.changes)

    def test_an_invalid_value_is_a_validation_error(self, client_in, posts):
        post = posts[0]
        response = move(client_in, post.pk, "nonsense", token_of(post))
        assert response.status_code == 422
        assert response.json()["errors"][0]["fields"]["status"]
        post.refresh_from_db()
        assert post.status == "draft"

    def test_a_stale_version_is_a_conflict(self, client_in, posts):
        post = posts[0]
        stale = token_of(post)
        Post.objects.filter(pk=post.pk).update(updated_at=timezone.now() + datetime.timedelta(1))
        response = move(client_in, post.pk, "published", stale)
        assert response.status_code == 409
        post.refresh_from_db()
        assert post.status == "draft"

    def test_a_viewer_cannot_move(self, client, viewer, posts):
        client.force_login(viewer)
        assert move(client, posts[0].pk, "published", token_of(posts[0])).status_code == 403
        posts[0].refresh_from_db()
        assert posts[0].status == "draft"

    def test_a_card_outside_your_rows_is_not_found(self, client_in, posts, monkeypatch):
        panel = site.get_panel(Post)
        monkeypatch.setattr(
            panel, "get_queryset", lambda request: Post.objects.exclude(pk=posts[0].pk)
        )
        assert move(client_in, posts[0].pk, "published", token_of(posts[0])).status_code == 404

    def test_an_object_level_refusal_is_forbidden(self, client_in, posts, monkeypatch):
        panel = site.get_panel(Post)
        monkeypatch.setattr(
            panel,
            "has_change_permission",
            lambda request, obj=None: obj is None or obj.pk != posts[0].pk,
            raising=False,
        )
        assert move(client_in, posts[0].pk, "published", token_of(posts[0])).status_code == 403

    def test_a_readonly_field_cannot_be_moved_by_hand(self, client_in, posts, monkeypatch):
        monkeypatch.setattr(site.get_panel(Post), "readonly_fields", ["status"])
        response = move(client_in, posts[0].pk, "published", token_of(posts[0]))
        assert response.status_code == 400
        posts[0].refresh_from_db()
        assert posts[0].status == "draft"

    def test_a_panel_without_a_board_refuses_moves(self, client_in, posts, monkeypatch):
        monkeypatch.setattr(site.get_panel(Post), "list_views", ["table"])
        assert move(client_in, posts[0].pk, "published", token_of(posts[0])).status_code == 400

    def test_read_only_mode_refuses(self, client_in, posts):
        with override_settings(VANTAGE={"READ_ONLY": True}):
            assert move(client_in, posts[0].pk, "published", token_of(posts[0])).status_code == 403

    def test_a_model_clean_runs(self, client_in, posts, monkeypatch):
        from django.core.exceptions import ValidationError

        def clean(self):
            if self.status == "published" and not self.summary:
                raise ValidationError("A published post needs a summary.")

        monkeypatch.setattr(Post, "clean", clean)
        response = move(client_in, posts[0].pk, "published", token_of(posts[0]))
        assert response.status_code == 422
        assert "summary" in json.dumps(response.json())

    def test_the_menu_works_without_javascript(self, client_in, posts):
        post = posts[0]
        back = reverse(POST_LIST) + "?view=kanban"
        response = client_in.post(
            reverse(MOVE_URL),
            {"pk": post.pk, "value": json.dumps("review"), "version": token_of(post), "next": back},
        )
        assert response.status_code == 302 and response["Location"] == back
        post.refresh_from_db()
        assert post.status == "review"

    def test_the_form_fallback_refuses_an_offsite_redirect(self, client_in, posts):
        post = posts[0]
        response = client_in.post(
            reverse(MOVE_URL),
            {
                "pk": post.pk,
                "value": json.dumps("review"),
                "version": token_of(post),
                "next": "https://evil.example/",
            },
        )
        assert response.status_code == 302
        assert "evil.example" not in response["Location"]

    def test_a_malformed_body_is_a_bad_request(self, client_in, posts):
        response = client_in.post(reverse(MOVE_URL), data="{", content_type="application/json")
        assert response.status_code == 400
        response = client_in.post(
            reverse(MOVE_URL), data=json.dumps({"value": "x"}), content_type="application/json"
        )
        assert response.status_code == 400

    def test_get_is_not_allowed(self, client_in):
        assert client_in.get(reverse(MOVE_URL)).status_code == 405


# ── calendar ────────────────────────────────────────────────────────────────


class TestCalendar:
    def test_parse_month(self):
        today = datetime.date(2026, 10, 26)
        assert parse_month("2026-02", today) == datetime.date(2026, 2, 1)
        assert parse_month("garbage", today) == datetime.date(2026, 10, 1)
        assert parse_month("2026-13", today) == datetime.date(2026, 10, 1)

    def test_the_grid_covers_whole_weeks_from_the_locale_first_day(self):
        # October 2026 starts on a Thursday.
        start, end = month_grid(datetime.date(2026, 10, 1), first_weekday=1)  # Monday
        assert start == datetime.date(2026, 9, 28) and start.weekday() == 0
        assert end == datetime.date(2026, 11, 1) and end.weekday() == 6
        start, _end = month_grid(datetime.date(2026, 10, 1), first_weekday=0)  # Sunday
        assert start == datetime.date(2026, 9, 27) and start.weekday() == 6

    def test_events_land_on_their_day_and_link_to_the_record(self, client_in, author):
        moment = timezone.make_aware(datetime.datetime(2026, 3, 14, 10, 0))
        post = make_post(author, "Pi day", published_at=moment)
        response = client_in.get(reverse(POST_LIST) + "?view=calendar&month=2026-03")
        day = day_of(response, "2026-03-14")
        assert [e["title"] for e in day["events"]] == ["Pi day"]
        assert reverse("vantage:testapp-post-detail", args=[post.pk]) in response.content.decode()

    def test_the_query_is_bounded_to_the_visible_range(self, client_in, author):
        inside = timezone.make_aware(datetime.datetime(2026, 3, 14, 10, 0))
        outside = timezone.make_aware(datetime.datetime(2026, 7, 1, 10, 0))
        make_post(author, "Inside", published_at=inside)
        make_post(author, "Outside", published_at=outside)
        with CaptureQueriesContext(connection) as queries:
            response = client_in.get(reverse(POST_LIST) + "?view=calendar&month=2026-03")
        assert response.context["calendar_count"] == 1
        # Quoting is the backend's: "x" on SQLite and PostgreSQL, `x` on MySQL.
        assert any('"published_at" >=' in q["sql"].replace("`", '"') for q in queries)

    @override_settings(TIME_ZONE="Asia/Tokyo")
    def test_days_are_the_viewers_days(self, client_in, author):
        # 20:00 UTC on 31 March is 05:00 on 1 April in Tokyo.
        moment = datetime.datetime(2026, 3, 31, 20, 0, tzinfo=datetime.UTC)
        make_post(author, "Late", published_at=moment)
        response = client_in.get(reverse(POST_LIST) + "?view=calendar&month=2026-04")
        day = day_of(response, "2026-04-01")
        assert [e["title"] for e in day["events"]] == ["Late"]

    def test_navigation_links(self, client_in, posts):
        response = client_in.get(reverse(POST_LIST) + "?view=calendar&month=2026-01")
        assert "month=2025-12" in response.context["calendar_prev_url"]
        assert "month=2026-02" in response.context["calendar_next_url"]
        assert "month=" not in response.context["calendar_today_url"]

    def test_the_calendar_honours_filters(self, client_in, author):
        moment = timezone.make_aware(datetime.datetime(2026, 3, 14, 10, 0))
        make_post(author, "Drafted", published_at=moment)
        make_post(author, "Shipped", Post.Status.PUBLISHED, published_at=moment)
        response = client_in.get(
            reverse(POST_LIST) + "?view=calendar&month=2026-03&status=published"
        )
        assert response.context["calendar_count"] == 1

    def test_a_busy_day_folds_into_more(self, client_in, author):
        moment = timezone.make_aware(datetime.datetime(2026, 3, 14, 10, 0))
        for i in range(5):
            make_post(author, f"Busy {i}", published_at=moment)
        response = client_in.get(reverse(POST_LIST) + "?view=calendar&month=2026-03")
        day = day_of(response, "2026-03-14")
        assert len(day["events"]) == 3 and len(day["overflow"]) == 2
        assert "2 more" in response.content.decode()

    def test_a_date_field_calendar(self, client_in, db):
        Venue.objects.create(name="Hall", starts_on=datetime.date(2026, 5, 2))
        response = client_in.get(
            reverse("vantage:testapp-venue-list") + "?view=calendar&month=2026-05"
        )
        assert response.context["calendar_count"] == 1


# ── gallery ─────────────────────────────────────────────────────────────────


class TestGallery:
    def test_cards_with_a_picture_or_a_placeholder(self, client_in, db):
        Author.objects.create(name="No face", email="n@example.com")
        response = client_in.get(reverse("vantage:testapp-author-list") + "?view=gallery")
        assert response.status_code == 200
        cards = response.context["gallery_cards"]
        assert [c["title"] for c in cards] == ["No face"]
        assert 'class="v-gallery"' in response.content.decode()

    def test_the_gallery_is_paginated_like_the_table(self, client_in, db, monkeypatch):
        monkeypatch.setattr(site.get_panel(Author), "list_per_page", 2)
        for i in range(5):
            Author.objects.create(name=f"A{i}", email=f"a{i}@example.com")
        response = client_in.get(reverse("vantage:testapp-author-list") + "?view=gallery&page=2")
        assert len(response.context["gallery_cards"]) == 2
        assert response.context["page"].number == 2
        assert "v-pagination" in response.content.decode()

    def test_the_page_cost_does_not_grow_with_the_cards(self, client_in, db):
        url = reverse("vantage:testapp-author-list") + "?view=gallery"
        Author.objects.create(name="First", email="f@example.com")
        client_in.get(url)
        with CaptureQueriesContext(connection) as small:
            client_in.get(url)
        for i in range(30):
            Author.objects.create(name=f"B{i}", email=f"b{i}@example.com")
        with CaptureQueriesContext(connection) as large:
            client_in.get(url)
        assert len(large) == len(small)


# ── map ─────────────────────────────────────────────────────────────────────


class TestMap:
    def test_points_link_to_the_record_and_to_openstreetmap(self, client_in, db):
        venue = Venue.objects.create(
            name="Lyon hall", latitude=Decimal("45.764"), longitude=Decimal("4.8357")
        )
        Venue.objects.create(name="Nowhere")
        response = client_in.get(reverse("vantage:testapp-venue-list") + "?view=map")
        html = response.content.decode()
        points = response.context["map_points"]
        assert [p["title"] for p in points] == ["Lyon hall"]
        assert reverse("vantage:testapp-venue-detail", args=[venue.pk]) in html
        assert "openstreetmap.org/?mlat=45.764000&amp;mlon=4.835700" in html
        assert "<circle" in html

    def test_nothing_is_fetched_from_anywhere_by_default(self, client_in, db):
        Venue.objects.create(name="A", latitude=Decimal("51.5"), longitude=Decimal("-0.12"))
        html = client_in.get(reverse("vantage:testapp-venue-list") + "?view=map").content.decode()
        assert "<image" not in html
        assert '<script src="http' not in html

    def test_impossible_coordinates_are_excluded_in_sql(self, client_in, db):
        Venue.objects.create(name="Bad", latitude=Decimal("123"), longitude=Decimal("0"))
        response = client_in.get(reverse("vantage:testapp-venue-list") + "?view=map")
        assert response.context["map_points"] == []

    def test_tiles_are_an_opt_in(self, client_in, db):
        Venue.objects.create(name="A", latitude=Decimal("51.5"), longitude=Decimal("-0.12"))
        Venue.objects.create(name="B", latitude=Decimal("51.6"), longitude=Decimal("-0.2"))
        with override_settings(VANTAGE={"MAP_TILE_URL": "https://tiles.example/{z}/{x}/{y}.png"}):
            html = client_in.get(
                reverse("vantage:testapp-venue-list") + "?view=map"
            ).content.decode()
        assert '<image href="https://tiles.example/' in html

    def test_the_map_honours_search(self, client_in, db):
        Venue.objects.create(name="Alpha", latitude=Decimal("1"), longitude=Decimal("1"))
        Venue.objects.create(name="Beta", latitude=Decimal("2"), longitude=Decimal("2"))
        response = client_in.get(reverse("vantage:testapp-venue-list") + "?view=map&q=Beta")
        assert [p["title"] for p in response.context["map_points"]] == ["Beta"]

    def test_equirectangular_fit_keeps_every_point_inside_the_plot(self):
        project, _frame = fit_equirectangular([(51.5, -0.12), (45.76, 4.83), (40.4, -3.7)])
        for lat, lng in [(51.5, -0.12), (45.76, 4.83), (40.4, -3.7)]:
            x, y = project(lat, lng)
            assert 0 <= x <= 960 and 0 <= y <= 540
        # North is up.
        assert project(51.5, 0)[1] < project(40.4, 0)[1]

    def test_a_single_point_still_has_a_frame(self):
        project, _frame = fit_equirectangular([(10.0, 10.0)])
        assert project(10.0, 10.0) == (480.0, 270.0)

    def test_mercator_fit_returns_tiles_around_the_points(self):
        project, tiles = fit_mercator([(51.5, -0.12), (51.6, -0.2)], "t/{z}/{x}/{y}")
        assert tiles and all(t["url"].startswith("t/") for t in tiles)
        x, y = project(51.5, -0.12)
        assert 0 <= x <= 960 and 0 <= y <= 540

    def test_osm_url(self):
        assert osm_url(1.5, -2.25).startswith(
            "https://www.openstreetmap.org/?mlat=1.500000&mlon=-2.250000"
        )

    def test_the_page_cost_does_not_grow_with_the_points(self, client_in, db):
        url = reverse("vantage:testapp-venue-list") + "?view=map"
        Venue.objects.create(name="First", latitude=Decimal("1"), longitude=Decimal("1"))
        client_in.get(url)
        with CaptureQueriesContext(connection) as small:
            client_in.get(url)
        Venue.objects.bulk_create(
            Venue(name=f"V{i}", latitude=Decimal(i % 80), longitude=Decimal(i)) for i in range(40)
        )
        with CaptureQueriesContext(connection) as large:
            client_in.get(url)
        assert len(large) == len(small)


# ── the rest of the page still applies ──────────────────────────────────────


class TestTheListPageAroundTheView:
    def test_the_stats_strip_still_renders_on_a_board(self, client_in, posts, monkeypatch):
        from django.db.models import Count

        from vantage import Stat

        monkeypatch.setattr(site.get_panel(Post), "list_stats", [Stat("Posts", Count("id"))])
        response = client_in.get(reverse(POST_LIST) + "?view=kanban")
        assert response.context["analytics"]["stats"]

    def test_the_search_form_carries_the_view(self, client_in, posts):
        html = client_in.get(reverse(POST_LIST) + "?view=kanban").content.decode()
        assert '<input type="hidden" name="view" value="kanban">' in html

    def test_a_permissionless_user_cannot_open_any_view(self, client, db):
        nobody = User.objects.create_user("nobody", password="x", is_staff=True)
        client.force_login(nobody)
        assert client.get(reverse(POST_LIST) + "?view=kanban").status_code == 403

    def test_the_table_still_renders_its_grid(self, client_in, posts):
        html = client_in.get(reverse(POST_LIST) + "?view=table").content.decode()
        assert 'class="v-table"' in html


# ── checks, scaffold and settings ───────────────────────────────────────────


def _check(**attrs):
    class Configured(ModelPanel):
        pass

    for name, value in attrs.items():
        setattr(Configured, name, value)
    return [error.id for error in Configured(Venue).check()]


class TestChecks:
    def test_a_good_configuration_is_clean(self):
        assert (
            _check(
                list_views=["table", "kanban", "map"],
                kanban_field="stage",
                calendar_field="starts_on",
                gallery_field="photo",
                map_fields=("latitude", "longitude"),
                card_fields=["city"],
            )
            == []
        )

    def test_an_unknown_view(self):
        assert _check(list_views=["table", "timeline"]) == ["vantage.E070"]

    def test_a_string_instead_of_a_list(self):
        assert _check(list_views="kanban") == ["vantage.E070"]

    def test_a_view_without_its_field(self):
        class NoMap(ModelPanel):
            list_views = ["table", "map"]

        assert [e.id for e in NoMap(Post).check()] == ["vantage.E071"]

    def test_bad_fields(self):
        assert _check(kanban_field="name") == ["vantage.E072"]
        assert _check(calendar_field="name") == ["vantage.E073"]
        assert _check(gallery_field="name") == ["vantage.E074"]
        assert _check(map_fields=("name", "city")) == ["vantage.E075"]
        assert _check(map_fields="latitude") == ["vantage.E075"]
        assert _check(card_fields=["nope"]) == ["vantage.E076"]


class TestScaffold:
    def test_the_inferred_views_are_written_out(self):
        from vantage.scaffold import render_panel_class

        source = render_panel_class(Venue)
        assert 'list_views = ["table", "kanban", "calendar", "gallery", "map"]' in source
        assert 'kanban_field = "stage"' in source
        assert 'calendar_field = "starts_on"' in source
        assert 'gallery_field = "photo"' in source
        assert 'map_fields = ("latitude", "longitude")' in source
        assert "card_fields = " in source
        compile(source, "<scaffold>", "exec")

    def test_a_table_only_model_gets_no_list_views_line(self):
        from vantage.scaffold import render_panel_class

        assert "list_views" not in render_panel_class(Category)


class TestSettings:
    def test_a_tile_url_must_be_a_template(self):
        from django.core.exceptions import ImproperlyConfigured

        from vantage.conf import vantage_settings

        with override_settings(VANTAGE={"MAP_TILE_URL": "https://tiles.example/x.png"}):
            with pytest.raises(ImproperlyConfigured):
                vantage_settings.SITE_TITLE  # noqa: B018

    def test_the_default_is_no_tiles(self):
        from vantage.conf import vantage_settings

        assert vantage_settings.MAP_TILE_URL is None
