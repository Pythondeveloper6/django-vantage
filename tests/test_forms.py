"""Create, edit and delete — the writable half of the panel.

The views are not mounted by ``vantage.urls`` yet, so this module doubles as its own
urlconf: ``_writable_urls`` builds the routes exactly as they are meant to be wired,
including the ordering constraint that matters (``<path:pk>`` swallows any suffix, so
the verb routes have to precede the detail route or ``/5/change/`` resolves as a detail
page for a record whose pk is the string ``"5/change"``).
"""

import pytest
from django.contrib.auth.models import Permission, User
from django.urls import reverse

from tests.testapp.models import Author, Category, Comment, Order, Post, Tag
from vantage import formviews, site
from vantage.permissions import PanelPermissions

# These routes now live in ``vantage.urls.build``. This module originally carried its
# own copy because the views were not yet wired; keeping one would mean every new
# route silently breaks these tests instead of exercising the real urlconf.
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def boss(client):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


@pytest.fixture
def author():
    return Author.objects.create(name="Ada Lovelace", email="ada@example.com")


@pytest.fixture
def post(author):
    return Post.objects.create(
        title="On Analytical Engines",
        slug="analytical-engines",
        body="Long body text.",
        author=author,
        status=Post.Status.DRAFT,
    )


@pytest.fixture
def post_panel():
    return site.get_panel(Post)


def add_url(model):
    return reverse(f"vantage:{model._meta.label_lower.replace('.', '-')}-add")


def change_url(obj):
    label = obj._meta.label_lower.replace(".", "-")
    return reverse(f"vantage:{label}-change", args=[obj.pk])


def delete_url(obj):
    label = obj._meta.label_lower.replace(".", "-")
    return reverse(f"vantage:{label}-delete", args=[obj.pk])


def post_payload(author, **overrides):
    """A complete, valid Post form submission."""
    data = {
        "title": "A New Post",
        "slug": "a-new-post",
        "body": "Body copy.",
        "summary": "",
        "status": Post.Status.DRAFT,
        "view_count": "0",
        "rating": "",
        "published_at": "",
        "author": str(author.pk),
        "category": "",
        "tags": [],
        "metadata": "{}",
    }
    data.update(overrides)
    return data


def grant(user, model, action):
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(model)
    user.user_permissions.add(
        Permission.objects.get(content_type=ct, codename=f"{action}_{model._meta.model_name}")
    )
    return User.objects.get(pk=user.pk)  # drop the cached permission set


def staff_client(client, model, *actions):
    user = User.objects.create_user("staffer", password="x", is_staff=True)
    for action in actions:
        user = grant(user, model, action)
    client.force_login(user)
    return client


# ── create ──────────────────────────────────────────────────────────────────


class TestCreate:
    def test_renders_a_blank_form(self, boss):
        body = boss.get(add_url(Post)).content.decode()
        assert 'name="title"' in body
        assert 'name="slug"' in body

    def test_renders_the_inferred_sections(self, boss):
        body = boss.get(add_url(Post)).content.decode()
        assert "Details" in body
        assert "Relationships" in body
        assert "Options" in body

    def test_uses_the_inferred_widget_attributes(self, boss):
        """The controls come from WidgetSpec, not from Django's 2005 defaults."""
        body = boss.get(add_url(Author)).content.decode()
        assert 'type="email"' in body
        assert 'data-vantage-kind="email"' in body

    def test_saves_a_real_row(self, boss, author):
        response = boss.post(add_url(Post), post_payload(author))
        assert response.status_code == 302
        created = Post.objects.get(slug="a-new-post")
        assert created.title == "A New Post"
        assert created.author == author

    def test_redirects_to_the_new_record(self, boss, author):
        response = boss.post(add_url(Post), post_payload(author))
        created = Post.objects.get(slug="a-new-post")
        assert response["Location"] == reverse("vantage:testapp-post-detail", args=[created.pk])

    def test_reports_success(self, boss, author):
        response = boss.post(add_url(Post), post_payload(author), follow=True)
        assert "Created post" in str(next(iter(response.context["messages"])))

    def test_writes_many_to_many(self, boss, author):
        tag = Tag.objects.create(name="engines")
        boss.post(add_url(Post), post_payload(author, tags=[str(tag.pk)]))
        assert list(Post.objects.get(slug="a-new-post").tags.all()) == [tag]

    def test_carries_the_unsaved_changes_guard(self, boss):
        body = boss.get(add_url(Post)).content.decode()
        assert "data-vantage-dirty" in body
        assert "beforeunload" in body

    def test_no_template_comment_leaks_into_the_page(self, boss):
        """Django's {# #} comments do not span lines; a multi-line one renders verbatim."""
        assert "{#" not in boss.get(add_url(Post)).content.decode()

    def test_accepts_uploads(self, boss):
        """A form that decides its encoding per-page silently drops the first upload."""
        assert 'enctype="multipart/form-data"' in boss.get(add_url(Author)).content.decode()


class TestValidation:
    def test_a_blank_required_field_re_renders(self, boss, author):
        response = boss.post(add_url(Post), post_payload(author, title=""))
        assert response.status_code == 200
        assert Post.objects.count() == 0

    def test_the_message_is_shown(self, boss, author):
        body = boss.post(add_url(Post), post_payload(author, title="")).content.decode()
        assert "This field is required." in body
        assert "was not saved" in body

    def test_the_control_is_marked_invalid_for_assistive_tech(self, boss, author):
        body = boss.post(add_url(Post), post_payload(author, title="")).content.decode()
        assert 'aria-invalid="true"' in body
        assert 'aria-describedby="id_title_error"' in body
        assert 'id="id_title_error"' in body

    def test_valid_values_survive_the_re_render(self, boss, author):
        body = boss.post(add_url(Post), post_payload(author, title="", body="Keep me")).content
        assert "Keep me" in body.decode()

    def test_a_duplicate_unique_value_is_reported(self, boss, author, post):
        response = boss.post(add_url(Post), post_payload(author, slug=post.slug))
        assert response.status_code == 200
        assert "already exists" in response.content.decode()


# ── update ──────────────────────────────────────────────────────────────────


class TestUpdate:
    def test_renders_bound_to_the_record(self, boss, post):
        body = boss.get(change_url(post)).content.decode()
        assert 'value="On Analytical Engines"' in body
        assert "Long body text." in body

    def test_preselects_a_relation(self, boss, post):
        body = boss.get(change_url(post)).content.decode()
        assert f'<option value="{post.author.pk}" selected>' in body

    def test_saves_changes(self, boss, post, author):
        boss.post(change_url(post), post_payload(author, title="Renamed", slug=post.slug))
        post.refresh_from_db()
        assert post.title == "Renamed"

    def test_redirects_to_the_record(self, boss, post, author):
        response = boss.post(change_url(post), post_payload(author, slug=post.slug))
        assert response["Location"] == reverse("vantage:testapp-post-detail", args=[post.pk])

    def test_does_not_create_a_second_row(self, boss, post, author):
        boss.post(change_url(post), post_payload(author, slug=post.slug))
        assert Post.objects.count() == 1

    def test_reports_success(self, boss, post, author):
        response = boss.post(
            change_url(post), post_payload(author, slug=post.slug), follow=True
        )
        assert "Saved changes" in str(next(iter(response.context["messages"])))

    def test_a_missing_record_is_a_404(self, boss):
        assert boss.get(reverse("vantage:testapp-post-change", args=[9999])).status_code == 404


class TestReadonlyFields:
    def test_rendered_as_a_value_not_an_input(self, boss, post, post_panel, monkeypatch):
        monkeypatch.setattr(post_panel, "readonly_fields", ["status"])
        body = boss.get(change_url(post)).content.decode()
        assert 'name="status"' not in body
        assert "v-form-static" in body

    def test_cannot_be_written_even_when_posted(self, boss, post, post_panel, monkeypatch):
        """Never trust the absence of an input — the field is off the form, not hidden."""
        monkeypatch.setattr(post_panel, "readonly_fields", ["status"])
        response = boss.post(
            change_url(post),
            post_payload(post.author, slug=post.slug, status=Post.Status.PUBLISHED),
        )
        assert response.status_code == 302
        post.refresh_from_db()
        assert post.status == Post.Status.DRAFT

    def test_a_non_editable_field_still_gets_displayed(self, boss, post, post_panel, monkeypatch):
        """``created_at`` is auto_now_add: naming it readonly must surface it, not drop it."""
        monkeypatch.setattr(post_panel, "readonly_fields", ["created_at"])
        assert "Created at" in boss.get(change_url(post)).content.decode()

    def test_nothing_is_shown_for_one_on_a_create_form(self, boss, post_panel, monkeypatch):
        monkeypatch.setattr(post_panel, "readonly_fields", ["created_at"])
        assert "Created at" not in boss.get(add_url(Post)).content.decode()


class TestSaveModelHook:
    def test_the_panel_hook_replaces_the_default_save(self, boss, author, post_panel, monkeypatch):
        seen = {}

        def save_model(request, obj, form, change):
            seen["change"] = change
            obj.summary = "set by the hook"
            obj.save()

        monkeypatch.setattr(post_panel, "save_model", save_model, raising=False)
        boss.post(add_url(Post), post_payload(author))
        assert Post.objects.get(slug="a-new-post").summary == "set by the hook"
        assert seen["change"] is False

    def test_it_is_told_this_is_an_edit(self, boss, post, post_panel, monkeypatch):
        seen = {}

        def save_model(request, obj, form, change):
            seen["change"] = change
            obj.save()

        monkeypatch.setattr(post_panel, "save_model", save_model, raising=False)
        boss.post(change_url(post), post_payload(post.author, slug=post.slug))
        assert seen["change"] is True


# ── delete ──────────────────────────────────────────────────────────────────


class TestDeleteConfirmation:
    def test_renders(self, boss, post):
        assert boss.get(delete_url(post)).status_code == 200

    def test_no_template_comment_leaks_into_the_page(self, boss, post):
        assert "{#" not in boss.get(delete_url(post)).content.decode()

    def test_lists_the_cascade_with_counts(self, boss, post):
        for i in range(3):
            Comment.objects.create(post=post, author_name=f"Reader {i}", body="Nice")
        body = boss.get(delete_url(post)).content.decode()
        assert "Comments" in body
        assert ">3<" in body

    def test_counts_fast_deleted_rows_too(self, boss, post):
        """Django never loads a fast-deletable child; omitting it understates the damage."""
        for i in range(4):
            Comment.objects.create(post=post, author_name=f"Reader {i}", body="Nice")
        summary = formviews.collect_cascade(post)
        assert ("Comments", 4) in [(g.label, g.count) for g in summary.groups]

    def test_the_record_itself_is_not_counted_as_collateral(self, boss, post):
        assert formviews.collect_cascade(post).total == 0

    def test_the_count_is_in_the_button_label(self, boss, post):
        for i in range(3):
            Comment.objects.create(post=post, author_name=f"Reader {i}", body="Nice")
        body = boss.get(delete_url(post)).content.decode()
        assert "Delete post and 3 related records" in body

    def test_the_label_is_singular_for_one(self, boss, post):
        Comment.objects.create(post=post, author_name="Reader", body="Nice")
        assert "and 1 related record<" in boss.get(delete_url(post)).content.decode()

    def test_a_lone_record_gets_a_plain_label(self, boss, post):
        body = boss.get(delete_url(post)).content.decode()
        assert "Delete post<" in body
        assert "Nothing else references this record." in body

    def test_set_null_relations_are_reported_separately(self, boss, post):
        """A cleared reference is not a deletion and must not be counted as one."""
        category = Category.objects.create(name="Engines", slug="engines")
        post.category = category
        post.save()
        summary = formviews.collect_cascade(category)
        assert summary.total == 0
        assert [(g.label, g.count) for g in summary.cleared] == [("Posts — category cleared", 1)]

    def test_a_protected_relation_blocks_the_delete(self, boss, author):
        Order.objects.create(number="A-1", customer=author, total="10.00")
        response = boss.get(delete_url(author))
        assert "cannot be deleted while Orders still reference it" in response.content.decode()
        assert "v-btn-destructive" not in response.content.decode()


class TestDeletePerformed:
    def test_deletes_the_record(self, boss, post):
        pk = post.pk
        assert boss.post(delete_url(post)).status_code == 302
        assert not Post.objects.filter(pk=pk).exists()

    def test_deletes_the_cascade(self, boss, post):
        for i in range(3):
            Comment.objects.create(post=post, author_name=f"Reader {i}", body="Nice")
        boss.post(delete_url(post))
        assert Comment.objects.count() == 0

    def test_redirects_to_the_list(self, boss, post):
        response = boss.post(delete_url(post))
        assert response["Location"] == reverse("vantage:testapp-post-list")

    def test_reports_what_went_with_it(self, boss, post):
        Comment.objects.create(post=post, author_name="Reader", body="Nice")
        response = boss.post(delete_url(post), follow=True)
        assert "and 1 related record" in str(next(iter(response.context["messages"])))

    def test_a_protected_record_survives_a_forced_post(self, boss, author):
        """The guard is on the POST, not only on the confirmation page."""
        Order.objects.create(number="A-1", customer=author, total="10.00")
        response = boss.post(delete_url(author))
        assert response.status_code == 409
        assert Author.objects.filter(pk=author.pk).exists()

    def test_a_get_does_not_delete(self, boss, post):
        boss.get(delete_url(post))
        assert Post.objects.filter(pk=post.pk).exists()


# ── access control ──────────────────────────────────────────────────────────


class TestModelPermissions:
    def test_add_is_refused_without_the_add_permission(self, client, post):
        assert staff_client(client, Post, "view", "change").get(add_url(Post)).status_code == 403

    def test_add_is_allowed_with_it(self, client, post):
        assert staff_client(client, Post, "add").get(add_url(Post)).status_code == 200

    def test_change_is_refused_without_the_change_permission(self, client, post):
        response = staff_client(client, Post, "view", "add").get(change_url(post))
        assert response.status_code == 403

    def test_change_is_allowed_with_it(self, client, post):
        assert staff_client(client, Post, "change").get(change_url(post)).status_code == 200

    def test_delete_is_refused_without_the_delete_permission(self, client, post):
        response = staff_client(client, Post, "view", "change").get(delete_url(post))
        assert response.status_code == 403

    def test_delete_is_allowed_with_it(self, client, post):
        assert staff_client(client, Post, "delete").get(delete_url(post)).status_code == 200

    def test_a_refused_post_changes_nothing(self, client, post, author):
        staff = staff_client(client, Post, "view")
        staff.post(change_url(post), post_payload(author, title="Renamed", slug=post.slug))
        post.refresh_from_db()
        assert post.title == "On Analytical Engines"

    def test_a_refused_delete_post_changes_nothing(self, client, post):
        staff_client(client, Post, "view").post(delete_url(post))
        assert Post.objects.filter(pk=post.pk).exists()

    def test_anonymous_users_are_redirected(self, client, post):
        assert client.get(change_url(post)).status_code == 302


class NoObjectWrites(PanelPermissions):
    """Model-level yes, object-level no. Proves the per-record check actually runs."""

    def has_object_perm(self, request, model, action, obj):
        return action == "view"


class HideEverything(PanelPermissions):
    """Row scoping that hides every row, to prove an invisible record 404s."""

    def filter_queryset(self, request, queryset, action="view"):
        return queryset.none()


class TestObjectPermissions:
    @pytest.fixture(autouse=True)
    def resolver(self, settings):
        settings.VANTAGE = {"PERMISSIONS": "tests.test_forms.NoObjectWrites"}

    def test_edit_is_refused_for_this_record(self, boss, post):
        assert boss.get(change_url(post)).status_code == 403

    def test_delete_is_refused_for_this_record(self, boss, post):
        assert boss.get(delete_url(post)).status_code == 403

    def test_the_record_survives_a_posted_delete(self, boss, post):
        boss.post(delete_url(post))
        assert Post.objects.filter(pk=post.pk).exists()

    def test_creating_is_unaffected(self, boss, author):
        """There is no object yet, so there is nothing to ask an object-level question about."""
        assert boss.post(add_url(Post), post_payload(author)).status_code == 302


class TestScopedQuerysets:
    @pytest.fixture(autouse=True)
    def resolver(self, settings):
        settings.VANTAGE = {"PERMISSIONS": "tests.test_forms.HideEverything"}

    def test_an_out_of_scope_record_is_a_404_not_a_403(self, boss, post):
        """403 would confirm the record exists, which is exactly what scoping hides."""
        assert boss.get(change_url(post)).status_code == 404

    def test_the_same_holds_for_delete(self, boss, post):
        assert boss.get(delete_url(post)).status_code == 404

    def test_and_for_a_posted_delete(self, boss, post):
        assert boss.post(delete_url(post)).status_code == 404
        assert Post.objects.filter(pk=post.pk).exists()

    def test_a_scoped_get_queryset_is_honoured_too(self, boss, post, post_panel, monkeypatch):
        monkeypatch.setattr(
            post_panel, "get_queryset", lambda request: Post.objects.filter(status="published")
        )
        assert boss.get(change_url(post)).status_code == 404


class TestWhenTheCascadePreviewCannotBeComputed:
    """A preview that fails must not read as a preview that found nothing.

    Django's ``Collector`` walks every model in the process with a foreign key into
    the target — not only the ones this panel registers, and not only the ones that
    have been migrated. So a missing table anywhere in that graph used to 500 a page
    that was merely counting rows: an app installed but not migrated, a model behind
    a feature flag, or one routed to a database this connection cannot reach.

    The count is advisory, so losing it must not block the delete. But an empty
    summary and a failed one look identical on the page and lead to opposite
    decisions, which is why the failure is its own state rather than a fallback to
    "nothing else references this record".
    """

    @pytest.fixture
    def broken_collector(self, monkeypatch):
        from django.db import DatabaseError
        from django.db.models.deletion import Collector

        def explode(self, *args, **kwargs):
            raise DatabaseError("no such table: some_unmigrated_thing")

        monkeypatch.setattr(Collector, "collect", explode)

    def test_the_page_still_renders(self, boss, post, broken_collector):
        assert boss.get(delete_url(post)).status_code == 200

    def test_it_says_the_preview_is_missing_rather_than_saying_nothing_is_affected(
        self, boss, post, broken_collector
    ):
        body = boss.get(delete_url(post)).content.decode()
        assert "could not be worked out" in body
        assert "Nothing else references this record" not in body

    def test_the_delete_is_not_blocked_by_a_failed_preview(self, boss, post, broken_collector):
        """Advisory means advisory: losing the count must not remove the button."""
        summary = formviews.collect_cascade(post)
        assert summary.unavailable is not None
        assert summary.is_blocked is False
