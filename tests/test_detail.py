"""The detail page: one record, its fields, and what points at it."""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from tests.testapp.models import Author, Comment, Post


@pytest.fixture
def client_in(client, db):
    # A superuser: these tests cover rendering, not access control.
    client.force_login(User.objects.create_superuser("staffer", "s@example.com", "x"))
    return client


@pytest.fixture
def post(db):
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    post = Post.objects.create(
        title="On Analytical Engines",
        slug="analytical-engines",
        body="Long body text.",
        author=author,
        status=Post.Status.PUBLISHED,
    )
    for i in range(3):
        Comment.objects.create(post=post, author_name=f"Reader {i}", body="Nice")
    return post


def detail_url(obj):
    label = obj._meta.label_lower.replace(".", "-")
    return reverse(f"vantage:{label}-detail", args=[obj.pk])


class TestDetailPage:
    def test_renders(self, client_in, post):
        assert client_in.get(detail_url(post)).status_code == 200

    def test_shows_the_record_title(self, client_in, post):
        body = client_in.get(detail_url(post)).content.decode()
        assert "On Analytical Engines" in body

    def test_shows_field_values(self, client_in, post):
        body = client_in.get(detail_url(post)).content.decode()
        assert "Ada Lovelace" in body
        assert "analytical-engines" in body

    def test_shows_long_text_the_list_page_omits(self, client_in, post):
        """The body is too long for a column but belongs on the record."""
        body = client_in.get(detail_url(post)).content.decode()
        assert "Long body text." in body

    def test_previews_related_records(self, client_in, post):
        body = client_in.get(detail_url(post)).content.decode()
        assert "Comments" in body
        assert "Reader 0" in body

    def test_the_preview_is_bounded_however_many_children_exist(self, client_in, post):
        """Loading every child is what makes a big record impossible to open."""
        from vantage.views import DetailView

        for i in range(50):
            Comment.objects.create(post=post, author_name=f"Extra {i}", body="b")
        body = client_in.get(detail_url(post)).content.decode()
        assert body.count("Extra ") <= DetailView.related_preview
        # Assert the affordance, not its wording: there must be a way through to the
        # rest, and it must say how many were held back.
        assert "v-related-more" in body
        assert str(50 - DetailView.related_preview) in body

    def test_the_preview_cost_does_not_grow_with_the_child_count(self, client_in, post):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as few:
            client_in.get(detail_url(post))

        Comment.objects.bulk_create(
            Comment(post=post, author_name=f"Bulk {i}", body="b") for i in range(200)
        )
        with CaptureQueriesContext(connection) as many:
            client_in.get(detail_url(post))

        assert len(many) == len(few)

    def test_related_link_is_prefiltered_to_this_record(self, client_in, post):
        body = client_in.get(detail_url(post)).content.decode()
        assert f"post={post.pk}" in body

    def test_breadcrumb_links_back_to_the_list(self, client_in, post):
        body = client_in.get(detail_url(post)).content.decode()
        assert reverse("vantage:testapp-post-list") in body

    def test_a_missing_record_is_a_404(self, client_in, db):
        url = reverse("vantage:testapp-post-detail", args=[999999])
        assert client_in.get(url).status_code == 404

    def test_anonymous_users_are_redirected(self, client, post):
        assert client.get(detail_url(post)).status_code == 302

    def test_sensitive_fields_are_not_shown(self, client_in, db):
        from django.db import models

        class Credential(models.Model):
            name = models.CharField(max_length=50)
            api_token = models.CharField(max_length=64)

            class Meta:
                app_label = "testapp"

        from vantage.views import _is_hidden

        fields = {f.name for f in Credential._meta.get_fields() if not _is_hidden(f)}
        assert "name" in fields
        assert "api_token" not in fields

    def test_respects_a_scoped_queryset(self, client_in, post):
        """get_queryset is the multi-tenancy hook; the detail page must honour it."""
        from vantage.panels import ModelPanel

        class Scoped(ModelPanel):
            def get_queryset(self, request):
                return Post.objects.filter(status="draft")

        from vantage.views import DetailView

        view = DetailView(site=None, panel=Scoped(Post), kwargs={"pk": post.pk})
        view.request = None
        with pytest.raises(Exception, match="No Post matches"):
            view.get_object()


class TestListLinksToDetail:
    def test_the_linked_column_is_an_anchor(self, client_in, post):
        body = client_in.get(reverse("vantage:testapp-post-list")).content.decode()
        assert f'href="{detail_url(post)}"' in body


class TestRelatedTabs:
    """``ModelPanel.related_tabs``: a tab strip once there is a choice to make."""

    @pytest.fixture
    def author_with_two_relations(self, db):
        author = Author.objects.create(name="Grace Hopper", email="grace@example.com")
        Post.objects.create(
            title="On Compilers",
            slug="on-compilers",
            body="Body.",
            author=author,
            status=Post.Status.PUBLISHED,
        )
        from tests.testapp.models import Order

        Order.objects.create(number="ORD-1", customer=author, total="10.00")
        return author

    def test_a_single_relation_stays_stacked_by_default(self, client_in, post):
        """Post has one relation (comments): a one-tab strip is a control that does
        nothing, so it stays the plain card."""
        body = client_in.get(detail_url(post)).content.decode()
        assert "v-related-blocks" in body
        assert "v-tabstrip" not in body

    def test_two_or_more_relations_become_tabs_by_default(
        self, client_in, author_with_two_relations
    ):
        body = client_in.get(detail_url(author_with_two_relations)).content.decode()
        assert "v-tabstrip" in body
        assert "v-related-blocks" not in body
        assert "Posts" in body
        assert "Orders" in body

    def test_related_tabs_true_asserts_a_strip_even_for_one_relation(
        self, client_in, post
    ):
        from vantage import site

        panel = site.get_panel(Post)
        original = panel.related_tabs
        panel.related_tabs = True
        try:
            body = client_in.get(detail_url(post)).content.decode()
        finally:
            panel.related_tabs = original
        assert "v-tabstrip" in body
        assert "v-related-blocks" not in body

    def test_related_tabs_false_keeps_the_stack_even_for_several_relations(
        self, client_in, author_with_two_relations
    ):
        from vantage import site

        panel = site.get_panel(Author)
        original = panel.related_tabs
        panel.related_tabs = False
        try:
            body = client_in.get(detail_url(author_with_two_relations)).content.decode()
        finally:
            panel.related_tabs = original
        assert "v-related-blocks" in body
        assert "v-tabstrip" not in body

    def test_every_related_block_still_renders_inside_the_tab_strip(
        self, client_in, author_with_two_relations
    ):
        """The content itself — not just the chrome around it — must survive wiring."""
        body = client_in.get(detail_url(author_with_two_relations)).content.decode()
        assert "On Compilers" in body
        assert "ORD-1" in body
