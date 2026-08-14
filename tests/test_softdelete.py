"""Delete means trash when the model has a trash, and the trash can be emptied.

The bug these cover: the panel detected a soft-delete column, filtered those rows out
of every list, counted them, and offered a trash view — while the Delete button called
``instance.delete()`` and destroyed the row for good. A trash that cannot be restored
from is worse than no trash at all, because it looks like an undo.

Two fixtures, because the hard case is the second one. ``Archive`` marks deletion with
a nullable timestamp and its default manager still returns trashed rows. ``Ticket``
marks it with a boolean *and* hides trashed rows behind a custom default manager, so
every lookup that goes through ``get_queryset`` cannot see the very record being
restored — which is exactly the model the feature exists for.
"""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from tests.testapp.models import Archive, Post, Ticket
from vantage.models import VantageLogEntry

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


def url(obj, verb):
    label = obj._meta.label_lower.replace(".", "-")
    return reverse(f"vantage:{label}-{verb}", args=[obj.pk])


class TestDeleteTrashesInsteadOfDestroying:
    def test_a_timestamp_model_is_marked_not_removed(self, boss):
        row = Archive.objects.create(title="Quarterly report")
        boss.post(url(row, "delete"))

        row.refresh_from_db()
        assert row.deleted_at is not None

    def test_a_boolean_model_is_marked_not_removed(self, boss):
        row = Ticket.objects.create(subject="Printer on fire")
        boss.post(url(row, "delete"))

        # Through _base_manager: the default manager hides it, which is the point.
        assert Ticket._base_manager.filter(pk=row.pk).exists()
        assert Ticket._base_manager.get(pk=row.pk).is_deleted is True

    def test_a_model_with_no_trash_is_still_destroyed(self, boss, django_user_model):
        """The feature must not quietly stop deletes working everywhere else."""
        from tests.testapp.models import Author

        author = Author.objects.create(name="Ada", email="ada@example.com")
        post = Post.objects.create(
            title="T", slug="t", body="b", author=author, status=Post.Status.DRAFT
        )
        boss.post(url(post, "delete"))
        assert not Post.objects.filter(pk=post.pk).exists()

    def test_the_row_leaves_the_list(self, boss):
        row = Archive.objects.create(title="Quarterly report")
        boss.post(url(row, "delete"))

        body = boss.get(reverse("vantage:testapp-archive-list")).content.decode()
        # The link, not the title: the title is also in the "moved to trash" message
        # that this very redirect just set, so matching the string would always fail.
        assert url(row, "detail") not in body


class TestEmptyingTheTrash:
    """A second delete on an already-trashed row destroys it.

    No separate "delete permanently" view: the trash's Delete button is the same URL,
    and the row's own state decides what it means. One less route, and no way to end up
    with a bin that cannot be emptied.
    """

    def test_deleting_an_already_trashed_row_destroys_it(self, boss):
        row = Archive.objects.create(title="Gone", deleted_at="2020-01-01T00:00:00Z")
        boss.post(url(row, "delete"))
        assert not Archive.objects.filter(pk=row.pk).exists()

    def test_it_works_even_when_the_manager_hides_the_row(self, boss):
        """The regression that made this worth a test of its own.

        ``get_record`` goes through the default manager, which for this model cannot
        return a trashed row — so without widening the lookup the delete page 404s and
        the trash fills up forever.
        """
        row = Ticket.objects.create(subject="Old", is_deleted=True)
        response = boss.post(url(row, "delete"))

        assert response.status_code != 404
        assert not Ticket._base_manager.filter(pk=row.pk).exists()


class TestRestore:
    def test_a_trashed_row_comes_back(self, boss):
        row = Archive.objects.create(title="Wanted back", deleted_at="2020-01-01T00:00:00Z")
        boss.post(url(row, "restore"))

        row.refresh_from_db()
        assert row.deleted_at is None

    def test_it_works_when_the_manager_hides_the_row(self, boss):
        row = Ticket.objects.create(subject="Reopen", is_deleted=True)
        boss.post(url(row, "restore"))

        assert Ticket.objects.filter(pk=row.pk).exists()

    def test_restore_refuses_a_get(self, boss):
        """Restoring changes what every list shows; a crawler must not be able to."""
        row = Archive.objects.create(title="Wanted back", deleted_at="2020-01-01T00:00:00Z")
        response = boss.get(url(row, "restore"))

        assert response.status_code == 405
        row.refresh_from_db()
        assert row.deleted_at is not None

    def test_a_model_with_no_trash_has_no_restore_route(self, boss):
        from tests.testapp.models import Author

        author = Author.objects.create(name="Ada", email="ada@example.com")
        post = Post.objects.create(
            title="T", slug="t", body="b", author=author, status=Post.Status.DRAFT
        )
        assert boss.post(url(post, "restore")).status_code == 404

    def test_a_reader_cannot_restore(self, client):
        """Guarded by the delete permission, so viewing the trash is not enough."""
        reader = User.objects.create_user("reader", "r@example.com", "x", is_staff=True)
        client.force_login(reader)
        row = Archive.objects.create(title="Not yours", deleted_at="2020-01-01T00:00:00Z")

        assert client.post(url(row, "restore")).status_code in (403, 404)
        row.refresh_from_db()
        assert row.deleted_at is not None


class TestTheAuditTrailTellsThemApart:
    """"Deleted" for both cases would make the log unable to answer the only
    question anyone asks it: is it gone, or can I get it back?"""

    def test_trashing_records_its_own_action(self, boss):
        row = Archive.objects.create(title="Report")
        boss.post(url(row, "delete"))

        entry = VantageLogEntry.objects.latest("id")
        assert entry.action == VantageLogEntry.Action.TRASH

    def test_destroying_records_a_delete(self, boss):
        row = Archive.objects.create(title="Report", deleted_at="2020-01-01T00:00:00Z")
        boss.post(url(row, "delete"))

        entry = VantageLogEntry.objects.latest("id")
        assert entry.action == VantageLogEntry.Action.DELETE

    def test_restoring_records_a_restore(self, boss):
        row = Archive.objects.create(title="Report", deleted_at="2020-01-01T00:00:00Z")
        boss.post(url(row, "restore"))

        entry = VantageLogEntry.objects.latest("id")
        assert entry.action == VantageLogEntry.Action.RESTORE


class TestTheWriteItself:
    def test_a_soft_delete_does_not_call_save(self, boss, monkeypatch):
        """Pressing Delete must not fire signals or stamps written for an edit."""
        row = Archive.objects.create(title="Report")

        # Patched only after the row exists: create() goes through save() too, and
        # stubbing it first would leave the fixture unsaved and the test meaningless.
        called = []
        monkeypatch.setattr(
            Archive, "save", lambda self, *a, **k: called.append(1), raising=False
        )

        boss.post(url(row, "delete"))

        assert called == []
        assert Archive._base_manager.get(pk=row.pk).deleted_at is not None
