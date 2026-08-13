"""Inline cell editing: the allowlist, the staged changeset, and what it refuses.

Most of this file is about refusals, because that is where the feature's risk lives.
Filament's documentation says outright that its inline column *does not check model
policies before saving*; the tests named
``test_a_readonly_field_posted_by_hand_is_never_written`` and
``test_a_field_the_change_form_would_refuse_is_refused_here_too`` exist so that the same
sentence can never be written about this one. They post fields no rendered page ever
offered, and assert the database did not move.

The other half is the staging contract from §12 of the list-page decisions. A changeset
is one transaction: twelve edits reviewed together either all land or none do, because
"ten of your twelve edits were applied" is not a state anybody asked for and not one
they can see. ``TestAtomicity`` is that sentence taken apart.

The urlconf at the bottom of this preamble mounts the inline routes ahead of the panel
routes, exactly as :mod:`vantage.urls` must: the detail route's ``<path:pk>`` converter
is greedy and would otherwise swallow ``_inline`` as a primary key. It tolerates the
routes already being wired, so the file passes before and after that change lands.
"""

import json

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.test import override_settings
from django.test.utils import isolate_apps
from django.urls import path, reverse

from tests.testapp.models import Author, Post
from vantage import inline
from vantage.models import VantageLogEntry
from vantage.panels import ModelPanel
from vantage.permissions import PanelPermissions
from vantage.sites import site


def _vantage_patterns():
    from vantage import urls as vantage_urls

    built = vantage_urls.build(site)
    mounted = {getattr(pattern, "name", None) for pattern in built}
    # Idempotent: once vantage.urls mounts these itself, this file stops adding them
    # rather than registering every inline route twice.
    extra = [] if f"{site.get_panel(Post).slug}-inline" in mounted else inline.build_urls(site)
    return (extra + built, "vantage", "vantage")


urlpatterns = [path("admin/", _vantage_patterns())]

pytestmark = pytest.mark.urls("tests.test_inline")

LIST_URL = "vantage:testapp-post-list"
SAVE_URL = "vantage:testapp-post-inline"
VALIDATE_URL = "vantage:testapp-post-inline-validate"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("editor", "editor@example.com", "x")


@pytest.fixture
def viewer(db):
    """Staff, may look at posts, may not change one."""
    user = User.objects.create_user("looker", password="x", is_staff=True)
    user.user_permissions.add(
        Permission.objects.get(
            content_type=ContentType.objects.get_for_model(Post), codename="view_post"
        )
    )
    return User.objects.get(pk=user.pk)  # drop the cached permission set


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada Lovelace", email="ada@example.com")


@pytest.fixture
def post(db, author):
    return Post.objects.create(
        title="Analytical engines",
        slug="analytical-engines",
        body="Notes.",
        status=Post.Status.DRAFT,
        author=author,
        view_count=3,
    )


@pytest.fixture
def other(db, author):
    return Post.objects.create(
        title="Difference engines",
        slug="difference-engines",
        body="More notes.",
        status=Post.Status.DRAFT,
        author=author,
        view_count=7,
    )


@pytest.fixture
def panel(monkeypatch):
    """``Post``, with three of its existing columns declared inline-editable.

    Nothing else is configured: ``status``, ``featured`` and ``view_count`` are already
    in the inferred ``list_display``, which is the point — turning this on is one line.
    """
    resolved = site.get_panel(Post)
    monkeypatch.setattr(resolved, "list_editable", ["status", "featured", "view_count"])
    return resolved


@pytest.fixture
def signed_in(client, boss, panel):
    client.force_login(boss)
    return client


def token_of(obj):
    from vantage import concurrency

    return concurrency.token_for(obj)


def change(obj, **fields):
    return {"pk": obj.pk, "version": token_of(obj), "fields": fields}


def save(client, *changes):
    return client.patch(
        reverse(SAVE_URL),
        data=json.dumps({"changes": list(changes)}),
        content_type="application/json",
    )


def validate(client, obj, field, value):
    return client.post(
        reverse(VALIDATE_URL),
        data=json.dumps({"pk": obj.pk, "field": field, "value": value}),
        content_type="application/json",
    )


def body_of(response):
    return json.loads(response.content)


# ── the allowlist ───────────────────────────────────────────────────────────


class TestAllowlist:
    def test_nothing_is_editable_until_a_panel_says_so(self):
        assert inline.inline_fields(site.get_panel(Post)) == ()

    def test_declared_columns_become_editable(self, panel):
        assert [field.name for field in inline.inline_fields(panel)] == [
            "status",
            "featured",
            "view_count",
        ]

    def test_the_kind_travels_with_the_field(self, panel):
        kinds = {field.name: str(field.kind) for field in inline.inline_fields(panel)}
        assert kinds == {"status": "choice", "featured": "boolean", "view_count": "number"}

    def test_the_link_column_is_refused(self, monkeypatch):
        """A cell is a link or an input. ``panel_checks`` says so as E016."""
        resolved = site.get_panel(Post)
        monkeypatch.setattr(resolved, "list_editable", ["title", "status"])
        assert [field.name for field in inline.inline_fields(resolved)] == ["status"]

    def test_a_column_that_is_not_displayed_is_refused(self, monkeypatch):
        resolved = site.get_panel(Post)
        monkeypatch.setattr(resolved, "list_editable", ["rating"])
        assert "rating" not in site.get_panel(Post).get_list_display()
        assert inline.inline_fields(resolved) == ()

    def test_a_readonly_field_is_refused(self, monkeypatch, panel):
        monkeypatch.setattr(panel, "readonly_fields", ["status"])
        assert [field.name for field in inline.inline_fields(panel)] == [
            "featured",
            "view_count",
        ]

    def test_an_excluded_field_is_refused(self, monkeypatch, panel):
        monkeypatch.setattr(panel, "exclude", ["featured"])
        assert [field.name for field in inline.inline_fields(panel)] == [
            "status",
            "view_count",
        ]

    @pytest.mark.parametrize(
        ("name", "why"),
        [
            ("body", "long text needs room a cell does not have"),
            ("metadata", "JSON is not a one-line control"),
            ("author", "a relation needs a search picker first"),
            ("tags", "many-to-many is not a cell"),
            ("published_at", "a datetime is two controls pretending to be one"),
            ("summary", "a 300-character CharField is prose with a limit"),
            ("created_at", "auto_now_add is not writable at all"),
        ],
    )
    def test_kinds_a_cell_cannot_hold_are_refused(self, monkeypatch, name, why):
        resolved = site.get_panel(Post)
        monkeypatch.setattr(resolved, "list_editable", [name])
        monkeypatch.setattr(resolved, "list_display", ["title", name])
        assert inline.inline_fields(resolved) == (), why

    @isolate_apps("tests.testapp")
    def test_a_model_that_validates_across_its_fields_is_refused(self):
        """No single cell can satisfy a rule about two of them."""

        class CValidated(models.Model):
            name = models.CharField(max_length=50)
            nickname = models.CharField(max_length=50)

            class Meta:
                app_label = "testapp"

            def clean(self):  # pragma: no cover - never called; its existence is the point
                pass

        resolved = ModelPanel(CValidated, site)
        resolved.list_editable = ["name"]
        resolved.list_display = ["name", "nickname"]
        assert inline.inline_fields(resolved) == ()

    @isolate_apps("tests.testapp")
    def test_a_field_in_a_multi_column_unique_constraint_is_refused(self):
        """Django *skips* the check when the other column is absent from the form.

        So the write passes validation, reaches the database, and raises an
        IntegrityError the user cannot read — on an edit they were told was fine.
        """

        class CPairedTogether(models.Model):
            slot = models.CharField(max_length=20)
            day = models.CharField(max_length=20)
            note = models.CharField(max_length=20)

            class Meta:
                app_label = "testapp"
                unique_together = [("slot", "day")]

        resolved = ModelPanel(CPairedTogether, site)
        resolved.list_editable = ["slot", "day", "note"]
        resolved.list_display = ["slot", "day", "note"]
        assert [field.name for field in inline.inline_fields(resolved)] == ["note"]

    @isolate_apps("tests.testapp")
    def test_a_multi_column_unique_constraint_counts_too(self):
        class CPairedConstrained(models.Model):
            slot = models.CharField(max_length=20)
            day = models.CharField(max_length=20)
            note = models.CharField(max_length=20)

            class Meta:
                app_label = "testapp"
                constraints = [
                    models.UniqueConstraint(fields=["slot", "day"], name="c_slot_day")
                ]

        resolved = ModelPanel(CPairedConstrained, site)
        resolved.list_editable = ["slot", "note"]
        resolved.list_display = ["slot", "day", "note"]
        assert [field.name for field in inline.inline_fields(resolved)] == ["note"]

    @isolate_apps("tests.testapp")
    def test_a_single_column_unique_field_is_still_editable(self):
        """Django validates that one properly, because the form has everything it needs."""

        class CUniqueAlone(models.Model):
            name = models.CharField(max_length=20)
            code = models.CharField(max_length=20, unique=True)

            class Meta:
                app_label = "testapp"

        resolved = ModelPanel(CUniqueAlone, site)
        resolved.list_editable = ["code"]
        resolved.list_display = ["name", "code"]
        resolved.list_link = "name"
        assert [field.name for field in inline.inline_fields(resolved)] == ["code"]


# ── what the page renders ───────────────────────────────────────────────────


class TestRendering:
    def test_an_editable_column_renders_a_control(self, signed_in, post):
        page = signed_in.get(reverse(LIST_URL)).content.decode()
        assert 'data-inline-field="status"' in page
        assert 'data-inline-cell data-field="status"' in page

    def test_a_column_that_is_not_editable_renders_no_control(self, signed_in, post):
        page = signed_in.get(reverse(LIST_URL)).content.decode()
        assert 'data-inline-field="title"' not in page

    def test_the_row_carries_the_version_it_was_rendered_with(self, signed_in, post):
        page = signed_in.get(reverse(LIST_URL)).content.decode()
        assert f'data-version="{token_of(post)}"' in page

    def test_the_control_is_reparented_out_of_the_action_form(self, signed_in, post):
        """Otherwise Enter in a cell submits a bulk action, with every cell attached."""
        page = signed_in.get(reverse(LIST_URL)).content.decode()
        assert 'form="v-inline-form"' in page
        assert '<form id="v-inline-form"' in page

    def test_the_changeset_bar_and_the_conflict_dialog_are_present(self, signed_in, post):
        page = signed_in.get(reverse(LIST_URL)).content.decode()
        assert "data-inline-bar" in page
        assert "data-inline-conflict" in page

    def test_controls_stay_out_of_the_tab_order(self, signed_in, post):
        """One tab stop for the whole grid; the arrow keys reach the rest (APG)."""
        page = signed_in.get(reverse(LIST_URL)).content.decode()
        marker = 'data-inline-field="status"'
        control = page[page.index("<select", page.index(marker) - 400) : page.index(marker) + 40]
        assert 'tabindex="-1"' in control

    def test_a_panel_without_list_editable_renders_nothing_inline(self, client, boss, post):
        client.force_login(boss)
        page = client.get(reverse(LIST_URL)).content.decode()
        assert "data-inline-bar" not in page
        assert "vantage-inline.js" not in page

    def test_the_assets_load_only_where_they_are_used(self, signed_in, post):
        page = signed_in.get(reverse(LIST_URL)).content.decode()
        assert "vantage-inline.css" in page
        assert "vantage-inline.js" in page


# ── committing a changeset ──────────────────────────────────────────────────


class TestCommit:
    def test_one_cell_is_written(self, signed_in, post):
        response = save(signed_in, change(post, status="published"))
        assert response.status_code == 200
        assert Post.objects.get(pk=post.pk).status == "published"

    def test_several_cells_of_one_row_are_written_together(self, signed_in, post):
        save(signed_in, change(post, status="review", view_count=99))
        fresh = Post.objects.get(pk=post.pk)
        assert (fresh.status, fresh.view_count) == ("review", 99)

    def test_several_rows_are_written_together(self, signed_in, post, other):
        response = save(
            signed_in, change(post, view_count=10), change(other, view_count=20)
        )
        assert body_of(response)["saved"] == 2
        assert Post.objects.get(pk=post.pk).view_count == 10
        assert Post.objects.get(pk=other.pk).view_count == 20

    def test_the_response_reports_the_stored_value_not_the_typed_one(self, signed_in, post):
        """"3" and "003" are the same number; the cell has to end up showing the first."""
        response = save(signed_in, change(post, view_count="007"))
        assert body_of(response)["rows"][0]["values"]["view_count"] == "7"

    def test_a_false_boolean_is_not_inverted(self, signed_in, post):
        """The classic JSON-to-form bug: a checkbox reads *presence*, not value.

        Passing ``{"featured": false}`` straight into a form would arrive as a present
        key and be read as ``True`` — every unticked box silently ticking itself.
        """
        post.featured = True
        post.save()
        save(signed_in, change(Post.objects.get(pk=post.pk), featured=False))
        assert Post.objects.get(pk=post.pk).featured is False

    def test_a_true_boolean_still_lands(self, signed_in, post):
        save(signed_in, change(post, featured=True))
        assert Post.objects.get(pk=post.pk).featured is True

    def test_an_empty_changeset_is_a_no_op(self, signed_in, post):
        response = save(signed_in)
        assert response.status_code == 200
        assert body_of(response)["saved"] == 0

    def test_the_panel_save_hook_runs(self, signed_in, post, panel, monkeypatch):
        """Inline writes must be indistinguishable from the change form's, hook included."""
        seen = []
        monkeypatch.setattr(
            panel,
            "save_model",
            lambda request, obj, form, changed: seen.append(changed) or obj.save(),
            raising=False,
        )
        save(signed_in, change(post, view_count=5))
        assert seen == [True]


class TestAtomicity:
    def test_one_invalid_row_leaves_the_others_untouched(self, signed_in, post, other):
        response = save(
            signed_in, change(post, view_count=42), change(other, view_count="not a number")
        )
        assert response.status_code == 422
        assert Post.objects.get(pk=post.pk).view_count == 3
        assert Post.objects.get(pk=other.pk).view_count == 7

    def test_the_rejection_names_the_row_and_the_field(self, signed_in, post, other):
        response = save(
            signed_in, change(post, view_count=42), change(other, view_count="not a number")
        )
        errors = body_of(response)["errors"]
        assert errors[0]["pk"] == str(other.pk)
        assert "view_count" in errors[0]["fields"]
        assert errors[0]["fields"]["view_count"]  # a sentence, not a code

    def test_nothing_is_reported_as_saved(self, signed_in, post, other):
        response = save(
            signed_in, change(post, view_count=42), change(other, view_count="nope")
        )
        assert body_of(response)["saved"] == 0

    def test_two_bad_cells_come_back_in_one_answer(self, signed_in, post):
        """One round trip to fix a changeset, not one per mistake."""
        response = save(signed_in, change(post, view_count="x", status="not-a-status"))
        fields = body_of(response)["errors"][0]["fields"]
        assert set(fields) == {"view_count", "status"}


# ── the security boundary ───────────────────────────────────────────────────


class TestHandWrittenPayloads:
    def test_a_readonly_field_posted_by_hand_is_never_written(
        self, signed_in, post, panel, monkeypatch
    ):
        """The exact failure Filament documents in its own inline column.

        ``featured`` is declared in ``list_editable`` *and* in ``readonly_fields``, so
        no page ever rendered a control for it — and a POST that carries one anyway is
        dropped, not honoured.
        """
        monkeypatch.setattr(panel, "readonly_fields", ["featured"])
        save(signed_in, change(post, featured=True))
        assert Post.objects.get(pk=post.pk).featured is False

    def test_a_field_the_change_form_would_refuse_is_refused_here_too(self, signed_in, post):
        """``slug`` is writable on the change form but was never declared inline."""
        save(signed_in, change(post, slug="hand-written"))
        assert Post.objects.get(pk=post.pk).slug == "analytical-engines"

    def test_a_field_that_is_not_a_field_at_all_is_dropped(self, signed_in, post):
        response = save(signed_in, change(post, is_superuser=True))
        assert response.status_code == 200
        assert body_of(response)["saved"] == 0

    def test_a_dropped_field_does_not_drag_a_real_one_down_with_it(self, signed_in, post):
        save(signed_in, change(post, slug="hand-written", status="review"))
        fresh = Post.objects.get(pk=post.pk)
        assert (fresh.slug, fresh.status) == ("analytical-engines", "review")

    def test_the_refusal_does_not_name_the_editable_fields(self, signed_in, post):
        """Answering "``slug`` is not editable" answers a question only a prober asks."""
        response = save(signed_in, change(post, slug="hand-written"))
        assert "slug" not in response.content.decode()


class TestPermissions:
    def test_a_user_without_change_is_refused(self, client, viewer, post, panel):
        client.force_login(viewer)
        response = save(client, change(post, status="published"))
        assert response.status_code == 403
        assert Post.objects.get(pk=post.pk).status == "draft"

    def test_a_user_without_change_cannot_validate_either(self, client, viewer, post, panel):
        client.force_login(viewer)
        assert validate(client, post, "status", "x").status_code == 403

    def test_an_anonymous_request_is_sent_to_the_login_page(self, client, post, panel):
        assert save(client, change(post, status="published")).status_code == 302

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_read_only_mode_refuses_the_write(self, signed_in, post):
        """An operational choice about the deployment outranks the superuser bypass."""
        assert save(signed_in, change(post, status="published")).status_code == 403

    @override_settings(VANTAGE={"PERMISSIONS": "tests.test_inline.OnlyPublished"})
    def test_a_row_outside_your_scope_answers_404_not_403(self, signed_in, post):
        """403 would confirm the record exists and is merely off-limits.

        Same order as the change form: scope first with ``view``, then authorise the
        verb. A row you cannot see does not exist.
        """
        assert save(signed_in, change(post, status="published")).status_code == 404

    @override_settings(VANTAGE={"PERMISSIONS": "tests.test_inline.OnlyPublished"})
    def test_a_row_inside_your_scope_still_saves(self, signed_in, post):
        Post.objects.filter(pk=post.pk).update(status="published")
        visible = Post.objects.get(pk=post.pk)
        assert save(signed_in, change(visible, view_count=4)).status_code == 200

    def test_a_row_that_does_not_exist_answers_404(self, signed_in, post):
        response = save(signed_in, {"pk": 999999, "version": None, "fields": {"view_count": 1}})
        assert response.status_code == 404


class OnlyPublished(PanelPermissions):
    """Row scoping, so the 404-before-403 order has something to bite on."""

    def filter_queryset(self, request, queryset, action="view"):
        if queryset.model is Post:
            return queryset.filter(status="published")
        return queryset


class TestMalformedRequests:
    def test_a_body_that_is_not_json_is_a_400(self, signed_in, post):
        response = signed_in.patch(
            reverse(SAVE_URL), data="{oops", content_type="application/json"
        )
        assert response.status_code == 400

    def test_a_missing_changes_list_is_a_400(self, signed_in, post):
        response = signed_in.patch(
            reverse(SAVE_URL), data=json.dumps({}), content_type="application/json"
        )
        assert response.status_code == 400

    def test_an_oversized_batch_is_refused(self, signed_in, post):
        """An unbounded batch is an unbounded transaction on someone's production data."""
        changes = [change(post, view_count=1)] * (inline.MAX_ROWS_PER_SAVE + 1)
        assert save(signed_in, *changes).status_code == 400

    def test_the_write_endpoint_takes_only_patch(self, signed_in, post):
        assert signed_in.get(reverse(SAVE_URL)).status_code == 405
        assert signed_in.post(reverse(SAVE_URL)).status_code == 405

    def test_the_validate_endpoint_takes_only_post(self, signed_in, post):
        assert signed_in.get(reverse(VALIDATE_URL)).status_code == 405


# ── live validation ─────────────────────────────────────────────────────────


class TestLiveValidation:
    def test_it_reports_an_error_for_one_field(self, signed_in, post):
        body = body_of(validate(signed_in, post, "view_count", "not a number"))
        assert body["valid"] is False
        assert body["errors"]

    def test_it_saves_nothing(self, signed_in, post):
        validate(signed_in, post, "view_count", 4242)
        assert Post.objects.get(pk=post.pk).view_count == 3

    def test_a_good_value_comes_back_clean(self, signed_in, post):
        body = body_of(validate(signed_in, post, "status", "published"))
        assert body == {"pk": str(post.pk), "field": "status", "valid": True, "errors": []}

    def test_it_uses_the_model_form_rather_than_a_second_rulebook(self, signed_in, post):
        """A choice the model does not declare must fail here exactly as it would on save."""
        assert body_of(validate(signed_in, post, "status", "invented"))["valid"] is False

    def test_a_field_that_is_not_editable_is_refused(self, signed_in, post):
        assert validate(signed_in, post, "slug", "x").status_code == 400

    def test_a_field_that_does_not_exist_is_refused(self, signed_in, post):
        assert validate(signed_in, post, "is_superuser", True).status_code == 400


# ── the audit trail ─────────────────────────────────────────────────────────


class TestAudit:
    def test_an_inline_write_is_recorded(self, signed_in, post):
        before = VantageLogEntry.objects.count()
        save(signed_in, change(post, status="published"))
        assert VantageLogEntry.objects.count() == before + 1

    def test_the_entry_carries_the_diff(self, signed_in, post):
        save(signed_in, change(post, status="published"))
        entry = VantageLogEntry.objects.latest("pk")
        assert entry.changes["status"] == {"from": "draft", "to": "published"}

    def test_the_entry_is_scoped_to_the_record(self, signed_in, post):
        """Per record, not per changeset: it has to show up in *that* row's history."""
        save(signed_in, change(post, status="published"))
        entry = VantageLogEntry.objects.latest("pk")
        assert entry.object_id == str(post.pk)
        assert entry.action == VantageLogEntry.Action.UPDATE

    def test_a_refused_changeset_leaves_no_entry(self, signed_in, post, other):
        before = VantageLogEntry.objects.count()
        save(signed_in, change(post, view_count=1), change(other, view_count="nope"))
        assert VantageLogEntry.objects.count() == before

    def test_two_rows_produce_two_entries(self, signed_in, post, other):
        before = VantageLogEntry.objects.count()
        save(signed_in, change(post, view_count=1), change(other, view_count=2))
        assert VantageLogEntry.objects.count() == before + 2


# ── routing ─────────────────────────────────────────────────────────────────


class TestRoutes:
    def test_both_routes_exist_for_every_panel(self):
        assert reverse(SAVE_URL) == "/admin/testapp/post/_inline/"
        assert reverse(VALIDATE_URL) == "/admin/testapp/post/_inline/validate/"

    def test_the_inline_route_is_not_swallowed_by_the_detail_route(self, signed_in, post):
        """``<path:pk>`` is greedy; the inline routes have to be mounted before it."""
        from django.urls import resolve

        assert resolve("/admin/testapp/post/_inline/").func.view_class is inline.InlineWriteView
