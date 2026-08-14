"""Duplicating a record, acting on one row, and editing many at once.

The three features share a test module because they share a rule: none of them may do
anything a person did not ask for. A copy must not silently share a file, a row action
must not run because something followed a link, and a bulk edit must not write a field
nobody ticked or overwrite a colleague's change without saying so. Every test below is
one of those sentences.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from tests.testapp.models import Archive, Author, Category, Comment, Post, Tag
from tests.testapp.panel import ArchivePanel
from vantage import records
from vantage.panels import ModelPanel

ARCHIVE_LIST = "vantage:testapp-archive-list"
ARCHIVE_ACTION = "vantage:testapp-archive-row-action"
POST_BULK = "vantage:testapp-post-bulk-edit"
POST_DUPLICATE = "vantage:testapp-post-duplicate"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada Lovelace", email="ada@example.com")


@pytest.fixture
def post(db, author):
    made = Post.objects.create(
        title="Analytical Engine",
        slug="analytical-engine",
        body="On the engine.",
        summary="A summary.",
        author=author,
        status=Post.Status.PUBLISHED,
        view_count=12,
    )
    made.tags.set([Tag.objects.create(name="history"), Tag.objects.create(name="maths")])
    return made


@pytest.fixture
def posts(db, author):
    return [
        Post.objects.create(title=f"P{i}", slug=f"p{i}", body="b", author=author)
        for i in range(5)
    ]


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


@pytest.fixture
def archives(db):
    """Five soft-deleted archives — the state the restore action is about."""
    now = timezone.now()
    return [Archive.objects.create(title=f"A{i}", deleted_at=now) for i in range(5)]


def clerk(client, db, *codenames):
    """A staff user holding exactly the named permissions and nothing else."""
    user = User.objects.create_user("clerk", "clerk@example.com", "x", is_staff=True)
    for codename in codenames:
        app_label, name = codename.split(".")
        user.user_permissions.add(
            Permission.objects.get(codename=name, content_type__app_label=app_label)
        )
    client.force_login(user)
    return client


def panel_for(model):
    from vantage import site

    return site.get_panel(model)


# ── duplicate: the plan ─────────────────────────────────────────────────────


class TestDuplicatePlan:
    def test_ordinary_values_come_across_unchanged(self, post):
        plan = records.duplicate_plan(panel_for(Post), post)
        assert plan.initial["body"] == "On the engine."
        assert plan.initial["view_count"] == 12
        assert plan.initial["status"] == Post.Status.PUBLISHED

    def test_a_unique_text_field_becomes_a_copy_of_it(self, post):
        plan = records.duplicate_plan(panel_for(Post), post, ["title", "slug"])
        # ``title`` is not unique on Post, so it is carried verbatim; the slug is the
        # one that has to move.
        assert plan.initial["slug"] == "analytical-engine-copy"

    def test_a_taken_candidate_is_stepped_over(self, post):
        Post.objects.create(
            title="x", slug="analytical-engine-copy", body="b", author=post.author
        )
        plan = records.duplicate_plan(panel_for(Post), post, ["slug"])
        assert plan.initial["slug"] == "analytical-engine-copy-2"

    def test_the_change_is_reported_rather_than_made_quietly(self, post):
        plan = records.duplicate_plan(panel_for(Post), post, ["slug"])
        assert any("unique" in note for note in plan.notes)

    def test_a_unique_email_is_cleared_not_prefixed(self, author):
        """"Copy of ada@example.com" is not an address, and would fail on save."""
        plan = records.duplicate_plan(panel_for(Author), author, ["email"])
        assert plan.initial["email"] is None
        assert any("unique" in note.lower() for note in plan.notes)

    def test_many_to_many_links_are_carried_and_counted(self, post):
        plan = records.duplicate_plan(panel_for(Post), post, ["tags"])
        assert sorted(plan.initial["tags"]) == sorted(
            post.tags.values_list("pk", flat=True)
        )
        assert any("2 links copied" in note for note in plan.notes)

    def test_a_copy_of_an_archived_record_is_a_live_one(self, db):
        archived = Archive.objects.create(title="Gone", deleted_at=timezone.now())
        plan = records.duplicate_plan(panel_for(Archive), archived)
        assert plan.initial["deleted_at"] is None

    def test_a_shared_file_is_declared_rather_than_hidden(self, db):
        writer = Author.objects.create(
            name="Grace", email="grace@example.com", avatar="avatars/grace.png"
        )
        plan = records.duplicate_plan(panel_for(Author), writer, ["avatar"])
        assert any("same file" in note for note in plan.notes)
        assert any("avatars/grace.png" in note for note in plan.notes)

    def test_a_record_with_no_file_says_nothing_about_files(self, author):
        plan = records.duplicate_plan(panel_for(Author), author, ["avatar"])
        assert plan.notes == ()

    def test_the_plan_never_offers_a_field_the_form_does_not_render(self, post):
        plan = records.duplicate_plan(panel_for(Post), post, ["title"])
        assert set(plan.initial) == {"title"}

    def test_reverse_children_are_offered_with_a_live_count(self, post):
        Comment.objects.create(post=post, author_name="Bob", body="hi")
        Comment.objects.create(post=post, author_name="Cai", body="yo")
        plan = records.duplicate_plan(panel_for(Post), post)
        groups = {group.accessor: group for group in plan.children}
        assert groups["comments"].count == 2
        assert groups["comments"].field_name == "post"

    def test_a_record_with_no_children_offers_none(self, post):
        assert records.duplicate_plan(panel_for(Post), post).children == ()


class TestCopyChildren:
    def test_children_are_recreated_against_the_copy(self, rf, post, author):
        Comment.objects.create(post=post, author_name="Bob", body="hi")
        copy = Post.objects.create(title="Copy", slug="copy", body="b", author=author)

        groups = records.duplicate_plan(panel_for(Post), post).children
        made = records.copy_children(rf.get("/"), post, copy, groups)

        assert made == {"comments": 1}
        assert copy.comments.count() == 1
        # The original keeps its own: a copy is not a move.
        assert post.comments.count() == 1

    def test_nothing_is_copied_when_nothing_was_chosen(self, rf, post, author):
        Comment.objects.create(post=post, author_name="Bob", body="hi")
        copy = Post.objects.create(title="Copy", slug="copy", body="b", author=author)
        assert records.copy_children(rf.get("/"), post, copy, []) == {}
        assert copy.comments.count() == 0


# ── duplicate: the page ─────────────────────────────────────────────────────


class TestDuplicateView:
    def test_the_form_arrives_filled_in(self, boss, post):
        page = boss.get(reverse(POST_DUPLICATE, args=[post.pk]))
        assert page.status_code == 200
        assert page.context["form"].initial["body"] == "On the engine."

    def test_nothing_is_created_by_opening_the_page(self, boss, post):
        boss.get(reverse(POST_DUPLICATE, args=[post.pk]))
        assert Post.objects.count() == 1

    def test_the_notes_are_shown_on_the_page(self, boss, post):
        body = boss.get(reverse(POST_DUPLICATE, args=[post.pk])).content.decode()
        assert "This is a copy of" in body
        assert "to keep it unique" in body

    def test_saving_creates_a_second_record(self, boss, post):
        boss.post(
            reverse(POST_DUPLICATE, args=[post.pk]),
            {
                "title": "Analytical Engine (copy)",
                "slug": "analytical-engine-copy",
                "body": "On the engine.",
                "summary": "",
                "status": Post.Status.DRAFT,
                "author": post.author_id,
                "view_count": 0,
                "metadata": "{}",
            },
        )
        assert Post.objects.count() == 2
        assert Post.objects.filter(slug="analytical-engine-copy").exists()
        # The source is untouched.
        post.refresh_from_db()
        assert post.slug == "analytical-engine"

    def test_children_are_copied_only_when_ticked(self, boss, post):
        Comment.objects.create(post=post, author_name="Bob", body="hi")
        boss.post(
            reverse(POST_DUPLICATE, args=[post.pk]),
            {
                "title": "Copy",
                "slug": "copy-one",
                "body": "b",
                "summary": "",
                "status": Post.Status.DRAFT,
                "author": post.author_id,
                "view_count": 0,
                "metadata": "{}",
                "_copy_children": ["comments"],
            },
        )
        assert Post.objects.get(slug="copy-one").comments.count() == 1

    def test_children_are_left_behind_by_default(self, boss, post):
        Comment.objects.create(post=post, author_name="Bob", body="hi")
        boss.post(
            reverse(POST_DUPLICATE, args=[post.pk]),
            {
                "title": "Copy",
                "slug": "copy-two",
                "body": "b",
                "summary": "",
                "status": Post.Status.DRAFT,
                "author": post.author_id,
                "view_count": 0,
                "metadata": "{}",
            },
        )
        assert Post.objects.get(slug="copy-two").comments.count() == 0

    def test_duplicating_needs_the_add_permission_not_change(self, client, db, post):
        """Someone who may edit but not create must not create one by copying."""
        page = clerk(client, db, "testapp.view_post", "testapp.change_post").get(
            reverse(POST_DUPLICATE, args=[post.pk])
        )
        assert page.status_code == 403

    def test_a_missing_source_is_a_404(self, boss, db):
        assert boss.get(reverse(POST_DUPLICATE, args=[9999])).status_code == 404


# ── row actions: resolution ─────────────────────────────────────────────────


class TestResolveRowActions:
    def test_builtins_resolve_by_name(self, rf, db):
        request = rf.get("/")
        request.user = User.objects.create_superuser("su", "su@example.com", "x")
        resolved = records.resolve_row_actions(ArchivePanel(Archive), request)
        assert [spec.name for spec, _ in resolved] == [
            "view",
            "edit",
            "duplicate",
            "restore",
            "purge",
            "delete",
        ]

    def test_an_action_is_read_off_its_own_decorator(self, rf, db):
        request = rf.get("/")
        request.user = User.objects.create_superuser("su", "su@example.com", "x")
        specs = {
            spec.name: spec
            for spec, _ in records.resolve_row_actions(ArchivePanel(Archive), request)
        }
        # The five facts @action already carries, not restated anywhere.
        assert specs["purge"].label == "Purge"
        assert specs["purge"].destructive is True
        assert specs["purge"].permission == "delete"

    def test_a_panel_that_declares_none_gets_none(self, rf, db):
        request = rf.get("/")
        request.user = User.objects.create_superuser("su", "su@example.com", "x")
        assert records.resolve_row_actions(ModelPanel(Post), request) == []

    def test_an_unknown_name_is_dropped_rather_than_raising(self, rf, db):
        class Odd(ModelPanel):
            row_actions = ["view", "nonsense"]

        request = rf.get("/")
        request.user = User.objects.create_superuser("su", "su@example.com", "x")
        resolved = records.resolve_row_actions(Odd(Post), request)
        assert [spec.name for spec, _ in resolved] == ["view"]

    def test_what_the_user_may_not_do_is_not_offered(self, rf, client, db):
        user = User.objects.create_user("c", "c@example.com", "x", is_staff=True)
        user.user_permissions.add(
            Permission.objects.get(
                codename="view_archive", content_type__app_label="testapp"
            )
        )
        request = rf.get("/")
        request.user = User.objects.get(pk=user.pk)  # a fresh permission cache
        names = [
            spec.name
            for spec, _ in records.resolve_row_actions(ArchivePanel(Archive), request)
        ]
        assert names == ["view"]
        assert "purge" not in names and "delete" not in names


class TestPermissionsAreCheckedOncePerPage:
    """The requirement the module's own docstring states, asserted rather than claimed."""

    def test_ten_rows_cost_one_round_of_checks(self, rf, db, monkeypatch):
        from vantage import permissions

        calls = []
        original = permissions.PanelPermissions.has_model_perm

        def counting(self, request, model, action):
            calls.append((model.__name__, action))
            return original(self, request, model, action)

        monkeypatch.setattr(permissions.PanelPermissions, "has_model_perm", counting)

        request = rf.get("/")
        request.user = User.objects.create_superuser("su", "su@example.com", "x")
        panel = ArchivePanel(Archive)
        rows = [Archive.objects.create(title=f"A{i}") for i in range(10)]

        for row in rows:
            records.row_actions_for(panel, request, row)

        # Six declared row actions, five distinct permission verbs among them, and one
        # check each — for the whole page, not for each of the ten rows.
        assert len(calls) == 6, calls

    def test_a_second_request_is_not_served_a_stale_answer(self, rf, db, monkeypatch):
        """The memo is per request. A module-level cache would outlive a revoked grant."""
        request_a = rf.get("/")
        request_a.user = User.objects.create_superuser("su", "su@example.com", "x")
        panel = ArchivePanel(Archive)
        assert records.resolve_row_actions(panel, request_a)

        narrow = User.objects.create_user("n", "n@example.com", "x", is_staff=True)
        request_b = rf.get("/")
        request_b.user = narrow
        assert records.resolve_row_actions(panel, request_b) == []


class TestRowActionUrls:
    def test_a_builtin_is_a_link_to_the_page_that_already_exists(self, rf, db, post):
        request = rf.get("/")
        request.user = User.objects.create_superuser("su", "su@example.com", "x")

        class Linked(ModelPanel):
            row_actions = ["view", "duplicate"]

        buttons = {b.name: b for b in records.row_actions_for(Linked(Post), request, post)}
        assert buttons["view"].is_link
        assert buttons["view"].url.endswith(f"/{post.pk}/")
        assert buttons["duplicate"].url.endswith(f"/{post.pk}/duplicate/")
        assert buttons["duplicate"].post_url is None

    def test_an_action_posts_and_never_links(self, rf, db, archives):
        request = rf.get("/")
        request.user = User.objects.create_superuser("su", "su@example.com", "x")
        buttons = {
            b.name: b
            for b in records.row_actions_for(ArchivePanel(Archive), request, archives[0])
        }
        assert buttons["purge"].url is None
        assert buttons["purge"].post_url.endswith(f"/{archives[0].pk}/action/")
        assert buttons["purge"].is_link is False

    def test_the_pages_filters_ride_along_so_the_action_comes_back_to_them(
        self, rf, db, archives
    ):
        request = rf.get("/?trash=1&page=2")
        request.user = User.objects.create_superuser("su", "su@example.com", "x")
        button = next(
            b
            for b in records.row_actions_for(ArchivePanel(Archive), request, archives[0])
            if b.name == "restore"
        )
        assert "trash=1" in button.post_url and "page=2" in button.post_url

    def test_a_row_with_no_actions_renders_no_cell_content(self, rf, db, post):
        request = rf.get("/")
        request.user = User.objects.create_superuser("su", "su@example.com", "x")
        assert records.row_actions_for(ModelPanel(Post), request, post) == []


class TestTheSlotSeam:
    def test_the_column_is_declared_without_a_context(self, db):
        """``slots.is_filled`` asks with nothing; the answer decides the whole column."""
        assert records.panel_slot(ArchivePanel(Archive), "row_actions") is True
        assert records.panel_slot(ModelPanel(Post), "row_actions") is False

    def test_the_panel_hook_is_wired_to_it(self, db):
        from vantage import slots

        assert slots.is_filled("row_actions", {"panel": ArchivePanel(Archive)}) is True
        assert slots.is_filled("row_actions", {"panel": ModelPanel(Post)}) is False

    def test_an_unrelated_slot_stays_empty(self, db):
        assert records.panel_slot(ArchivePanel(Archive), "detail_sidebar") is None

    def test_the_buttons_render_into_the_list_page(self, boss, archives):
        body = boss.get(reverse(ARCHIVE_LIST) + "?trash=1").content.decode()
        assert "v-row-actions" in body
        assert f"/{archives[0].pk}/action/" in body

    def test_a_destructive_button_is_a_button_and_not_a_link(self, boss, archives):
        body = boss.get(reverse(ARCHIVE_LIST) + "?trash=1").content.decode()
        # The purge control posts. A crawler following every href must not be able to
        # reach it, so its URL may never appear inside an anchor.
        assert 'name="action" value="purge"' in body
        assert f'href="/admin/testapp/archive/{archives[0].pk}/action/' not in body


# ── row actions: running one ────────────────────────────────────────────────


class TestRowActionView:
    def url(self, pk):
        return reverse(ARCHIVE_ACTION, args=[pk])

    def test_an_action_runs_against_exactly_one_row(self, boss, archives):
        boss.post(self.url(archives[0].pk) + "?trash=1", {"action": "restore"})
        archives[0].refresh_from_db()
        assert archives[0].deleted_at is None
        # Every other row is untouched, including the ones a stale selection named.
        for other in archives[1:]:
            other.refresh_from_db()
            assert other.deleted_at is not None

    def test_a_stray_selection_in_the_body_is_ignored(self, boss, archives):
        """The row is named by the URL. The bulk form's checkboxes ride along; they
        must not widen the scope of a single-row action."""
        boss.post(
            self.url(archives[0].pk) + "?trash=1",
            {
                "action": "restore",
                "selected": [str(a.pk) for a in archives[1:]],
                "select_all": "1",
            },
        )
        for other in archives[1:]:
            other.refresh_from_db()
            assert other.deleted_at is not None

    def test_a_get_never_runs_anything(self, boss, archives):
        response = boss.get(self.url(archives[0].pk) + "?trash=1&action=restore")
        assert response.status_code == 302
        archives[0].refresh_from_db()
        assert archives[0].deleted_at is not None

    def test_a_destructive_action_confirms_first(self, boss, archives):
        response = boss.post(self.url(archives[0].pk) + "?trash=1", {"action": "purge"})
        assert response.status_code == 200
        assert Archive.objects.count() == 5
        assert response.context["confirming"].name == "purge"

    def test_the_confirmation_is_a_csrf_protected_post(self, boss, archives):
        body = boss.post(
            self.url(archives[0].pk) + "?trash=1", {"action": "purge"}
        ).content.decode()
        assert 'method="post"' in body
        assert "csrfmiddlewaretoken" in body
        assert 'name="confirmed" value="1"' in body

    def test_a_confirmed_destructive_action_runs(self, boss, archives):
        boss.post(
            self.url(archives[0].pk) + "?trash=1",
            {"action": "purge", "confirmed": "1"},
        )
        assert Archive.objects.count() == 4
        assert not Archive.objects.filter(pk=archives[0].pk).exists()

    def test_an_action_the_panel_does_not_offer_as_a_row_action_is_refused(
        self, boss, archives, monkeypatch
    ):
        monkeypatch.setattr(ArchivePanel, "row_actions", ["view"])
        response = boss.post(self.url(archives[0].pk) + "?trash=1", {"action": "restore"})
        assert response.status_code == 403
        archives[0].refresh_from_db()
        assert archives[0].deleted_at is not None

    def test_an_unknown_action_is_refused(self, boss, archives):
        assert (
            boss.post(self.url(archives[0].pk), {"action": "nonsense"}).status_code == 403
        )

    def test_a_user_without_the_actions_permission_is_refused(self, client, db, archives):
        signed_in = clerk(client, db, "testapp.view_archive", "testapp.change_archive")
        response = signed_in.post(
            self.url(archives[0].pk) + "?trash=1",
            {"action": "purge", "confirmed": "1"},
        )
        assert response.status_code == 403
        assert Archive.objects.count() == 5

    def test_an_object_level_refusal_is_honoured_on_the_one_row_it_is_about(
        self, boss, archives, monkeypatch
    ):
        """Model-level yes, object-level no. The check that *can* differ per row runs
        exactly here, where N is one."""
        monkeypatch.setattr(
            ArchivePanel,
            "has_delete_permission",
            lambda self, request, obj=None: obj is None,
            raising=False,
        )
        response = boss.post(
            self.url(archives[0].pk) + "?trash=1",
            {"action": "purge", "confirmed": "1"},
        )
        assert response.status_code == 403
        assert Archive.objects.count() == 5

    def test_a_row_that_is_gone_is_a_404(self, boss, archives):
        assert boss.post(self.url(99999), {"action": "restore"}).status_code == 404

    def test_the_run_is_audited_once(self, boss, archives):
        from vantage.models import VantageLogEntry

        boss.post(self.url(archives[0].pk) + "?trash=1", {"action": "restore"})
        entries = VantageLogEntry.objects.filter(action_name="restore")
        assert entries.count() == 1
        assert entries.first().object_repr == "1 record"


# ── bulk edit: which fields ─────────────────────────────────────────────────


class TestBulkEditFields:
    def test_a_unique_field_is_never_offered(self, db):
        """One value across N rows violates the constraint by definition."""
        assert "slug" not in records.bulk_edit_fields(panel_for(Post))

    def test_files_are_never_offered(self, db):
        assert "avatar" not in records.bulk_edit_fields(panel_for(Author))

    def test_ordinary_fields_are(self, db):
        offered = records.bulk_edit_fields(panel_for(Post))
        assert "status" in offered and "featured" in offered and "category" in offered

    def test_readonly_is_respected(self, db):
        class Locked(ModelPanel):
            readonly_fields = ["status"]

        assert "status" not in records.bulk_edit_fields(Locked(Post))

    def test_excluded_is_respected(self, db):
        class Narrow(ModelPanel):
            exclude = ["status"]

        assert "status" not in records.bulk_edit_fields(Narrow(Post))


class TestTickedFields:
    def test_only_what_was_ticked_is_returned(self):
        post = {records.BULK_EDIT_ENABLE: ["status"]}
        assert records.ticked_fields(_Q(post), ["status", "featured"]) == ["status"]

    def test_a_name_that_is_not_on_offer_is_dropped(self):
        """A hand-written POST does not get to widen the form."""
        post = {records.BULK_EDIT_ENABLE: ["slug", "status"]}
        assert records.ticked_fields(_Q(post), ["status"]) == ["status"]

    def test_the_order_of_the_offer_wins_over_the_order_of_the_post(self):
        post = {records.BULK_EDIT_ENABLE: ["featured", "status"]}
        assert records.ticked_fields(_Q(post), ["status", "featured"]) == [
            "status",
            "featured",
        ]


class _Q:
    """The smallest thing that answers ``getlist`` the way a QueryDict does."""

    def __init__(self, data):
        self._data = data

    def getlist(self, key):
        return list(self._data.get(key, ()))


# ── bulk edit: how it writes ────────────────────────────────────────────────


class TestBulkEditStrategy:
    def test_a_plain_column_is_one_update(self, db):
        assert records.bulk_edit_strategy(panel_for(Post), ["status"]) == ("sql", "")

    def test_a_many_to_many_forces_a_save_per_row(self, db):
        strategy, reason = records.bulk_edit_strategy(panel_for(Post), ["tags"])
        assert strategy == "instances"
        assert "many-to-many" in reason

    def test_a_save_model_hook_forces_a_save_per_row(self, db):
        class Hooked(ModelPanel):
            def save_model(self, request, obj, form, change):
                obj.save()

        strategy, reason = records.bulk_edit_strategy(Hooked(Post), ["status"])
        assert strategy == "instances"
        assert "save_model" in reason

    def test_an_overridden_save_forces_a_save_per_row(self, db, monkeypatch):
        def custom(self, *args, **kwargs):
            return models.Model.save(self, *args, **kwargs)

        monkeypatch.setattr(Post, "save", custom)
        strategy, reason = records.bulk_edit_strategy(panel_for(Post), ["status"])
        assert strategy == "instances"
        assert "overrides save()" in reason

    def test_a_connected_receiver_forces_a_save_per_row(self, db):
        from django.db.models.signals import post_save

        def receiver(**kwargs):
            pass

        post_save.connect(receiver, sender=Post)
        try:
            strategy, reason = records.bulk_edit_strategy(panel_for(Post), ["status"])
        finally:
            post_save.disconnect(receiver, sender=Post)
        assert strategy == "instances"
        assert "receiver" in reason


def bulk_form(panel, names, values):
    data = {f"{records.BULK_EDIT_PREFIX}-{k}": v for k, v in values.items()}
    form = records.bulk_edit_form_class(panel, names)(
        data=data, prefix=records.BULK_EDIT_PREFIX
    )
    assert form.is_valid(), form.errors
    return form


class TestRunBulkEdit:
    def test_the_fast_path_is_a_single_update(self, rf, posts):
        panel = panel_for(Post)
        form = bulk_form(panel, ["status"], {"status": "published"})
        from django.db import connection

        with CaptureQueriesContext(connection) as captured:
            outcome = records.run_bulk_edit(
                rf.post("/"), panel, Post.objects.all(), form, ["status"]
            )
        updates = [q for q in captured.captured_queries if q["sql"].startswith("UPDATE")]
        assert len(updates) == 1
        assert outcome.updated == 5
        assert Post.objects.filter(status="published").count() == 5

    def test_it_says_which_path_it_took(self, rf, posts):
        panel = panel_for(Post)
        form = bulk_form(panel, ["status"], {"status": "published"})
        outcome = records.run_bulk_edit(
            rf.post("/"), panel, Post.objects.all(), form, ["status"]
        )
        assert outcome.strategy == "sql"
        assert "in one query" in outcome.message

    def test_the_slow_path_says_why_it_was_slow(self, rf, posts):
        panel = panel_for(Post)
        tag = Tag.objects.create(name="ops")
        form = bulk_form(panel, ["tags"], {"tags": [str(tag.pk)]})
        outcome = records.run_bulk_edit(
            rf.post("/"), panel, Post.objects.all(), form, ["tags"]
        )
        assert outcome.strategy == "instances"
        assert "many-to-many" in outcome.message
        assert Post.objects.first().tags.count() == 1

    def test_the_slow_path_refuses_a_selection_it_cannot_finish(self, rf, posts, monkeypatch):
        monkeypatch.setattr(records, "BULK_EDIT_INSTANCE_LIMIT", 2)
        panel = panel_for(Post)
        tag = Tag.objects.create(name="ops")
        form = bulk_form(panel, ["tags"], {"tags": [str(tag.pk)]})
        outcome = records.run_bulk_edit(
            rf.post("/"), panel, Post.objects.all(), form, ["tags"]
        )
        assert outcome.refused
        assert "too many" in outcome.refused
        assert Post.objects.first().tags.count() == 0

    def test_the_fast_path_has_no_such_ceiling(self, rf, posts, monkeypatch):
        monkeypatch.setattr(records, "BULK_EDIT_INSTANCE_LIMIT", 2)
        panel = panel_for(Post)
        form = bulk_form(panel, ["status"], {"status": "published"})
        outcome = records.run_bulk_edit(
            rf.post("/"), panel, Post.objects.all(), form, ["status"]
        )
        assert outcome.refused is None
        assert outcome.updated == 5


class TestBulkEditAndConcurrency:
    """A bulk edit must not silently stomp somebody else's change."""

    def test_a_row_that_moved_since_the_selection_is_left_alone(self, rf, posts):
        panel = panel_for(Post)
        stale = timezone.now() - timedelta(hours=1)
        form = bulk_form(panel, ["status"], {"status": "published"})
        outcome = records.run_bulk_edit(
            rf.post("/"), panel, Post.objects.all(), form, ["status"], since=stale
        )
        assert outcome.updated == 0
        assert outcome.skipped == 5
        assert Post.objects.filter(status="published").count() == 0

    def test_the_skip_is_reported_rather_than_hidden(self, rf, posts):
        panel = panel_for(Post)
        # Move one row into the future, leaving the other four selectable.
        Post.objects.filter(pk=posts[0].pk).update(
            updated_at=timezone.now() + timedelta(minutes=5)
        )
        form = bulk_form(panel, ["status"], {"status": "published"})
        outcome = records.run_bulk_edit(
            rf.post("/"),
            panel,
            Post.objects.all(),
            form,
            ["status"],
            since=timezone.now(),
        )
        assert outcome.updated == 4
        assert outcome.skipped == 1
        assert "changed by someone else" in outcome.message
        assert outcome.level == "warning"

    def test_the_version_column_is_advanced_by_the_write_itself(self, rf, posts):
        """``update()`` does not run ``pre_save``, so ``auto_now`` would otherwise stay
        put and every optimistic lock in flight would still look valid."""
        before = Post.objects.get(pk=posts[0].pk).updated_at
        panel = panel_for(Post)
        form = bulk_form(panel, ["status"], {"status": "published"})
        records.run_bulk_edit(rf.post("/"), panel, Post.objects.all(), form, ["status"])
        assert Post.objects.get(pk=posts[0].pk).updated_at > before

    def test_an_open_change_form_can_no_longer_win_the_race(self, rf, posts):
        """The point of advancing the column: a stale token must now be refused."""
        from vantage.concurrency import StaleWrite, claim, token_for

        victim = Post.objects.get(pk=posts[0].pk)
        stale_token = token_for(victim)

        panel = panel_for(Post)
        form = bulk_form(panel, ["status"], {"status": "published"})
        records.run_bulk_edit(rf.post("/"), panel, Post.objects.all(), form, ["status"])

        with pytest.raises(StaleWrite):
            claim(victim, stale_token)

    def test_the_slow_path_takes_each_rows_lock_before_saving_it(self, rf, posts):
        panel = panel_for(Post)
        tag = Tag.objects.create(name="ops")
        form = bulk_form(panel, ["tags"], {"tags": [str(tag.pk)]})
        before = {p.pk: p.updated_at for p in Post.objects.all()}
        records.run_bulk_edit(rf.post("/"), panel, Post.objects.all(), form, ["tags"])
        for updated in Post.objects.all():
            assert updated.updated_at > before[updated.pk]


# ── bulk edit: the page ─────────────────────────────────────────────────────


class TestBulkEditView:
    def url(self, query=""):
        return reverse(POST_BULK) + query

    def test_the_picker_lists_the_fields_on_offer(self, boss, posts):
        page = boss.post(self.url(), {"selected": [str(posts[0].pk)]})
        assert page.status_code == 200
        assert page.context["count"] == 1
        names = [entry["name"] for entry in page.context["entries"]]
        assert "status" in names and "slug" not in names

    def test_the_picker_writes_nothing(self, boss, posts):
        boss.post(self.url(), {"selected": [str(p.pk) for p in posts]})
        assert Post.objects.filter(status="published").count() == 0

    def test_applying_writes_the_ticked_field(self, boss, posts):
        response = boss.post(
            self.url(),
            {
                "selected": [str(p.pk) for p in posts[:3]],
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "published",
            },
        )
        assert response.status_code == 302
        assert Post.objects.filter(status="published").count() == 3

    def test_an_unticked_field_is_not_written_even_when_its_value_is_posted(
        self, boss, posts
    ):
        """The security property: an unticked field is absent from the form the server
        builds, so a hand-written POST carrying its value writes nothing."""
        boss.post(
            self.url(),
            {
                "selected": [str(p.pk) for p in posts],
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "published",
                f"{records.BULK_EDIT_PREFIX}-body": "OVERWRITTEN",
                f"{records.BULK_EDIT_PREFIX}-summary": "OVERWRITTEN",
            },
        )
        assert Post.objects.filter(body="OVERWRITTEN").count() == 0
        assert Post.objects.filter(status="published").count() == 5

    def test_a_field_that_is_never_on_offer_cannot_be_ticked_into_the_form(
        self, boss, posts
    ):
        boss.post(
            self.url(),
            {
                "selected": [str(posts[0].pk)],
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["slug"],
                f"{records.BULK_EDIT_PREFIX}-slug": "hijacked",
            },
        )
        assert not Post.objects.filter(slug="hijacked").exists()

    def test_select_all_with_exclusions_reaches_everything_but_the_exclusions(
        self, boss, posts
    ):
        """The selection model: a predicate plus an exclusion set, never a list of ids."""
        response = boss.post(
            self.url(),
            {
                "select_all": "1",
                "excluded": [str(posts[0].pk), str(posts[1].pk)],
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "published",
            },
        )
        assert response.status_code == 302
        assert Post.objects.filter(status="published").count() == 3
        for spared in posts[:2]:
            spared.refresh_from_db()
            assert spared.status == Post.Status.DRAFT

    def test_select_all_stays_inside_the_pages_own_filters(self, boss, posts, author):
        """"All matching" is the predicate the page was showing, not the whole table."""
        Post.objects.filter(pk=posts[0].pk).update(status=Post.Status.REVIEW)
        response = boss.post(
            self.url("?status=review"),
            {
                "select_all": "1",
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["featured"],
                f"{records.BULK_EDIT_PREFIX}-featured": "on",
            },
        )
        assert response.status_code == 302
        assert Post.objects.filter(featured=True).count() == 1

    def test_select_all_never_materialises_ids(self, boss, posts):
        """The picker echoes the selection back; a select-all must not grow with N."""
        page = boss.post(self.url(), {"select_all": "1"})
        body = page.content.decode()
        assert 'name="select_all" value="1"' in body
        assert 'name="selected"' not in body

    def test_an_empty_selection_does_nothing(self, boss, posts):
        response = boss.post(self.url(), {})
        assert response.status_code == 302
        assert Post.objects.filter(status="published").count() == 0

    def test_a_get_is_bounced_back_to_the_list(self, boss, posts):
        response = boss.get(self.url())
        assert response.status_code == 302
        assert response["Location"].endswith("/admin/testapp/post/")

    def test_applying_with_nothing_ticked_asks_rather_than_writes(self, boss, posts):
        response = boss.post(
            self.url(), {"selected": [str(posts[0].pk)], "_bulk_apply": "1"}
        )
        assert response.status_code == 200
        assert Post.objects.filter(status="published").count() == 0

    def test_an_invalid_value_re_renders_with_the_error(self, boss, posts):
        response = boss.post(
            self.url(),
            {
                "selected": [str(posts[0].pk)],
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["rating"],
                f"{records.BULK_EDIT_PREFIX}-rating": "not-a-number",
            },
        )
        assert response.status_code == 200
        assert response.context["form"].errors["rating"]

    def test_an_untouched_required_field_is_not_reported_as_an_error(self, boss, posts):
        """A field nobody ticked is not part of this edit, so "required" is not a
        complaint about anything the person did."""
        response = boss.post(
            self.url(),
            {
                "selected": [str(posts[0].pk)],
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["rating"],
                f"{records.BULK_EDIT_PREFIX}-rating": "not-a-number",
            },
        )
        assert set(response.context["form"].errors) == {"rating"}

    def test_the_scope_is_stated_before_anything_is_written(self, boss, posts):
        page = boss.post(self.url(), {"select_all": "1", "excluded": [str(posts[0].pk)]})
        assert "4 matching" in page.context["scope"]
        assert "1 excluded" in page.context["scope"]

    def test_the_outcome_is_reported_to_the_user(self, boss, posts):
        response = boss.post(
            self.url(),
            {
                "select_all": "1",
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "published",
            },
            follow=True,
        )
        text = " ".join(str(m) for m in response.context["messages"])
        assert "Updated 5 records in one query" in text

    def test_change_permission_is_required(self, client, db, posts):
        response = clerk(client, db, "testapp.view_post").post(
            reverse(POST_BULK),
            {
                "selected": [str(posts[0].pk)],
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "published",
            },
        )
        assert response.status_code == 403
        assert Post.objects.filter(status="published").count() == 0

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self, client, db, posts):
        response = client.post(reverse(POST_BULK), {"selected": ["1"]})
        assert response.status_code == 302
        assert "login" in response["Location"]
        assert Post.objects.filter(status="published").count() == 0

    def test_a_relation_can_be_set_across_the_selection(self, boss, posts, db):
        category = Category.objects.create(name="Ops", slug="ops")
        boss.post(
            self.url(),
            {
                "select_all": "1",
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["category"],
                f"{records.BULK_EDIT_PREFIX}-category": str(category.pk),
            },
        )
        assert Post.objects.filter(category=category).count() == 5

    def test_the_button_appears_on_the_list_for_someone_who_may_change(self, boss, posts):
        body = boss.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "Bulk edit" in body
        assert "/admin/testapp/post/_bulk-edit/" in body

    def test_the_button_is_absent_for_someone_who_may_not(self, client, db, posts):
        body = (
            clerk(client, db, "testapp.view_post")
            .get(reverse("vantage:testapp-post-list"))
            .content.decode()
        )
        assert "_bulk-edit" not in body


# ── the audit trail ─────────────────────────────────────────────────────────


class TestBulkEditIsAudited:
    def test_one_entry_per_run_not_one_per_row(self, boss, posts):
        from vantage.models import VantageLogEntry

        boss.post(
            reverse(POST_BULK),
            {
                "select_all": "1",
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "published",
            },
        )
        entries = VantageLogEntry.objects.filter(action_name="bulk_edit")
        assert entries.count() == 1
        entry = entries.first()
        assert entry.object_repr == "5 records"
        assert entry.changes["status"]["to"] == "published"

    def test_the_content_type_is_the_model_that_was_edited(self, boss, posts):
        from vantage.models import VantageLogEntry

        boss.post(
            reverse(POST_BULK),
            {
                "select_all": "1",
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "published",
            },
        )
        entry = VantageLogEntry.objects.get(action_name="bulk_edit")
        assert entry.content_type == ContentType.objects.get_for_model(Post)
