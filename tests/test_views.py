"""The pages have to actually render. Everything else is theory until they do."""

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from tests.testapp.models import Author, Post


@pytest.fixture
def staff(db):
    return User.objects.create_user("staffer", password="x", is_staff=True)


@pytest.fixture
def client_in(client, staff):
    client.force_login(staff)
    return client


@pytest.fixture
def posts(db):
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    return [
        Post.objects.create(
            title=f"Post {i}", slug=f"post-{i}", body="Body text", author=author,
            status=Post.Status.PUBLISHED if i % 2 else Post.Status.DRAFT,
        )
        for i in range(1, 6)
    ]


class TestAccess:
    def test_anonymous_is_redirected_to_login(self, client, db):
        response = client.get(reverse("vantage:dashboard"))
        assert response.status_code == 302
        assert "/login" in response["Location"]

    def test_a_non_staff_user_is_redirected(self, client, db):
        User.objects.create_user("plain", password="x")
        client.login(username="plain", password="x")
        assert client.get(reverse("vantage:dashboard")).status_code == 302

    def test_staff_get_in(self, client_in):
        assert client_in.get(reverse("vantage:dashboard")).status_code == 200


class TestDashboard:
    def test_renders(self, client_in):
        assert client_in.get(reverse("vantage:dashboard")).status_code == 200

    def test_lists_registered_models(self, client_in):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "Posts" in body
        assert "Authors" in body

    def test_includes_the_theme_tokens(self, client_in):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "--background:" in body
        assert "--primary:" in body

    def test_includes_the_no_flash_script(self, client_in):
        """Without this the page paints light and then flips."""
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "vantage-theme" in body
        assert "localStorage" in body


class TestListPage:
    def url(self):
        return reverse("vantage:testapp-post-list")

    def test_renders(self, client_in, posts):
        assert client_in.get(self.url()).status_code == 200

    def test_shows_the_inferred_columns(self, client_in, posts):
        body = client_in.get(self.url()).content.decode()
        assert "Title" in body
        assert "Status" in body

    def test_shows_the_rows(self, client_in, posts):
        body = client_in.get(self.url()).content.decode()
        assert "Post 1" in body
        assert "Ada Lovelace" in body

    def test_renders_choices_as_badges(self, client_in, posts):
        body = client_in.get(self.url()).content.decode()
        assert "v-badge" in body
        assert "Published" in body

    def test_search_filters_the_rows(self, client_in, posts):
        body = client_in.get(self.url(), {"q": "Post 3"}).content.decode()
        assert "Post 3" in body
        assert "Post 4" not in body

    def test_search_reaches_into_the_related_author(self, client_in, posts):
        body = client_in.get(self.url(), {"q": "Lovelace"}).content.decode()
        assert "Post 1" in body

    def test_a_search_with_no_matches_echoes_the_query(self, client_in, posts):
        """Empty-state 2: say what was searched, offer to clear it, no create CTA."""
        body = client_in.get(self.url(), {"q": "zzzznope"}).content.decode()
        assert "zzzznope" in body
        assert "Clear search" in body

    def test_an_empty_model_gets_the_first_run_empty_state(self, client_in, db):
        body = client_in.get(reverse("vantage:testapp-archive-list")).content.decode()
        assert "No archives yet" in body
        assert "Clear search" not in body

    def test_list_page_issues_no_n_plus_one(self, client_in, django_assert_num_queries, posts):
        """The promise the optimiser exists to keep, asserted rather than described."""
        with django_assert_num_queries(4):
            client_in.get(self.url())

    def test_adding_rows_does_not_add_queries(self, client_in, django_assert_num_queries, posts):
        author = Author.objects.create(name="Grace Hopper", email="grace@example.com")
        for i in range(20, 40):
            Post.objects.create(
                title=f"Extra {i}", slug=f"extra-{i}", body="b", author=author
            )
        with django_assert_num_queries(4):
            client_in.get(self.url())


class TestPagination:
    def test_paginates(self, client_in, db):
        author = Author.objects.create(name="A", email="a@example.com")
        for i in range(60):
            Post.objects.create(title=f"P{i}", slug=f"p-{i}", body="b", author=author)
        body = client_in.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "Next" in body
        assert "of 60" in body

    def test_paging_preserves_the_search(self, client_in, db):
        """Losing filter state on page 2 is the classic pagination bug."""
        author = Author.objects.create(name="A", email="a@example.com")
        for i in range(60):
            Post.objects.create(title=f"Keep {i}", slug=f"k-{i}", body="b", author=author)
        body = client_in.get(reverse("vantage:testapp-post-list"), {"q": "Keep"}).content.decode()
        assert "q=Keep" in body


class TestTheme:
    def test_toggle_sets_a_cookie(self, client_in):
        response = client_in.post(reverse("vantage:theme"), {"theme": "dark"})
        assert response.status_code == 204
        assert client_in.cookies["vantage-theme"].value == "dark"

    def test_the_cookie_drives_the_rendered_theme(self, client_in):
        client_in.cookies["vantage-theme"] = "dark"
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert 'data-theme="dark"' in body

    def test_get_is_rejected(self, client_in):
        assert client_in.get(reverse("vantage:theme")).status_code == 405

    def test_a_junk_value_is_ignored(self, client_in):
        client_in.post(reverse("vantage:theme"), {"theme": "chartreuse"})
        assert client_in.cookies.get("vantage-theme").value == ""


class TestBranding:
    @override_settings(VANTAGE={"SITE_TITLE": "Acme Admin", "BRAND_COLOR": "#ef4444"})
    def test_title_and_brand_reach_the_page(self, client_in):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "Acme Admin" in body
        assert "AA" in body  # derived acronym in the collapsed mark

    @override_settings(VANTAGE={"ENVIRONMENT": "Production"})
    def test_environment_banner_shows(self, client_in):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "v-env-banner" in body
        assert "Production" in body

    @override_settings(VANTAGE={"THEME_TOGGLE": False})
    def test_the_toggle_can_be_turned_off(self, client_in):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "data-vantage-theme-toggle" not in body


class TestRtl:
    @override_settings(LANGUAGE_CODE="ar", USE_I18N=True)
    def test_rtl_direction_is_set(self, client_in):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert 'dir="rtl"' in body
