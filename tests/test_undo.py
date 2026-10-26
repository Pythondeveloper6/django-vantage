"""Undo toasts: reversing a delete or an edit, and refusing to when that would lose data.

The tests are grouped by the promise they check. Most of them are about the second half
of that sentence — an undo that would overwrite a newer edit, re-create a row over one
that has taken its place, or resurrect a record whose side effects already happened must
not run at all — because that is the half a happy-path test cannot see.
"""

import datetime as dt
import json

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.db.models.signals import post_delete
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from tests.testapp.models import Archive, Author, Category, Comment, Order, Post, Tag, Ticket
from vantage import actions, records, undo
from vantage.models import UndoEntry, VantageLogEntry
from vantage.sites import site

pytestmark = pytest.mark.django_db

UNDO_URL = "vantage:undo"


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def boss_user(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def boss(client, boss_user):
    client.force_login(boss_user)
    return client


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada", email="ada@example.com")


def make_post(author, slug, **extra):
    return Post.objects.create(title=slug.title(), slug=slug, body="b", author=author, **extra)


def url(obj, verb):
    label = obj._meta.label_lower.replace(".", "-")
    return reverse(f"vantage:{label}-{verb}", args=[obj.pk])


def list_url(model):
    return reverse(f"vantage:{model._meta.label_lower.replace('.', '-')}-list")


def last_toast(response):
    found = list(get_messages(response.wsgi_request))
    assert found, "no message was set"
    return found[-1]


def token_of(response):
    offer = undo.offer_from_message(last_toast(response))
    return offer["token"] if offer else None


def press(client, token, **headers):
    return client.post(reverse(UNDO_URL), {"token": token}, **headers)


def press_json(client, token):
    response = client.post(reverse(UNDO_URL), {"token": token}, HTTP_ACCEPT="application/json")
    return response.status_code, json.loads(response.content)


def undo_audits():
    return VantageLogEntry.objects.filter(action_name="undo")


# ── settings ────────────────────────────────────────────────────────────────


class TestSettings:
    def test_the_window_defaults_to_ten_seconds(self):
        assert undo.window_seconds() == 10

    @pytest.mark.parametrize("value", [-1, "10", 2.5, True])
    def test_a_bad_window_is_refused(self, value):
        from django.core.exceptions import ImproperlyConfigured

        from vantage.conf import vantage_settings

        with override_settings(VANTAGE={"UNDO_WINDOW": value}):
            with pytest.raises(ImproperlyConfigured, match="UNDO_WINDOW"):
                _ = vantage_settings.UNDO_WINDOW

    def test_a_panel_must_say_true_false_or_none(self):
        from vantage.panel_checks import ModelPanelChecks

        panel = site.get_panel(Archive)
        panel.undo = "yes"
        try:
            ids = [error.id for error in ModelPanelChecks().check(panel)]
        finally:
            panel.undo = None
        assert "vantage.E070" in ids


# ── soft delete ─────────────────────────────────────────────────────────────


class TestSoftDelete:
    def test_the_toast_offers_undo_and_undo_restores(self, boss):
        row = Archive.objects.create(title="Quarterly")
        response = boss.post(url(row, "delete"))
        row.refresh_from_db()
        assert row.deleted_at is not None

        token = token_of(response)
        assert token
        status, body = press_json(boss, token)
        assert status == 200 and body["ok"]
        row.refresh_from_db()
        assert row.deleted_at is None

    def test_it_works_when_the_manager_hides_the_trash(self, boss):
        row = Ticket.objects.create(subject="Printer")
        token = token_of(boss.post(url(row, "delete")))
        assert press_json(boss, token)[0] == 200
        assert Ticket.objects.filter(pk=row.pk).exists()

    def test_restored_by_someone_else_meanwhile_is_a_conflict(self, boss):
        row = Archive.objects.create(title="Quarterly")
        token = token_of(boss.post(url(row, "delete")))
        # Restored and trashed again by a colleague: the mark is no longer ours.
        Archive.objects.filter(pk=row.pk).update(
            deleted_at=timezone.now() + dt.timedelta(minutes=1)
        )
        status, body = press_json(boss, token)
        assert status == 409 and not body["ok"]
        row.refresh_from_db()
        assert row.deleted_at is not None
        assert not undo_audits().exists()


# ── hard delete ─────────────────────────────────────────────────────────────


@pytest.fixture
def graph(author):
    """An author with two posts, comments on one, tags on both, and a category."""
    category = Category.objects.create(name="Engines", slug="engines")
    tags = [Tag.objects.create(name="a"), Tag.objects.create(name="b")]
    first = make_post(author, "first", category=category, rating="4.50", metadata={"k": [1]})
    second = make_post(author, "second")
    first.tags.set(tags)
    second.tags.set(tags[:1])
    Comment.objects.create(post=first, author_name="x", body="hi")
    Comment.objects.create(post=first, author_name="y", body="ho")
    return {"author": author, "posts": [first, second], "category": category, "tags": tags}


def state(author_pk):
    """Everything about an author's graph that a faithful restore must reproduce."""
    posts = Post.objects.filter(author_id=author_pk).order_by("pk")
    return {
        "author": list(Author.objects.filter(pk=author_pk).values()),
        "posts": list(posts.values()),
        "comments": list(Comment.objects.filter(post__author_id=author_pk).order_by("pk").values()),
        "tags": sorted((p.pk, t.pk) for p in posts for t in p.tags.all()),
    }


class TestHardDelete:
    def test_undo_restores_the_whole_cascade_exactly(self, boss, graph):
        author = graph["author"]
        before = state(author.pk)
        response = boss.post(url(author, "delete"))
        assert not Author.objects.filter(pk=author.pk).exists()
        assert not Comment.objects.exists()

        status, body = press_json(boss, token_of(response))
        assert status == 200, body
        # Same primary keys, same microsecond timestamps, same m2m rows, same JSON.
        assert state(author.pk) == before

    def test_set_null_references_are_put_back(self, boss, graph):
        category = graph["category"]
        response = boss.post(url(category, "delete"))
        assert Post.objects.get(pk=graph["posts"][0].pk).category_id is None

        assert press_json(boss, token_of(response))[0] == 200
        assert Post.objects.get(pk=graph["posts"][0].pk).category_id == category.pk

    def test_a_re_pointed_reference_is_a_conflict_and_nothing_is_restored(self, boss, graph):
        category = graph["category"]
        token = token_of(boss.post(url(category, "delete")))
        other = Category.objects.create(name="Other", slug="other")
        Post.objects.filter(pk=graph["posts"][0].pk).update(category=other)

        status, _body = press_json(boss, token)
        assert status == 409
        assert not Category.objects.filter(pk=category.pk).exists()
        assert Post.objects.get(pk=graph["posts"][0].pk).category_id == other.pk

    def test_a_reused_primary_key_is_never_overwritten(self, boss, author):
        token = token_of(boss.post(url(author, "delete")))
        squatter = Author.objects.create(pk=author.pk, name="Someone else", email="s@e.com")

        status, _body = press_json(boss, token)
        assert status == 409
        squatter.refresh_from_db()
        assert squatter.name == "Someone else"

    def test_a_taken_unique_value_rolls_everything_back(self, boss, graph):
        author = graph["author"]
        token = token_of(boss.post(url(author, "delete")))
        Author.objects.create(name="New", email=author.email)

        status, _body = press_json(boss, token)
        assert status == 409
        # All or nothing: not one post or comment came back on its own.
        assert not Post.objects.filter(author_id=author.pk).exists()
        assert not Comment.objects.exists()

    def test_delete_receivers_mean_no_undo_but_the_delete_still_happens(self, boss, graph):
        calls = []

        def receiver(sender, **kwargs):
            calls.append(sender)

        post_delete.connect(receiver, sender=Comment)
        try:
            response = boss.post(url(graph["author"], "delete"))
        finally:
            post_delete.disconnect(receiver, sender=Comment)
        assert not Author.objects.filter(pk=graph["author"].pk).exists()
        assert calls  # the receiver ran: Django's own delete path was used
        assert token_of(response) is None
        assert "Deleted" in str(last_toast(response))

    def test_an_overridden_delete_runs_and_offers_no_undo(self, boss, author, monkeypatch):
        seen = []
        original = Author.delete

        def custom(self, *args, **kwargs):
            seen.append(self.pk)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Author, "delete", custom)
        response = boss.post(url(author, "delete"))
        assert seen == [author.pk]
        assert token_of(response) is None

    def test_too_many_rows_means_no_undo(self, boss, graph):
        with override_settings(VANTAGE={"UNDO_MAX_ROWS": 3}):
            response = boss.post(url(graph["author"], "delete"))
        assert not Author.objects.filter(pk=graph["author"].pk).exists()
        assert token_of(response) is None

    def test_no_undo_when_the_window_is_zero(self, boss, author):
        with override_settings(VANTAGE={"UNDO_WINDOW": 0}):
            response = boss.post(url(author, "delete"))
        assert token_of(response) is None
        assert not UndoEntry.objects.exists()

    def test_no_undo_when_the_panel_opts_out(self, boss, author, monkeypatch):
        monkeypatch.setattr(site.get_panel(Author), "undo", False, raising=False)
        response = boss.post(url(author, "delete"))
        assert token_of(response) is None

    def test_restoring_from_the_trash_puts_it_back_in_the_trash(self, boss):
        row = Archive.objects.create(title="Old", deleted_at=timezone.now())
        token = token_of(boss.post(url(row, "delete")))
        assert not Archive.objects.filter(pk=row.pk).exists()
        assert press_json(boss, token)[0] == 200
        assert Archive.objects.get(pk=row.pk).deleted_at is not None


# ── bulk delete ─────────────────────────────────────────────────────────────


@pytest.fixture
def bulk_delete(monkeypatch):
    def enable(model):
        monkeypatch.setattr(site.get_panel(model), "actions", ["delete_selected"])

    return enable


def run_bulk(client, model, pks):
    return client.post(
        list_url(model),
        {"action": "delete_selected", "selected": [str(pk) for pk in pks], "confirmed": "1"},
    )


class TestBulkDelete:
    def test_the_built_in_is_opt_in(self):
        names = [a.name for a in actions.resolve_actions(site.get_panel(Author))]
        assert "delete_selected" not in names

    def test_a_hard_bulk_delete_comes_back(self, boss, bulk_delete, graph):
        bulk_delete(Post)
        pks = [p.pk for p in graph["posts"]]
        before = state(graph["author"].pk)
        response = run_bulk(boss, Post, pks)
        assert not Post.objects.filter(pk__in=pks).exists()
        assert "Deleted 2 posts" in str(last_toast(response))

        assert press_json(boss, token_of(response))[0] == 200
        assert state(graph["author"].pk) == before

    def test_a_soft_bulk_delete_is_a_trash_and_comes_back(self, boss, bulk_delete):
        bulk_delete(Archive)
        rows = [Archive.objects.create(title=f"r{i}") for i in range(3)]
        response = run_bulk(boss, Archive, [r.pk for r in rows])
        assert Archive.objects.filter(deleted_at__isnull=False).count() == 3
        assert "trash" in str(last_toast(response))

        assert press_json(boss, token_of(response))[0] == 200
        assert Archive.objects.filter(deleted_at__isnull=True).count() == 3

    def test_a_protected_reference_deletes_nothing(self, boss, bulk_delete, author):
        bulk_delete(Author)
        spare = Author.objects.create(name="Spare", email="spare@example.com")
        Order.objects.create(number="1", customer=author, total="1.00")
        response = run_bulk(boss, Author, [author.pk, spare.pk])
        assert Author.objects.filter(pk__in=[author.pk, spare.pk]).count() == 2
        assert last_toast(response).level_tag == "error"

    def test_it_needs_the_delete_permission(self, client, bulk_delete, author):
        bulk_delete(Author)
        user = User.objects.create_user("ed", password="x", is_staff=True)
        for codename in ("view_author", "change_author"):
            user.user_permissions.add(Permission.objects.get(codename=codename))
        client.force_login(user)
        response = run_bulk(client, Author, [author.pk])
        assert response.status_code == 403
        assert Author.objects.filter(pk=author.pk).exists()


# ── edits ───────────────────────────────────────────────────────────────────


def bulk_edit(client, pks, **values):
    data = {
        "selected": [str(pk) for pk in pks],
        "_bulk_apply": "1",
        records.BULK_EDIT_ENABLE: list(values),
    }
    data.update({f"{records.BULK_EDIT_PREFIX}-{k}": v for k, v in values.items()})
    return client.post(reverse("vantage:testapp-post-bulk-edit"), data)


class TestBulkEdit:
    def test_undo_restores_each_rows_previous_value(self, boss, author):
        draft = make_post(author, "a", status="draft")
        review = make_post(author, "b", status="review")
        response = bulk_edit(boss, [draft.pk, review.pk], status="published")
        assert Post.objects.filter(status="published").count() == 2

        assert press_json(boss, token_of(response))[0] == 200
        assert Post.objects.get(pk=draft.pk).status == "draft"
        assert Post.objects.get(pk=review.pk).status == "review"

    def test_a_row_edited_since_is_a_conflict_and_nothing_changes(self, boss, author):
        first = make_post(author, "a", status="draft")
        second = make_post(author, "b", status="draft")
        token = token_of(bulk_edit(boss, [first.pk, second.pk], status="published"))
        Post.objects.filter(pk=second.pk).update(status="review")

        status, body = press_json(boss, token)
        assert status == 409 and "1 record" in body["message"]
        # Not even the untouched row was reverted: undo is all or nothing.
        assert Post.objects.get(pk=first.pk).status == "published"
        assert Post.objects.get(pk=second.pk).status == "review"

    def test_undo_advances_the_version_column(self, boss, author):
        post = make_post(author, "a")
        token = token_of(bulk_edit(boss, [post.pk], status="published"))
        edited = Post.objects.get(pk=post.pk).updated_at
        press_json(boss, token)
        assert Post.objects.get(pk=post.pk).updated_at > edited

    def test_queries_do_not_grow_with_the_row_count(
        self, boss, author, django_assert_max_num_queries
    ):
        pks = [make_post(author, f"p{i}").pk for i in range(25)]
        token = token_of(bulk_edit(boss, pks, status="published"))
        with django_assert_max_num_queries(20):
            press(boss, token)
        assert Post.objects.filter(status="draft").count() == 25


class TestInlineEdit:
    @pytest.fixture
    def editable(self, monkeypatch):
        monkeypatch.setattr(site.get_panel(Post), "list_editable", ["status", "view_count"])

    def save(self, client, post, **fields):
        from vantage import concurrency

        payload = {
            "changes": [{"pk": post.pk, "version": concurrency.token_for(post), "fields": fields}]
        }
        return client.patch(
            reverse("vantage:testapp-post-inline"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_the_response_offers_undo_and_undo_returns_fresh_rows(self, boss, author, editable):
        post = make_post(author, "a", status="draft", view_count=3)
        body = json.loads(self.save(boss, post, status="review", view_count=9).content)
        assert body["ok"] and body["undo"]["remaining"] > 0

        status, result = press_json(boss, body["undo"]["token"])
        assert status == 200
        post.refresh_from_db()
        assert (post.status, post.view_count) == ("draft", 3)
        # The grid gets the rows back, with a fresh version token, and needs no reload.
        assert result["rows"][0]["values"]["view_count"] == "3"
        assert result["rows"][0]["version"]

    def test_a_cell_changed_since_is_a_conflict(self, boss, author, editable):
        post = make_post(author, "a", status="draft")
        token = json.loads(self.save(boss, post, status="review").content)["undo"]["token"]
        Post.objects.filter(pk=post.pk).update(status="published")
        assert press_json(boss, token)[0] == 409
        assert Post.objects.get(pk=post.pk).status == "published"


class TestActionHooks:
    def install(self, monkeypatch, act):
        monkeypatch.setattr(site.get_panel(Order), "actions", [act])

    def order(self, author, number, status="pending"):
        return Order.objects.create(number=number, customer=author, total="9.99", status=status)

    def test_undo_fields_reverses_an_update_action(self, boss, author, monkeypatch):
        act = actions.Action(
            func=lambda panel, request, qs: f"{qs.update(status='paid')} paid.",
            name="mark_paid",
            label="Mark paid",
            bulk_strategy="sql",
            undo_fields=["status"],
        )
        self.install(monkeypatch, act)
        one, two = self.order(author, "1"), self.order(author, "2", "shipped")
        response = boss.post(list_url(Order), {"action": "mark_paid", "selected": [one.pk, two.pk]})
        assert Order.objects.filter(status="paid").count() == 2

        assert press_json(boss, token_of(response))[0] == 200
        assert Order.objects.get(pk=one.pk).status == "pending"
        assert Order.objects.get(pk=two.pk).status == "shipped"

    def test_an_action_without_an_undo_offers_none(self, boss, author, monkeypatch):
        act = actions.Action(
            func=lambda panel, request, qs: f"{qs.update(status='paid')} paid.",
            name="mark_paid",
            label="Mark paid",
        )
        self.install(monkeypatch, act)
        one = self.order(author, "1")
        response = boss.post(list_url(Order), {"action": "mark_paid", "selected": [one.pk]})
        assert token_of(response) is None

    def test_a_custom_hook_gets_its_state_back(self, boss, author, monkeypatch):
        received = []

        def run(panel, request, qs):
            pks = list(qs.values_list("pk", flat=True))
            qs.update(status="refunded")
            return actions.ActionResult("Refunded.", undo_state={"pks": pks})

        def reverse_it(panel, request, state):
            received.append(state)
            Order.objects.filter(pk__in=state["pks"]).update(status="pending")

        self.install(
            monkeypatch,
            actions.Action(func=run, name="refund", label="Refund", undo=reverse_it),
        )
        one = self.order(author, "1")
        response = boss.post(list_url(Order), {"action": "refund", "selected": [one.pk]})
        assert press_json(boss, token_of(response))[0] == 200
        assert received == [{"pks": [one.pk]}]
        assert Order.objects.get(pk=one.pk).status == "pending"

    def test_a_hook_that_refuses_changes_nothing(self, boss, author, monkeypatch):
        def run(panel, request, qs):
            qs.update(status="refunded")
            return actions.ActionResult("Refunded.", undo_state={"pks": [1]})

        def refuse(panel, request, state):
            Order.objects.update(status="pending")  # rolled back by the conflict
            raise undo.UndoConflict("The refund has already been paid out.")

        self.install(
            monkeypatch, actions.Action(func=run, name="refund", label="Refund", undo=refuse)
        )
        one = self.order(author, "1")
        response = boss.post(list_url(Order), {"action": "refund", "selected": [one.pk]})
        status, body = press_json(boss, token_of(response))
        assert status == 409 and body["message"] == "The refund has already been paid out."
        assert Order.objects.get(pk=one.pk).status == "refunded"

    def test_the_built_in_publish_action_is_undoable(self, boss, monkeypatch):
        from tests.testapp.models import Promo

        promo = Promo.objects.create(headline="Sale", is_published=False)
        response = boss.post(list_url(Promo), {"action": "publish", "selected": [promo.pk]})
        assert Promo.objects.get(pk=promo.pk).is_published
        assert press_json(boss, token_of(response))[0] == 200
        assert not Promo.objects.get(pk=promo.pk).is_published

    def test_a_job_sized_action_offers_no_undo(self, boss, author, monkeypatch):
        from vantage import jobs

        act = actions.Action(
            func=lambda panel, request, qs: f"{qs.update(status='paid')} paid.",
            name="mark_paid",
            label="Mark paid",
            undo_fields=["status"],
        )
        self.install(monkeypatch, act)
        monkeypatch.setattr(jobs, "should_enqueue", lambda action, count: True)
        one = self.order(author, "1")
        boss.post(list_url(Order), {"action": "mark_paid", "selected": [one.pk]})
        assert not UndoEntry.objects.exists()


# ── the token ───────────────────────────────────────────────────────────────


@pytest.fixture
def trashed(boss):
    row = Archive.objects.create(title="Quarterly")
    return row, token_of(boss.post(url(row, "delete")))


class TestToken:
    def test_it_is_single_use_and_a_double_click_undoes_once(self, boss, trashed):
        row, token = trashed
        first = press_json(boss, token)
        Archive.objects.filter(pk=row.pk).update(deleted_at=timezone.now())  # trashed again
        second = press_json(boss, token)
        assert first[0] == 200 and second[0] == 200
        assert second[1]["already"] and second[1]["message"] == first[1]["message"]
        # The second press changed nothing: the row stays in the trash it was put back in.
        assert Archive.objects.get(pk=row.pk).deleted_at is not None
        assert undo_audits().count() == 1

    def test_it_is_audit_logged(self, boss, trashed):
        _row, token = trashed
        press_json(boss, token)
        entry = undo_audits().get()
        assert entry.object_repr.startswith("undid delete of archive")

    def test_another_user_cannot_redeem_it(self, trashed, db):
        row, token = trashed
        other = Client()
        other.force_login(User.objects.create_superuser("eve", "eve@example.com", "x"))
        assert press_json(other, token)[0] == 404
        assert Archive.objects.get(pk=row.pk).deleted_at is not None

    def test_another_session_of_the_same_user_cannot_redeem_it(self, trashed, boss_user):
        row, token = trashed
        laptop = Client()
        laptop.force_login(boss_user)
        assert press_json(laptop, token)[0] == 404
        assert Archive.objects.get(pk=row.pk).deleted_at is not None

    def test_an_expired_token_changes_nothing(self, boss, trashed):
        row, token = trashed
        UndoEntry.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))
        assert press_json(boss, token)[0] == 410
        assert Archive.objects.get(pk=row.pk).deleted_at is not None

    def test_a_revoked_permission_is_refused_and_the_token_survives(self, boss, trashed, boss_user):
        row, token = trashed
        # Same browser, same session — only the grant changed. Viewing archives is
        # still allowed; deleting them, which is what the undo needs, is not.
        boss_user.is_superuser = False
        boss_user.save()
        boss_user.user_permissions.add(
            Permission.objects.get(
                content_type=ContentType.objects.get_for_model(Archive), codename="view_archive"
            )
        )
        assert press_json(boss, token)[0] == 403
        assert Archive.objects.get(pk=row.pk).deleted_at is not None
        assert UndoEntry.objects.get().used_at is None

    def test_a_get_is_refused(self, boss, trashed):
        _row, token = trashed
        assert boss.get(reverse(UNDO_URL), {"token": token}).status_code == 405

    def test_it_is_csrf_protected(self, boss_user, db):
        client = Client(enforce_csrf_checks=True)
        client.force_login(boss_user)
        row = Archive.objects.create(title="Q")
        # The delete itself is not under test here; make the entry directly.
        response = press(client, "anything")
        assert response.status_code == 403
        assert Archive.objects.filter(pk=row.pk).exists()

    def test_read_only_mode_refuses_it(self, boss, trashed):
        row, token = trashed
        with override_settings(VANTAGE={"READ_ONLY": True}):
            assert press_json(boss, token)[0] == 403
        assert Archive.objects.get(pk=row.pk).deleted_at is not None

    def test_without_js_the_page_redirects_back_with_a_message(self, boss, trashed):
        _row, token = trashed
        response = boss.post(reverse(UNDO_URL), {"token": token, "next": list_url(Archive)})
        assert response.status_code == 302 and response["Location"] == list_url(Archive)
        assert "Restored" in str(last_toast(response))

    def test_an_offsite_next_is_ignored(self, boss, trashed):
        _row, token = trashed
        response = boss.post(reverse(UNDO_URL), {"token": token, "next": "https://evil.example/"})
        assert response["Location"] == list_url(Archive)

    def test_an_unknown_token_is_simply_gone(self, boss):
        assert press_json(boss, "nope")[0] == 404


class TestHousekeeping:
    def test_expired_entries_are_purged_when_a_new_one_is_written(self, boss):
        first = Archive.objects.create(title="a")
        boss.post(url(first, "delete"))
        UndoEntry.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))
        second = Archive.objects.create(title="b")
        boss.post(url(second, "delete"))
        assert UndoEntry.objects.count() == 1

    def test_each_user_keeps_a_bounded_number(self, boss, monkeypatch):
        monkeypatch.setattr(undo, "MAX_PER_USER", 3)
        for i in range(5):
            boss.post(url(Archive.objects.create(title=str(i)), "delete"))
        assert UndoEntry.objects.count() == 3

    def test_a_redeemed_entry_drops_its_snapshot(self, boss, author):
        token = token_of(boss.post(url(author, "delete")))
        press_json(boss, token)
        assert UndoEntry.objects.get().payload == ""

    def test_the_management_command_purges(self, boss):
        from django.core.management import call_command

        boss.post(url(Archive.objects.create(title="a"), "delete"))
        UndoEntry.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))
        call_command("vantage_purge_undo")
        assert not UndoEntry.objects.exists()


# ── the toast ───────────────────────────────────────────────────────────────


class TestToast:
    def test_the_form_is_rendered_hidden_for_progressive_enhancement(self, boss):
        row = Archive.objects.create(title="Q")
        page = boss.post(url(row, "delete"), follow=True).content.decode()
        assert 'role="status"' in page
        assert "data-undo-form hidden" in page
        assert "data-undo-remaining=" in page
        assert f'action="{reverse(UNDO_URL)}"' in page

    def test_a_plain_toast_has_no_undo_form(self, boss):
        page = boss.post(
            reverse("vantage:testapp-archive-add"), {"title": "New"}, follow=True
        ).content.decode()
        assert "data-undo-form" not in page

    def test_an_expired_offer_renders_no_button(self):
        class Message:
            extra_tags = f"{undo.TAG_PREFIX}abc:{int(timezone.now().timestamp()) - 5}"

        assert undo.offer_from_message(Message()) is None
