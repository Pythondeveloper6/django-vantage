"""Where the 0.2.0a1 features meet: masking must hold on every surface added beside it.

Each feature was built on its own branch, so each branch's leak tests only knew the
surfaces that existed when it was written. List view cards, undo responses and the
data quality page arrived alongside sensitive-field masking, not before it; these tests
are the seam between them.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from tests.test_sensitive import CARD, KEY, PIN, TOKEN, leaks
from tests.testapp.models import Author, ZCredential
from vantage import concurrency
from vantage.sites import site


@pytest.fixture
def panel():
    panel = site.get_panel(ZCredential)
    saved = dict(panel.__dict__)
    yield panel
    panel.__dict__.clear()
    panel.__dict__.update(saved)


@pytest.fixture(autouse=True)
def _fresh_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


@pytest.fixture
def cred(db):
    from django.utils import timezone

    owner = Author.objects.create(name="Ada Owner", email="ada@example.com")
    return ZCredential.objects.create(
        name="Production key",
        owner=owner,
        api_token=TOKEN,
        card_number=CARD,
        pin=PIN,
        private_key=KEY,
        password_changed_at=timezone.now(),
    )


LIST_URL = "vantage:testapp-zcredential-list"


class TestListViewCards:
    """A card is a list row by another name, so it must mask like one."""

    @pytest.mark.parametrize("mode", ["kanban", "calendar"])
    def test_declared_sensitive_card_fields_are_masked(self, boss, panel, cred, mode):
        panel.list_views = ["table", "kanban", "calendar"]
        panel.kanban_field = "token_type"
        panel.calendar_field = "password_changed_at"
        panel.card_fields = ["api_token", "card_number", "private_key"]
        response = boss.get(reverse(LIST_URL), {"view": mode})
        assert response.status_code == 200
        assert "Production key" in response.content.decode()
        assert not leaks(response.content)

    def test_inferred_cards_leak_nothing(self, boss, panel, cred):
        response = boss.get(reverse(LIST_URL), {"view": "kanban"})
        assert response.status_code == 200
        assert not leaks(response.content)

    def test_a_kanban_move_answers_without_secrets(self, boss, panel, cred):
        panel.list_views = ["table", "kanban"]
        panel.kanban_field = "token_type"
        response = boss.post(
            reverse("vantage:testapp-zcredential-kanban-move"),
            data=json.dumps(
                {"pk": cred.pk, "value": "basic", "version": concurrency.token_for(cred)}
            ),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert not leaks(response.content)
        cred.refresh_from_db()
        assert cred.token_type == "basic"
        assert cred.api_token == TOKEN


class TestUndoResponses:
    def test_inline_edit_and_its_undo_return_masked_rows(self, boss, panel, cred):
        # The returned rows carry every list column, the masked one included.
        panel.list_display = ["name", "tokenizer_name", "api_token"]
        panel.list_editable = ["tokenizer_name"]
        fields = {"tokenizer_name": "bpe"}
        payload = {
            "changes": [{"pk": cred.pk, "version": concurrency.token_for(cred), "fields": fields}]
        }
        saved = boss.patch(
            reverse("vantage:testapp-zcredential-inline"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert saved.status_code == 200, saved.content
        assert not leaks(saved.content)
        body = json.loads(saved.content)

        undone = boss.post(
            reverse("vantage:undo"),
            {"token": body["undo"]["token"]},
            HTTP_ACCEPT="application/json",
        )
        assert undone.status_code == 200, undone.content
        assert not leaks(undone.content)
        cred.refresh_from_db()
        assert cred.tokenizer_name == ""
        assert cred.api_token == TOKEN

    def test_an_undone_hard_delete_restores_the_secret_without_showing_it(self, boss, panel, cred):
        from django.contrib.messages import get_messages

        from vantage import undo

        panel.actions = ["delete_selected"]
        response = boss.post(
            reverse(LIST_URL),
            {"action": "delete_selected", "selected": [str(cred.pk)], "confirmed": "1"},
        )
        assert not ZCredential.objects.filter(pk=cred.pk).exists()
        offer = undo.offer_from_message(list(get_messages(response.wsgi_request))[-1])
        assert offer, "a plain delete of one row should be undoable"

        undone = boss.post(
            reverse("vantage:undo"), {"token": offer["token"]}, HTTP_ACCEPT="application/json"
        )
        assert undone.status_code == 200, undone.content
        assert not leaks(undone.content)
        assert ZCredential.objects.get(pk=cred.pk).api_token == TOKEN


class TestDataQuality:
    def test_the_audit_never_prints_a_secret(self, boss, cred):
        overview = boss.get(reverse("vantage:page-data-quality"))
        assert overview.status_code == 200
        assert not leaks(overview.content)
        detail = boss.get(reverse("vantage:page-data-quality"), {"model": "testapp.zcredential"})
        assert not leaks(detail.content)


class TestUnparseablePk:
    """``<path:pk>`` hands ``/1/change/`` over as the pk "1/change": a 404, not a 500."""

    @pytest.mark.parametrize("suffix", ["change/", "nope/", "edit/extra/"])
    def test_a_guessed_record_url_is_not_found(self, boss, cred, suffix):
        base = reverse("vantage:testapp-zcredential-detail", args=[cred.pk])
        assert boss.get(f"{base}{suffix}").status_code == 404


class TestKanbanRouteEverywhere:
    """The move route exists for every panel; the view decides what may move."""

    def test_a_model_without_a_board_changes_nothing(self, boss, panel, cred):
        panel.list_views = ["table"]
        response = boss.post(
            reverse("vantage:testapp-zcredential-kanban-move"),
            data=json.dumps(
                {"pk": cred.pk, "value": "basic", "version": concurrency.token_for(cred)}
            ),
            content_type="application/json",
        )
        assert response.status_code >= 400
        cred.refresh_from_db()
        assert cred.token_type == "bearer"


class TestCardTitles:
    """A card's title is the record's name, and a name can carry the secret."""

    @pytest.mark.parametrize("mode", ["kanban", "calendar"])
    def test_a_secret_in_str_is_not_a_card_title(self, boss, panel, cred, mode):
        ZCredential.objects.filter(pk=cred.pk).update(name=f"key {TOKEN}")
        panel.list_views = ["table", "kanban", "calendar"]
        panel.kanban_field = "token_type"
        panel.calendar_field = "password_changed_at"
        response = boss.get(reverse(LIST_URL), {"view": mode})
        assert response.status_code == 200
        assert not leaks(response.content)


class TestUndoEntriesAreNotAdministrable:
    """Snapshots hold deleted rows verbatim; they must never get a generic CRUD page."""

    def test_undo_entries_are_not_registered(self):
        from vantage.models import UndoEntry

        assert not site.is_registered(UndoEntry)
