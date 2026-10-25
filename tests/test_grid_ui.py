"""The data grid's markup: APG grid semantics, the controls, the drawer, the cheat sheet.

These are UI tests, so they assert markup. The behaviour that needs a browser — the
focus ring moving on ``j``, the drawer's history entries, localStorage round-tripping —
is verified in a browser, not here. What *is* checkable from Python is the contract the
server has to hold up for that JavaScript to be correct at all, and every assertion
below is one of those:

* ``aria-rowcount``/``aria-rowindex`` are absolute, not per page. Get this wrong and a
  screen reader announces "row 3 of 50" on page 7 — the single easiest APG grid bug to
  ship, because it looks right on page 1.
* Exactly one ``tabindex="0"`` in the whole table. Tab enters the grid once and leaves
  it once; the arrow keys do the rest. A single stray tabbable cell link breaks that.
* The cheat sheet and the shortcut preference exist, because WCAG 2.1.4 asks for them
  by name.
"""

import re

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from tests.testapp.models import Archive, Author, Category, Post

LIST_URL = "vantage:testapp-post-list"
PER_PAGE = 50


@pytest.fixture
def staff(db):
    return User.objects.create_superuser("grid", "grid@example.com", "x")


@pytest.fixture
def client_in(client, staff):
    client.force_login(staff)
    return client


@pytest.fixture
def posts(db):
    """Enough records for a second page, which is where the interesting bugs live."""
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    category = Category.objects.create(name="Essays", slug="essays")
    Post.objects.bulk_create(
        Post(
            title=f"Post {index:03d}",
            slug=f"post-{index:03d}",
            body="Body",
            status=Post.Status.DRAFT,
            author=author,
            category=category,
            view_count=index,
        )
        for index in range(55)
    )
    return Post.objects.all()


def body_of(client, **params) -> str:
    return client.get(reverse(LIST_URL), params).content.decode()


def table_of(body: str) -> str:
    """Just the ``<table>``, so a tab stop elsewhere on the page is not this suite's."""
    match = re.search(r"<table[^>]*class=\"v-table\".*?</table>", body, re.S)
    assert match, "the list page rendered no table"
    return match.group(0)


# ── APG grid semantics ──────────────────────────────────────────────────────


class TestGridSemantics:
    def test_the_table_is_a_grid(self, client_in, posts):
        table = table_of(body_of(client_in))
        assert 'role="grid"' in table

    def test_rowcount_is_the_whole_result_set(self, client_in, posts):
        assert 'aria-rowcount="55"' in body_of(client_in)

    def test_rowcount_follows_the_filtered_set_not_the_table(self, client_in, posts):
        body = body_of(client_in, q="Post 00")
        # "Post 000".."Post 009" — the count has to be about what the filter matched.
        assert 'aria-rowcount="10"' in body

    def test_rowindex_is_absolute_on_page_two(self, client_in, posts):
        body = body_of(client_in, page=2)
        indexes = [int(value) for value in re.findall(r'aria-rowindex="(\d+)"', body)]
        assert indexes == [51, 52, 53, 54, 55]

    def test_rowindex_starts_at_one_on_page_one(self, client_in, posts):
        indexes = [int(value) for value in re.findall(r'aria-rowindex="(\d+)"', body_of(client_in))]
        assert indexes[0] == 1
        assert len(indexes) == PER_PAGE

    def test_exactly_one_tab_stop_in_the_table(self, client_in, posts):
        table = table_of(body_of(client_in))
        assert table.count('tabindex="0"') == 1

    def test_exactly_one_tab_stop_on_page_two_too(self, client_in, posts):
        # The roving tabindex is server-rendered, so every page has to ship its own.
        table = table_of(body_of(client_in, page=2))
        assert table.count('tabindex="0"') == 1

    def test_the_tab_stop_is_the_first_data_row(self, client_in, posts):
        table = table_of(body_of(client_in))
        first_row = re.search(r"<tbody>\s*(<tr[^>]*>)", table)
        assert first_row and 'tabindex="0"' in first_row.group(1)

    def test_the_header_row_is_navigable_but_not_a_tab_stop(self, client_in, posts):
        table = table_of(body_of(client_in))
        header = re.search(r"<thead>\s*(<tr[^>]*>)", table)
        assert header and "data-grid-row" in header.group(1)
        assert 'tabindex="-1"' in header.group(1)

    def test_cell_links_are_out_of_the_tab_order(self, client_in, posts):
        table = table_of(body_of(client_in))
        links = re.findall(r"<a[^>]*class=\"v-cell-link\"[^>]*>", table)
        assert links, "the linked column should render an anchor"
        assert all('tabindex="-1"' in link for link in links)

    def test_sort_links_are_out_of_the_tab_order(self, client_in, posts):
        table = table_of(body_of(client_in))
        links = re.findall(r"<a[^>]*class=\"v-sort\"[^>]*>", table)
        assert links
        assert all('tabindex="-1"' in link for link in links)

    def test_selection_checkboxes_are_out_of_the_tab_order(self, client_in, db):
        # Archive is the one panel with actions, so it is the one with select cells.
        Archive.objects.create(title="Old records")
        table = table_of(client_in.get(reverse("vantage:testapp-archive-list")).content.decode())
        boxes = re.findall(r"<input[^>]*data-select-(?:row|page)[^>]*>", table)
        assert len(boxes) == 2  # the page box and the one row's box
        assert all('tabindex="-1"' in box for box in boxes)

    def test_rows_carry_their_record_url_and_pk(self, client_in, posts):
        table = table_of(body_of(client_in))
        first = Post.objects.all().first()  # the model's own ordering, same as the page
        assert f'data-pk="{first.pk}"' in table
        assert f'data-url="{reverse("vantage:testapp-post-detail", args=[first.pk])}"' in table

    def test_columns_are_identified_by_name_not_position(self, client_in, posts):
        table = table_of(body_of(client_in))
        assert 'data-col-name="title"' in table
        assert 'data-col-index="0"' in table
        # Body cells carry the index so the script can hide a whole column at once.
        assert table.count('data-col-index="0"') > 1


# ── the controls ────────────────────────────────────────────────────────────


class TestGridControls:
    def test_the_controls_render(self, client_in, posts):
        body = body_of(client_in)
        assert "v-grid-controls" in body
        assert 'data-density-option="comfortable"' in body
        assert 'data-density-option="compact"' in body
        assert "data-columns-toggle" in body

    def test_the_column_count_badge_states_the_ratio(self, client_in, posts):
        body = body_of(client_in)
        columns = body_of(client_in).count('data-col-name="')
        assert f"· {columns} of {columns}" in body

    def test_the_columns_popover_has_a_search_box(self, client_in, posts):
        body = body_of(client_in)
        assert "data-columns-search" in body
        assert 'id="v-grid-columns-panel"' in body
        assert 'aria-controls="v-grid-columns-panel"' in body

    def test_the_shortcut_preference_renders_and_defaults_on(self, client_in, posts):
        body = body_of(client_in)
        assert "data-shortcuts-toggle" in body
        assert re.search(r"<input[^>]*data-shortcuts-toggle[^>]*checked", body)

    def test_the_grid_carries_the_page_links_with_the_query_intact(self, client_in, posts):
        # "[" and "]" have to keep the search, or paging silently widens the result set.
        body = body_of(client_in, q="Post 0", page=2)
        root = re.search(r"<div class=\"v-grid\"[^>]*>", body).group(0)
        assert "data-prev-url" in root
        assert "q=Post" in root

    def test_no_controls_and_no_drawer_when_there_is_nothing_to_drive(self, client_in, db):
        body = body_of(client_in)
        assert "v-empty-title" in body
        assert "v-grid-controls" not in body
        assert "data-grid-drawer" not in body
        # And no stylesheet or script for a feature that is not on the page.
        assert "vantage-grid.js" not in body

    def test_the_assets_are_loaded_from_the_partial(self, client_in, posts):
        body = body_of(client_in)
        assert "vantage/vantage-grid.css" in body
        assert "vantage/vantage-grid.js" in body


# ── the cheat sheet ─────────────────────────────────────────────────────────


class TestCheatSheet:
    def test_it_renders_as_a_dialog(self, client_in, posts):
        body = body_of(client_in)
        assert "v-grid-cheatsheet" in body
        assert 'id="v-grid-cheatsheet-title"' in body

    @pytest.mark.parametrize(
        "key",
        [
            "<kbd>j</kbd>",
            "<kbd>k</kbd>",
            "<kbd>x</kbd>",
            "<kbd>Enter</kbd>",
            "<kbd>Space</kbd>",
            "<kbd>Home</kbd>",
            "<kbd>End</kbd>",
            "<kbd>[</kbd>",
            "<kbd>]</kbd>",
            "<kbd>?</kbd>",
            "<kbd>Esc</kbd>",
            "<kbd>Tab</kbd>",
        ],
    )
    def test_every_binding_is_listed(self, client_in, posts, key):
        # WCAG 2.1.4 wants the keymap discoverable; a shortcut missing from this list
        # is a shortcut nobody can find.
        assert key in body_of(client_in)

    def test_it_can_be_opened_without_the_keyboard(self, client_in, posts):
        assert "data-cheatsheet-open" in body_of(client_in)


# ── the drawer ──────────────────────────────────────────────────────────────


class TestPeekDrawer:
    def test_it_renders_closed_and_non_modal(self, client_in, posts):
        body = body_of(client_in)
        drawer = re.search(r"<aside class=\"v-grid-drawer\"[^>]*>", body).group(0)
        assert "hidden" in drawer
        assert 'tabindex="-1"' in drawer
        # role=dialog without aria-modal: focus goes in, but nothing is trapped.
        assert 'role="dialog"' in drawer
        assert "aria-modal" not in drawer

    def test_it_ships_pointer_controls_for_everything_the_keyboard_does(self, client_in, posts):
        body = body_of(client_in)
        hooks = ("data-drawer-prev", "data-drawer-next", "data-drawer-close", "data-drawer-open")
        for hook in hooks:
            assert hook in body


# ── template hygiene ────────────────────────────────────────────────────────


class TestTemplateHygiene:
    def test_no_comment_leaks_into_the_page(self, client_in, posts):
        # Django's {# #} is single-line only: a multi-line one renders verbatim into
        # the page. Nothing this suite touches may leak either delimiter.
        body = body_of(client_in)
        assert "{#" not in body
        assert "#}" not in body
        assert "{%" not in body

    def test_every_form_on_the_list_page_is_closed(self, client_in, posts):
        # The action form used to run to the end of the document, swallowing the
        # pagination and the drawer with it.
        body = body_of(client_in)
        assert body.count("<form") == body.count("</form>")
