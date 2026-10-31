"""The phone layout, as far as a test client can see it.

The layout itself is CSS and cannot be asserted from here (``tests/test_browser.py``'s
phone class drives a real viewport for that). What a test client can pin is the markup
the CSS stands on: a labelled, opt-in set of cells for the cards, the sheet's dismiss
button, the viewport meta that makes safe-area insets work, and the stylesheet link
that keeps all of it off wider screens.
"""

import re

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from tests.testapp.models import Author, Category, Post
from vantage import site

LIST_URL = "vantage:testapp-post-list"


@pytest.fixture
def client_in(client, db):
    client.force_login(User.objects.create_superuser("phone", "phone@example.com", "x"))
    return client


@pytest.fixture
def post(db):
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    category = Category.objects.create(name="Essays", slug="essays")
    return Post.objects.create(
        title="Notes", slug="notes", body="Body", author=author, category=category
    )


def first_row(client) -> str:
    body = client.get(reverse(LIST_URL)).content.decode()
    match = re.search(r"<tbody>.*?<tr\b.*?</tr>", body, re.S)
    assert match, "the list rendered no rows"
    return match.group(0)


def labels(row: str) -> list[str]:
    return re.findall(r'<td[^>]*\bdata-card data-label="([^"]*)"', row)


def card_cells(row: str) -> int:
    return len(re.findall(r"<td[^>]*\bdata-card\b", row))


class TestCardCells:
    def test_the_first_columns_are_cards_by_default(self, client_in, post):
        row = first_row(client_in)
        columns = site.get_panel(Post).get_list_display(None)
        shown = labels(row)
        assert 1 <= len(shown) <= 4
        assert len(shown) == min(4, len(columns))

    def test_the_title_is_the_first_column(self, client_in, post):
        row = first_row(client_in)
        assert re.search(r'<td[^>]*data-col-index="0"[^>]*data-card data-label="[^"]+"', row)

    def test_columns_past_the_limit_are_not_cards(self, client_in, post):
        columns = site.get_panel(Post).get_list_display(None)
        if len(columns) <= 4:
            pytest.skip("the panel has no more columns than a card shows")
        row = first_row(client_in)
        assert card_cells(row) == 4

    def test_card_fields_chooses_the_columns(self, client_in, post, monkeypatch):
        panel = site.get_panel(Post)
        columns = panel.get_list_display(None)
        picked = columns[-1]
        monkeypatch.setattr(panel, "card_fields", [picked])
        row = first_row(client_in)
        # The title always stays, and the one named column joins it.
        assert card_cells(row) == (2 if len(columns) > 1 else 1)
        assert f'data-col-index="{len(columns) - 1}" data-card' in row

    def test_card_fields_naming_a_non_column_adds_nothing(self, client_in, post, monkeypatch):
        monkeypatch.setattr(site.get_panel(Post), "card_fields", ["not_a_column"])
        assert card_cells(first_row(client_in)) == 1

    def test_labels_are_escaped(self, client_in, post, monkeypatch):
        from vantage import views

        monkeypatch.setattr(
            views.ListView,
            "_card_columns",
            classmethod(lambda cls, panel, columns, headers: {0: '"><b>x</b>'}),
        )
        row = first_row(client_in)
        assert "<b>x</b>" not in row


class TestShell:
    def test_the_viewport_reaches_the_screen_edges(self, client_in, post):
        body = client_in.get(reverse(LIST_URL)).content.decode()
        # Without viewport-fit=cover, env(safe-area-inset-*) is always 0.
        assert "viewport-fit=cover" in body

    def test_the_phone_stylesheet_is_scoped_to_small_screens(self, client_in, post):
        body = client_in.get(reverse(LIST_URL)).content.decode()
        link = re.search(r"<link[^>]*vantage-mobile\.css[^>]*>", body)
        assert link and 'media="(max-width: 60rem)"' in link.group(0)

    def test_the_sheet_can_be_dismissed_without_a_gesture(self, client_in, post):
        body = client_in.get(reverse(LIST_URL)).content.decode()
        assert "data-action-close" in body

    def test_the_sheet_has_no_dialog_role_in_the_markup(self, client_in, post):
        # Applied by the script only while the bar is a sheet; on a desktop it is
        # still the plain toolbar it always was.
        body = client_in.get(reverse(LIST_URL)).content.decode()
        bar = re.search(r"<div class=\"v-action-bar\"[^>]*>", body).group(0)
        assert "role=" not in bar

    def test_the_navigation_toggle_still_declares_its_state(self, client_in, post):
        body = client_in.get(reverse(LIST_URL)).content.decode()
        toggle = re.search(r'<button[^>]*id="v-sidebar-toggle"[^>]*>', body).group(0)
        assert 'aria-expanded="false"' in toggle and 'aria-controls="v-sidebar"' in toggle
