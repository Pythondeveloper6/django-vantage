"""Choosing the field a board or calendar is arranged by: "Group by" and "Dates from".

``Post`` has two board fields (``status``, then the stage-like boolean ``featured``)
and two dates (``published_at``, then the audit stamp ``created_at``), so every test
here has a default and one alternative to switch to.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from tests.testapp.models import Author, Post
from vantage import concurrency
from vantage.models import ListPreference
from vantage.panel_checks import ModelPanelChecks
from vantage.savedviews import capture, hydrate
from vantage.sites import site

POST_LIST = "vantage:testapp-post-list"
MOVE_URL = "vantage:testapp-post-kanban-move"


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def panel():
    panel = site.get_panel(Post)
    saved = dict(panel.__dict__)
    yield panel
    panel.__dict__.clear()
    panel.__dict__.update(saved)


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def client_in(client, boss):
    client.force_login(boss)
    return client


@pytest.fixture
def post(db):
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    return Post.objects.create(
        title="Out now",
        slug="out-now",
        body="b",
        author=author,
        status=Post.Status.DRAFT,
        featured=False,
        published_at=timezone.now(),
    )


def board(client, **params):
    response = client.get(reverse(POST_LIST), {"view": "kanban", **params})
    assert response.status_code == 200
    return response


def calendar(client, **params):
    response = client.get(reverse(POST_LIST), {"view": "calendar", **params})
    assert response.status_code == 200
    return response


class TestChoices:
    def test_the_default_comes_first_then_the_other_candidates(self, panel):
        assert panel.get_kanban_fields() == ["status", "featured"]
        assert panel.get_calendar_fields() == ["published_at", "created_at"]

    def test_a_declared_default_moves_to_the_front(self, panel):
        panel.kanban_field = "featured"
        assert panel.get_kanban_fields() == ["featured", "status"]

    def test_an_empty_list_leaves_only_the_default(self, panel):
        panel.kanban_fields = []
        assert panel.get_kanban_fields() == ["status"]

    def test_a_sensitive_field_is_never_offered(self, panel, monkeypatch):
        monkeypatch.setattr(
            type(panel), "get_sensitive_fields", lambda self: frozenset({"featured"})
        )
        assert panel.get_kanban_fields() == ["status"]


class TestBoard:
    def test_the_picker_offers_every_board_field(self, client_in, post):
        html = board(client_in).content.decode()
        assert 'name="by"' in html
        assert 'value="status" selected' in html
        assert 'value="featured"' in html

    def test_by_regroups_the_columns(self, client_in, post):
        response = board(client_in, by="featured")
        assert response.context["kanban_field"] == "featured"
        assert [c["value"] for c in response.context["kanban_columns"]] == [True, False]

    def test_an_unknown_field_falls_back_to_the_default(self, client_in, post):
        assert board(client_in, by="title").context["kanban_field"] == "status"

    def test_no_picker_when_there_is_nothing_to_choose(self, client_in, post, panel):
        panel.kanban_fields = []
        assert 'name="by"' not in board(client_in).content.decode()

    def test_the_choice_is_remembered_per_person(self, client_in, boss, post):
        board(client_in, by="featured")
        row = ListPreference.objects.get(user=boss, resource_key="testapp.post")
        assert row.list_view_fields == {"kanban": "featured"}
        assert board(client_in).context["kanban_field"] == "featured"

    def test_picking_the_default_writes_nothing(self, client_in, boss, post):
        board(client_in, by="status")
        # The row exists (it remembers the board itself); the field is not stored.
        row = ListPreference.objects.get(user=boss, resource_key="testapp.post")
        assert not row.list_view_fields

    def test_the_picker_keeps_filters_and_drops_show_more(self, client_in, post):
        html = board(client_in, q="Out", more="draft").content.decode()
        assert '<input type="hidden" name="q" value="Out">' in html
        assert 'name="more"' not in html.split("data-view-field")[1].split("</form>")[0]

    def test_a_switch_to_another_view_drops_the_field(self, client_in, post):
        response = board(client_in, by="featured")
        calendar_link = next(
            entry["url"] for entry in response.context["list_modes"] if entry["name"] == "calendar"
        )
        assert "by=" not in calendar_link


class TestMove:
    def move(self, client, post, value, **extra):
        return client.post(
            reverse(MOVE_URL),
            data=json.dumps(
                {"pk": post.pk, "value": value, "version": concurrency.token_for(post), **extra}
            ),
            content_type="application/json",
        )

    def test_a_move_writes_the_field_the_board_was_grouped_by(self, client_in, post):
        assert self.move(client_in, post, True, field="featured").status_code == 200
        post.refresh_from_db()
        assert post.featured is True
        assert post.status == Post.Status.DRAFT

    def test_without_a_field_the_default_is_written(self, client_in, post):
        assert self.move(client_in, post, "review").status_code == 200
        post.refresh_from_db()
        assert post.status == "review"

    def test_a_field_the_board_cannot_be_grouped_by_writes_the_default(self, client_in, post):
        response = self.move(client_in, post, "Hijacked", field="title")
        post.refresh_from_db()
        assert post.title == "Out now"
        assert response.status_code != 200 or post.status != "Hijacked"

    def test_a_remembered_choice_does_not_redirect_a_move(self, client_in, post):
        board(client_in, by="featured")
        assert self.move(client_in, post, "review").status_code == 200
        post.refresh_from_db()
        assert (post.status, post.featured) == ("review", False)

    def test_the_no_script_menu_posts_the_field(self, client_in, post):
        assert 'name="field" value="featured"' in board(client_in, by="featured").content.decode()


class TestCalendar:
    def test_dates_from_switches_the_date_field(self, client_in, post):
        response = calendar(client_in, by="created_at")
        assert response.context["calendar_field"] == "created_at"
        assert "Dates from" in response.content.decode()

    def test_month_navigation_keeps_the_field(self, client_in, post):
        response = calendar(client_in, by="created_at")
        assert "by=created_at" in response.context["calendar_next_url"]


class TestSavedViews:
    def test_the_field_is_saved_with_the_view(self, panel):
        from django.http import QueryDict

        state = capture(panel, QueryDict("view=kanban&by=featured"))
        assert state["list_view_field"] == "featured"
        assert "by=featured" in hydrate(panel, state).query

    def test_a_field_that_no_longer_qualifies_is_reported(self, panel):
        hydrated = hydrate(panel, {"list_view": "kanban", "list_view_field": "title"})
        assert "by=" not in hydrated.query
        assert [ref.parameter for ref in hydrated.broken] == ["by"]


class TestChecks:
    def ids(self, panel):
        return [error.id for error in ModelPanelChecks().check(panel)]

    def test_a_field_that_cannot_group_a_board_is_e078(self, panel):
        panel.kanban_fields = ["title"]
        assert "vantage.E078" in self.ids(panel)

    def test_a_field_that_is_not_a_date_is_e079(self, panel):
        panel.calendar_fields = ["status"]
        assert "vantage.E079" in self.ids(panel)

    def test_a_string_instead_of_a_list_is_e078(self, panel):
        panel.kanban_fields = "status"
        assert "vantage.E078" in self.ids(panel)

    def test_valid_lists_pass(self, panel):
        panel.kanban_fields = ["featured"]
        panel.calendar_fields = ["created_at"]
        assert not {"vantage.E078", "vantage.E079"} & set(self.ids(panel))


class TestScaffold:
    def test_the_choices_are_written_only_where_there_is_a_choice(self):
        from tests.testapp.models import Venue
        from vantage.scaffold import _list_view_lines

        post = "\n".join(_list_view_lines(Post))
        assert 'kanban_fields = ["status", "featured"]' in post
        assert 'calendar_fields = ["published_at", "created_at"]' in post
        assert "kanban_fields" not in "\n".join(_list_view_lines(Venue))
