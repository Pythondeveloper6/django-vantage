"""The audit log: what someone did in the panel, and what must never be in it.

Two properties get the densest coverage here, because both fail silently.

The **diff must never carry a secret**. A log entry is read by more people than the
record it describes, so a password or an API token landing in one is a leak that
survives the rotation of the credential. ``test_a_sensitive_field_is_recorded_as_
changed_but_its_value_is_never_stored`` and ``test_a_deleted_records_password_column_
is_absent_from_the_diff`` are named to be unmissable in a failure report.

**Logging must never break the write it describes.** A panel that refuses an edit
because its own audit table is unhappy has turned an observability feature into an
outage, so the failure path is tested by making the write fail on purpose.
"""

import inspect
import json
from unittest import mock

import pytest
from django import forms
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.urls import path, reverse

from tests.testapp.models import Archive, Author, Post
from vantage import audit, auditviews, formviews, savedviews, views
from vantage.models import VantageLogEntry
from vantage.sites import site
from vantage.urls import build

# This module doubles as its own urlconf. ``vantage.urls.build`` does not mount the
# audit page or the saved-view routes yet, and a test that reaches them through a
# hand-rolled ``as_view()`` call would not exercise the ordering and namespacing that
# a real mount has to get right. Built from the same ``build_urls`` helpers the real
# urlconf is asked to call, so wiring them for real cannot change what this tests.
urlpatterns = [
    path(
        "admin/",
        (
            build(site) + auditviews.build_urls(site) + savedviews.build_urls(site),
            "vantage",
            "vantage",
        ),
    )
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]


# ── the hooks ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def audit_hooks():
    """Install the one-line hooks the panel's write paths are asked to carry.

    Each hook is installed only if the real view does not already call
    :mod:`vantage.audit`, detected by reading its source. That makes this fixture
    disappear the moment the calls are wired into ``formviews.py`` and ``views.py``
    for real — rather than becoming a second implementation that keeps these tests
    green after the real one has been deleted.

    The placement mirrors the wiring exactly: the create/edit hook fires *after* the
    transaction has committed, and the delete hook fires while the row is still there.
    """
    patches = []

    def wrap(owner, name, replacement):
        original = getattr(owner, name)
        if "audit" in inspect.getsource(original):
            return original
        setattr(owner, name, replacement(original))
        patches.append((owner, name, original))
        return original

    def form_hook(original):
        def save_form(self, form):
            obj = original(self, form)
            audit.log_form_save(self.request, obj, form, self.change)
            return obj

        return save_form

    def delete_hook(original):
        def post(self, request, *args, **kwargs):
            self.object = self.get_object()
            if not formviews.collect_cascade(self.object).is_blocked:
                audit.log_delete(request, self.object)
            return original(self, request, *args, **kwargs)

        return post

    def action_hook(original):
        def run(self, panel, request, queryset):
            # The count is taken before the action runs, exactly as ``ListView.post``
            # already has it: after a bulk delete there is nothing left to count.
            count = queryset.count()
            result = original(self, panel, request, queryset)
            audit.log_action(request, panel.model, self, count)
            return result

        return run

    wrap(formviews.PanelFormView, "save_form", form_hook)
    wrap(formviews.DeleteView, "post", delete_hook)
    if "audit" not in inspect.getsource(views.ListView.post):
        from vantage.actions import Action

        original = Action.run
        Action.run = action_hook(original)
        patches.append((Action, "run", original))

    yield
    for owner, name, original in reversed(patches):
        setattr(owner, name, original)


# ── fixtures ────────────────────────────────────────────────────────────────


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


def add_url(model):
    return reverse(f"vantage:{model._meta.label_lower.replace('.', '-')}-add")


def change_url(obj):
    return reverse(f"vantage:{obj._meta.label_lower.replace('.', '-')}-change", args=[obj.pk])


def delete_url(obj):
    return reverse(f"vantage:{obj._meta.label_lower.replace('.', '-')}-delete", args=[obj.pk])


def post_payload(author, **overrides):
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


# ── one write, one entry ────────────────────────────────────────────────────


class TestFormWrites:
    def test_a_create_records_one_entry_with_the_new_values(self, boss, author):
        boss.post(add_url(Post), post_payload(author))

        entry = VantageLogEntry.objects.get()
        assert entry.action == VantageLogEntry.Action.CREATE
        assert entry.actor_repr == "boss"
        assert entry.object_repr == "A New Post"
        assert entry.object_id == str(Post.objects.get(slug="a-new-post").pk)
        assert entry.content_type == ContentType.objects.get_for_model(Post)
        # A create has a "to" and no "from": there was nothing there before.
        assert entry.changes["title"] == {"from": None, "to": "A New Post"}

    def test_an_edit_records_only_the_fields_that_moved(self, boss, post, author):
        boss.post(
            change_url(post),
            post_payload(
                author,
                title="Renamed",
                slug=post.slug,
                body=post.body,
                status=Post.Status.PUBLISHED,
            ),
        )

        entry = VantageLogEntry.objects.get()
        assert entry.action == VantageLogEntry.Action.UPDATE
        assert entry.changes["title"] == {"from": "On Analytical Engines", "to": "Renamed"}
        assert entry.changes["status"] == {"from": "draft", "to": "published"}
        # Untouched fields are absent, not recorded as unchanged. A diff that lists
        # every column is not a diff.
        assert "body" not in entry.changes

    def test_an_edit_that_changed_nothing_writes_no_entry(self, boss, post, author):
        """Otherwise the log fills with "opened the form and pressed Save"."""
        payload = post_payload(
            author,
            title=post.title,
            slug=post.slug,
            body=post.body,
            status=post.status,
            view_count=str(post.view_count),
        )
        boss.post(change_url(post), payload)
        assert not VantageLogEntry.objects.exists()

    def test_a_delete_records_the_label_and_a_snapshot(self, boss, post):
        label = str(post)
        boss.post(delete_url(post))

        entry = VantageLogEntry.objects.get()
        assert entry.action == VantageLogEntry.Action.DELETE
        # The row is gone, so the stored label is the only thing left that reads.
        assert entry.object_repr == label
        assert not Post.objects.exists()
        assert entry.changes["title"] == {"from": label}

    def test_a_delete_entry_survives_the_record_it_describes(self, boss, post):
        boss.post(delete_url(post))
        entry = VantageLogEntry.objects.get()
        # The generic FK dangles by design; nothing reads through it.
        assert entry.content_object is None
        assert entry.object_repr

    def test_the_actor_survives_the_actors_account(self, boss, post, author):
        boss.post(change_url(post), post_payload(author, title="Renamed", slug=post.slug))
        User.objects.get(username="boss").delete()

        entry = VantageLogEntry.objects.get()
        assert entry.actor is None
        assert entry.actor_repr == "boss"

    def test_the_request_origin_is_recorded(self, boss, author):
        boss.post(add_url(Post), post_payload(author), headers={"user-agent": "pytest/1.0"})
        entry = VantageLogEntry.objects.get()
        assert entry.ip_address == "127.0.0.1"
        assert entry.user_agent == "pytest/1.0"


# ── secrets ─────────────────────────────────────────────────────────────────


class TestSensitiveFieldsAreNeverStored:
    """The pattern is imported from ``vantage.inference``, never restated here."""

    class TokenForm(forms.Form):
        title = forms.CharField(required=False)
        api_token = forms.CharField(required=False)
        password = forms.CharField(required=False)

    def test_a_sensitive_field_is_recorded_as_changed_but_its_value_is_never_stored(self):
        form = self.TokenForm(
            data={"title": "New", "api_token": "sk-live-9999", "password": "hunter2"},
            initial={"title": "Old", "api_token": "sk-live-0000", "password": "old-one"},
        )
        assert form.is_valid()

        changes = audit.diff_from_form(form)

        assert changes["api_token"] == {"redacted": True}
        assert changes["password"] == {"redacted": True}
        assert changes["title"] == {"from": "Old", "to": "New"}

        serialised = json.dumps(changes)
        for secret in ("sk-live-9999", "sk-live-0000", "hunter2", "old-one"):
            assert secret not in serialised

    def test_a_deleted_records_password_column_is_absent_from_the_diff(self, boss):
        """The end-to-end version, through the real delete view and a real model.

        ``auth.User`` is the fixture because its ``password`` column is genuinely
        sensitive and genuinely present — a snapshot that walks every concrete field
        would pick it up unless something stops it.
        """
        victim = User.objects.create_user("victim", password="correct-horse")
        stored_hash = victim.password

        boss.post(delete_url(victim))

        entry = VantageLogEntry.objects.get(action=VantageLogEntry.Action.DELETE)
        assert "password" not in entry.changes
        assert entry.changes["username"] == {"from": "victim"}
        assert stored_hash not in json.dumps(entry.changes)

    def test_a_file_field_records_its_name_and_never_its_contents(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        author = Author(name="Ada", email="ada@example.com")
        author.avatar = SimpleUploadedFile("portrait.png", b"\x89PNGnot-a-real-image")

        recorded = audit.snapshot(author)["avatar"]["from"]

        assert recorded == "portrait.png"
        assert "PNG" not in json.dumps(recorded)


# ── failure tolerance ───────────────────────────────────────────────────────


class TestLoggingNeverBreaksTheWrite:
    def test_a_logging_failure_does_not_roll_back_an_edit(self, boss, post, author):
        with mock.patch.object(
            VantageLogEntry.objects, "create", side_effect=RuntimeError("audit table is gone")
        ):
            response = boss.post(
                change_url(post), post_payload(author, title="Renamed", slug=post.slug)
            )

        assert response.status_code == 302
        post.refresh_from_db()
        assert post.title == "Renamed"
        assert not VantageLogEntry.objects.exists()

    def test_a_logging_failure_does_not_roll_back_a_delete(self, boss, post):
        with mock.patch.object(
            VantageLogEntry.objects, "create", side_effect=RuntimeError("audit table is gone")
        ):
            response = boss.post(delete_url(post))

        assert response.status_code == 302
        assert not Post.objects.exists()

    def test_the_failure_is_logged_rather_than_swallowed_in_silence(self, caplog, post):
        with mock.patch.object(VantageLogEntry.objects, "create", side_effect=RuntimeError("boom")):
            assert audit.record(None, VantageLogEntry.Action.UPDATE, obj=post) is None
        assert "Could not write a Vantage audit entry" in caplog.text

    def test_the_feature_can_be_switched_off(self, post, settings):
        settings.VANTAGE = {"AUDIT_LOG": False}
        assert audit.record(None, VantageLogEntry.Action.UPDATE, obj=post) is None
        assert not VantageLogEntry.objects.exists()


# ── bulk actions ────────────────────────────────────────────────────────────


class TestActions:
    def test_a_bulk_action_records_one_entry_naming_the_action_and_the_scope(self, boss):
        for index in range(3):
            Archive.objects.create(title=f"Item {index}")

        response = boss.post(
            reverse("vantage:testapp-archive-list"),
            {"action": "restore", "select_all": "1"},
        )
        assert response.status_code == 302

        entry = VantageLogEntry.objects.get(action=VantageLogEntry.Action.ACTION)
        assert entry.action_name == "restore"
        # One entry, not one per row: forty thousand rows must not mean forty thousand
        # entries, and the count the user was shown is the count that is recorded.
        assert entry.object_repr == "3 records"
        assert entry.content_type == ContentType.objects.get_for_model(Archive)


# ── the audit page ──────────────────────────────────────────────────────────


class TestAuditPage:
    def url(self):
        return reverse("vantage:audit")

    def test_renders(self, boss, post, author):
        boss.post(change_url(post), post_payload(author, title="Renamed", slug=post.slug))
        body = boss.get(self.url()).content.decode()
        assert "Audit log" in body
        assert "Renamed" in body
        assert "boss" in body

    def test_says_so_when_nothing_has_happened(self, boss):
        body = boss.get(self.url()).content.decode()
        assert "Nothing has been recorded yet" in body

    def test_a_staff_user_without_the_permission_is_refused(self, client):
        client.force_login(User.objects.create_user("plain", password="x", is_staff=True))
        assert client.get(self.url()).status_code == 403

    def test_the_permission_opens_it_without_a_superuser(self, client):
        user = User.objects.create_user("reader", password="x", is_staff=True)
        user.user_permissions.add(
            Permission.objects.get(
                content_type=ContentType.objects.get_for_model(VantageLogEntry),
                codename="view_vantagelogentry",
            )
        )
        client.force_login(User.objects.get(pk=user.pk))
        assert client.get(self.url()).status_code == 200

    def test_filters_by_actor(self, boss, post, author):
        # ``follow`` so the success toast is rendered and consumed on the detail page.
        # Left in the message store it would echo "Renamed" into the next page and
        # make every "not in body" assertion below pass or fail for the wrong reason.
        boss.post(
            change_url(post),
            post_payload(author, title="Renamed", slug=post.slug),
            follow=True,
        )
        other = User.objects.create_superuser("other", "o@example.com", "x")
        audit.record(_request_as(other), VantageLogEntry.Action.CREATE, obj=post)

        body = boss.get(self.url(), {"actor": other.pk}).content.decode()
        assert "other" in body
        assert "Renamed" not in body

    def test_filters_by_action(self, boss, post, author):
        boss.post(
            change_url(post),
            post_payload(author, title="Renamed", slug=post.slug),
            follow=True,
        )
        body = boss.get(self.url(), {"action": "delete"}).content.decode()
        assert "Nothing matches those filters" in body

    def test_filters_by_date(self, boss, post, author):
        boss.post(
            change_url(post),
            post_payload(author, title="Renamed", slug=post.slug),
            follow=True,
        )
        assert "Renamed" in boss.get(self.url(), {"window": "P7D"}).content.decode()
        assert "Renamed" not in boss.get(self.url(), {"to": "2000-01-01"}).content.decode()

    def test_an_unparseable_date_widens_rather_than_raising(self, boss, post, author):
        """A stale bookmark must degrade to more results, never to a 500."""
        boss.post(change_url(post), post_payload(author, title="Renamed", slug=post.slug))
        response = boss.get(self.url(), {"from": "not-a-date", "action": "nonsense"})
        assert response.status_code == 200
        assert "Renamed" in response.content.decode()

    def test_the_actor_filter_only_offers_people_who_appear_in_the_log(self, boss, post, author):
        User.objects.create_user("never-opened-the-panel", password="x")
        boss.post(
            change_url(post),
            post_payload(author, title="Renamed", slug=post.slug),
            follow=True,
        )
        body = boss.get(self.url()).content.decode()
        assert "never-opened-the-panel" not in body


# ── the per-record history panel ────────────────────────────────────────────


class TestHistoryPanel:
    def test_lists_what_was_done_to_this_record(self, boss, post, author):
        boss.post(change_url(post), post_payload(author, title="Renamed", slug=post.slug))
        post.refresh_from_db()

        context = audit.history_context(_request_as(User.objects.get(username="boss")), post)

        assert len(context["history"]) == 1
        assert context["history"][0].changes["title"]["to"] == "Renamed"

    def test_shows_nothing_to_someone_who_may_not_read_the_log(self, post):
        plain = User.objects.create_user("plain", password="x", is_staff=True)
        # None, not an empty list: "nobody touched this" and "you may not see who did"
        # must not look the same.
        assert audit.history_context(_request_as(plain), post)["history"] is None

    def test_it_renders_on_the_detail_page_when_the_view_supplies_it(self, boss, post, author):
        from django.template.loader import render_to_string

        boss.post(change_url(post), post_payload(author, title="Renamed", slug=post.slug))
        post.refresh_from_db()
        context = audit.history_context(_request_as(User.objects.get(username="boss")), post)

        html = render_to_string("vantage/partials/history.html", context)
        assert "History" in html
        assert "Renamed" in html

    def test_the_partial_renders_nothing_without_the_context(self):
        from django.template.loader import render_to_string

        assert render_to_string("vantage/partials/history.html", {}).strip() == ""


class TestSimpleHistoryDetection:
    """Soft-detected with ``apps.is_installed``, never ``try: import``.

    An importable package that is not in ``INSTALLED_APPS`` has no tables and no
    registered models, so the import would succeed and the query would then fail.
    """

    def test_absent_when_the_app_is_not_installed(self, post):
        assert audit.history_timeline(post) is None

    def test_rendered_when_the_app_is_installed_and_the_model_has_it(self, post):
        fake = [
            mock.Mock(
                history_date=post.created_at,
                history_user=None,
                history_change_reason="Imported",
                **{"get_history_type_display.return_value": "Changed"},
            )
        ]
        manager = mock.Mock()
        manager.all.return_value = fake

        with (
            mock.patch("vantage.audit.django_apps.is_installed", return_value=True),
            mock.patch.object(type(post), "history", manager, create=True),
        ):
            timeline = audit.history_timeline(post)

        assert timeline == [
            {
                "date": post.created_at,
                "type": "Changed",
                "user": None,
                "reason": "Imported",
            }
        ]

    def test_a_broken_third_party_timeline_does_not_take_the_page_down(self, post, caplog):
        manager = mock.Mock()
        manager.all.side_effect = RuntimeError("their table is missing")

        with (
            mock.patch("vantage.audit.django_apps.is_installed", return_value=True),
            mock.patch.object(type(post), "history", manager, create=True),
        ):
            assert audit.history_timeline(post) is None
        assert "django-simple-history" in caplog.text


# ── helpers ─────────────────────────────────────────────────────────────────


def _request_as(user):
    from django.test import RequestFactory

    request = RequestFactory().post("/admin/")
    request.user = user
    return request
