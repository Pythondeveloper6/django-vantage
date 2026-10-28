"""A record's history: the record as it was at any point, and restoring fields from there.

Grouped by promise. Most of the weight is on the refusals — a value the log cannot
vouch for must not be shown as though it could, a secret must not surface from an old
entry, and a restore must not run over a record that has moved since the page was
drawn — because a happy-path test passes just as well when every one of those is
broken.
"""

import datetime as dt

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from tests import test_tenancy
from tests.testapp.models import Archive, Author, Order, Post, Preference, ZCredential
from vantage import audit, history
from vantage.models import VantageLogEntry
from vantage.sites import site

pytestmark = pytest.mark.django_db

# The tenancy suite's two-tenant fixture set, borrowed rather than rebuilt.
http_tenancy = test_tenancy.http_tenancy
orgs = test_tenancy.orgs
tenants = test_tenancy.tenants


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


def staff(client, *perms, username="sam"):
    """A non-superuser staff member holding exactly ``perms`` ("app.codename")."""
    user = User.objects.create_user(username, f"{username}@example.com", "x", is_staff=True)
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    client.force_login(user)
    return client


def edit_preference(client, pref, **values):
    data = {"key": pref.key, "value": pref.value, **values}
    response = client.post(url(pref, "change"), data)
    assert response.status_code == 302, response.content
    pref.refresh_from_db()
    return VantageLogEntry.objects.filter(object_id=str(pref.pk)).latest("created_at", "pk")


def entries_of(obj):
    return VantageLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(type(obj)), object_id=str(obj.pk)
    )


def state_of(reconstruction, name):
    return next(state for state in reconstruction.fields if state.name == name)


def at_entry(obj, entry):
    panel = site.get_panel(type(obj))
    return history.reconstruct(panel, obj, history.Position(entry.created_at, entry.pk))


def restore(client, obj, entry, fields, **extra):
    """Open the history page at ``entry``, then post its own restore form back."""
    page = client.get(url(obj, "history"), {"entry": entry.pk})
    assert page.status_code == 200
    context = page.context
    data = {
        "entry": entry.pk,
        "fields": fields,
        "version": context["version"],
        "fingerprint": context["fingerprint"],
        **extra,
    }
    return client.post(url(obj, "history"), data)


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


@pytest.fixture
def pref(boss):
    """A preference edited twice through the panel: "dark" → "light" → "sepia"."""
    created = Preference.objects.create(key="theme", value="dark")
    first = edit_preference(boss, created, value="light")
    edit_preference(boss, created, value="sepia")
    created.first_edit = first
    return created


# ── reconstruction ──────────────────────────────────────────────────────────


class TestReconstruction:
    def test_a_field_reads_as_it_was_just_after_the_chosen_change(self, pref):
        state = state_of(at_entry(pref, pref.first_edit), "value")
        assert state.known and state.value == "light" and state.current == "sepia"
        assert state.changed and state.restorable

    def test_a_field_no_one_has_touched_is_its_current_value_and_unchanged(self, pref):
        state = state_of(at_entry(pref, pref.first_edit), "key")
        assert state.known and state.value == "theme" and not state.changed

    def test_a_point_given_as_a_date_replays_every_entry_up_to_it(self, pref):
        second = entries_of(pref).latest("created_at", "pk")
        later = timezone.now() + dt.timedelta(days=1)
        entries_of(pref).filter(pk=second.pk).update(created_at=later)
        panel = site.get_panel(Preference)
        between = history.reconstruct(panel, pref, history.Position(later - dt.timedelta(hours=1)))
        assert state_of(between, "value").value == "light"

    def test_a_change_made_outside_the_panel_makes_the_field_unknown(self, pref):
        # The shell, a script, a migration: the log never saw it, so the chain of
        # "from"s and "to"s no longer meets the row as it stands.
        Preference.objects.filter(pk=pref.pk).update(value="hacked")
        pref.refresh_from_db()
        state = state_of(at_entry(pref, pref.first_edit), "value")
        assert not state.known and not state.restorable and not state.shown
        assert "audit log does not see" in state.reason

    def test_a_gap_between_two_logged_changes_is_caught_too(self, pref):
        first = pref.first_edit
        VantageLogEntry.objects.filter(pk=first.pk).update(
            changes={"value": {"from": "dark", "to": "LIGHT?"}}
        )
        state = state_of(at_entry(pref, first), "value")
        assert not state.known

    def test_before_the_record_was_created_says_so(self, boss):
        response = boss.post(
            reverse("vantage:testapp-preference-add"), {"key": "font", "value": "serif"}
        )
        assert response.status_code == 302
        created = Preference.objects.get(key="font")
        entry = entries_of(created).get()
        panel = site.get_panel(Preference)
        early = history.Position(entry.created_at - dt.timedelta(seconds=1))
        assert history.reconstruct(panel, created, early).status == "before_create"

    def test_before_the_history_begins_shows_nothing_rather_than_guessing(self, pref):
        # The record existed before the first entry, and nothing says what it held.
        first = entries_of(pref).order_by("created_at", "pk").first()
        panel = site.get_panel(Preference)
        early = history.Position(first.created_at - dt.timedelta(days=1))
        result = history.reconstruct(panel, pref, early)
        assert result.status == "before_history" and result.fields == []

    def test_a_record_with_no_entries_is_empty(self, boss):
        quiet = Preference.objects.create(key="quiet")
        panel = site.get_panel(Preference)
        assert history.reconstruct(panel, quiet, history.Position(timezone.now())).status == (
            "empty"
        )

    def test_fields_the_form_never_offers_are_listed_as_unrecorded(self, pref):
        # ``updated_at`` moves on every save and is never in a diff: presenting today's
        # value as the old one would be a lie with a timestamp on it.
        result = at_entry(pref, pref.first_edit)
        assert "Updated at" in result.unrecorded
        assert "updated_at" not in {state.name for state in result.fields}

    def test_a_readonly_field_is_unrecorded_because_the_form_never_writes_it(self, pref):
        panel = site.get_panel(Preference)
        panel.readonly_fields = ["key"]
        try:
            result = at_entry(pref, pref.first_edit)
        finally:
            panel.readonly_fields = None
        assert "Key" in result.unrecorded
        assert [state.name for state in result.fields] == ["value"]

    def test_a_single_relation_is_never_shown_or_restored(self, boss):
        ada = Author.objects.create(name="Ada", email="ada@example.com")
        bob = Author.objects.create(name="Bob", email="bob@example.com")
        order = Order.objects.create(number="A-1", customer=ada, status="pending", total="5.00")
        data = {"number": "A-1", "customer": ada.pk, "status": "paid", "total": "5.00"}
        assert boss.post(url(order, "change"), data).status_code == 302
        first = entries_of(order).get()
        assert boss.post(url(order, "change"), {**data, "customer": bob.pk}).status_code == 302
        order.refresh_from_db()
        state = state_of(at_entry(order, first), "customer")
        assert not state.shown and not state.restorable
        assert "label" in state.reason

    def test_a_clipped_long_text_is_shown_but_not_restorable(self, pref):
        long = "x" * (audit.MAX_VALUE_CHARS - 1) + "…"
        VantageLogEntry.objects.filter(pk=pref.first_edit.pk).update(
            changes={"value": {"from": "dark", "to": long}}
        )
        second = entries_of(pref).latest("created_at", "pk")
        VantageLogEntry.objects.filter(pk=second.pk).update(
            changes={"value": {"from": long, "to": "sepia"}}
        )
        state = state_of(at_entry(pref, pref.first_edit), "value")
        assert state.shown and not state.restorable
        assert "too long" in state.reason

    def test_a_bulk_change_since_then_withholds_fields_nothing_earlier_confirms(self, pref):
        # Logged once for the run, with no object id: it might have touched this row.
        audit.record(
            None,
            VantageLogEntry.Action.ACTION,
            model=Preference,
            action_name="bulk_edit",
            object_repr="3 records",
            changes={"key": {"to": "theme"}},
        )
        result = at_entry(pref, pref.first_edit)
        assert result.bulk_runs == 1
        key = state_of(result, "key")
        assert key.shown and not key.known and not key.restorable
        # ``value`` has an entry at or before the point that the chain still meets.
        assert state_of(result, "value").restorable


# ── sensitive values ────────────────────────────────────────────────────────


class TestSensitive:
    def test_a_sensitive_field_never_appears_then_or_now(self, boss):
        cred = ZCredential.objects.create(name="Acme", api_token="tok_live_current")
        entry = audit.record(
            None,
            VantageLogEntry.Action.UPDATE,
            obj=cred,
            # Written before the field was masked, the way an old entry would be.
            changes={
                "api_token": {"from": "tok_live_OLD", "to": "tok_live_current"},
                "name": {"from": "Acme Ltd", "to": "Acme"},
            },
        )
        response = boss.get(url(cred, "history"), {"entry": entry.pk})
        body = response.content.decode()
        assert response.status_code == 200
        assert "tok_live_OLD" not in body and "tok_live_current" not in body
        state = state_of(response.context["reconstruction"], "api_token")
        assert state.masked and not state.restorable and state.value is None

    def test_the_fingerprint_leaves_sensitive_fields_out(self, boss):
        cred = ZCredential.objects.create(name="Acme", pin="1234")
        changes = {"name": {"from": "A", "to": "Acme"}}
        entry = audit.record(None, "update", obj=cred, changes=changes)
        result = at_entry(cred, entry)
        before = history.fingerprint(result)
        ZCredential.objects.filter(pk=cred.pk).update(pin="9999")
        cred.refresh_from_db()
        assert history.fingerprint(at_entry(cred, entry)) == before


# ── the page ────────────────────────────────────────────────────────────────


class TestPage:
    def test_the_timeline_lists_every_entry_newest_first(self, boss, pref):
        response = boss.get(url(pref, "history"))
        assert response.status_code == 200
        timeline = [row["entry"] for row in response.context["timeline"]]
        assert timeline == list(entries_of(pref).order_by("-created_at", "-pk"))
        assert response.context["reconstruction"] is None

    def test_the_page_shows_then_and_now_side_by_side(self, boss, pref):
        response = boss.get(url(pref, "history"), {"entry": pref.first_edit.pk})
        body = response.content.decode()
        assert "light" in body and "sepia" in body
        assert 'name="fields" value="value"' in body

    def test_an_entry_from_another_record_is_not_a_point_in_this_ones_history(self, boss, pref):
        other = Preference.objects.create(key="other", value="v")
        stray = edit_preference(boss, other, value="w")
        response = boss.get(url(pref, "history"), {"entry": stray.pk})
        assert response.context["reconstruction"] is None
        assert "not in this record" in response.content.decode()

    def test_a_malformed_date_is_ignored_rather_than_an_error(self, boss, pref):
        assert boss.get(url(pref, "history"), {"at": "yesterday-ish"}).status_code == 200

    def test_the_detail_page_links_to_it(self, boss, pref):
        body = boss.get(url(pref, "detail")).content.decode()
        assert url(pref, "history") in body

    def test_an_unknown_record_is_a_404(self, boss):
        missing = reverse("vantage:testapp-preference-history", args=[999])
        assert boss.get(missing).status_code == 404


# ── permissions ─────────────────────────────────────────────────────────────


class TestPermissions:
    def test_reading_needs_the_audit_permission_as_well_as_view(self, client, pref):
        staff(client, "testapp.view_preference")
        assert client.get(url(pref, "history")).status_code == 403

    def test_without_view_on_the_model_it_is_refused(self, client, pref):
        staff(client, "vantage.view_vantagelogentry")
        assert client.get(url(pref, "history")).status_code == 403

    def test_view_only_sees_the_reconstruction_but_no_restore(self, client, pref):
        staff(client, "testapp.view_preference", "vantage.view_vantagelogentry")
        response = client.get(url(pref, "history"), {"entry": pref.first_edit.pk})
        assert response.status_code == 200
        assert not response.context["offer_restore"]
        assert 'name="fields"' not in response.content.decode()
        posted = client.post(url(pref, "history"), {"entry": pref.first_edit.pk, "fields": "value"})
        assert posted.status_code == 403
        pref.refresh_from_db()
        assert pref.value == "sepia"

    def test_change_permission_may_restore(self, client, pref):
        staff(
            client,
            "testapp.view_preference",
            "testapp.change_preference",
            "vantage.view_vantagelogentry",
        )
        response = restore(client, pref, pref.first_edit, ["value"])
        assert response.status_code == 302
        pref.refresh_from_db()
        assert pref.value == "light"

    def test_read_only_mode_refuses_a_restore(self, boss, pref):
        with override_settings(VANTAGE={"READ_ONLY": True}):
            response = boss.post(
                url(pref, "history"), {"entry": pref.first_edit.pk, "fields": "value"}
            )
        assert response.status_code == 403


class TestTenancy:
    @pytest.fixture
    def scoped(self, http_tenancy, tenants):
        for user in (tenants.alice, tenants.bob):
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label="vantage", codename="view_vantagelogentry"
                )
            )
        return tenants

    def test_another_tenants_record_has_no_history_page(self, client, scoped):
        client.force_login(scoped.alice)
        assert client.get(url(scoped.widget_b, "history")).status_code == 404
        assert client.post(url(scoped.widget_b, "history"), {"fields": "name"}).status_code == 404

    def test_a_restore_on_your_own_tenant_works(self, client, scoped):
        client.force_login(scoped.alice)
        widget = scoped.widget_a
        data = {"name": "Widget A2", "org": scoped.org_a.pk}
        assert client.post(url(widget, "change"), data).status_code == 302
        first = entries_of(widget).get()
        assert client.post(url(widget, "change"), {**data, "name": "A3"}).status_code == 302
        widget.refresh_from_db()
        assert restore(client, widget, first, ["name"]).status_code == 302
        widget.refresh_from_db()
        assert (widget.name, widget.org_id) == ("Widget A2", scoped.org_a.pk)


# ── restoring ───────────────────────────────────────────────────────────────


class TestRestore:
    def test_restoring_writes_the_old_value_back_and_returns_to_the_record(self, boss, pref):
        response = restore(boss, pref, pref.first_edit, ["value"])
        assert response.status_code == 302 and response["Location"] == url(pref, "detail")
        pref.refresh_from_db()
        assert pref.value == "light"
        assert any("Restored 1 field" in message for message in messages_of(response))

    def test_the_restore_writes_its_own_audit_entry(self, boss, pref):
        restore(boss, pref, pref.first_edit, ["value"])
        entry = entries_of(pref).latest("created_at", "pk")
        assert entry.action == VantageLogEntry.Action.UPDATE
        assert entry.changes == {"value": {"from": "sepia", "to": "light"}}
        assert entry.restored_as_of == pref.first_edit.created_at
        assert entry.actor.username == "boss"

    def test_a_restore_is_itself_history_that_can_be_replayed(self, boss, pref):
        restore(boss, pref, pref.first_edit, ["value"])
        pref.refresh_from_db()
        latest = entries_of(pref).latest("created_at", "pk")
        second = entries_of(pref).exclude(pk=latest.pk).latest("created_at", "pk")
        assert state_of(at_entry(pref, second), "value").value == "sepia"
        assert state_of(at_entry(pref, second), "value").restorable

    def test_only_the_ticked_fields_are_restored(self, boss):
        pref = Preference.objects.create(key="theme", value="dark")
        first = edit_preference(boss, pref, key="look", value="light")
        edit_preference(boss, pref, key="style", value="sepia")
        restore(boss, pref, first, ["key"])
        pref.refresh_from_db()
        assert (pref.key, pref.value) == ("look", "sepia")

    def test_values_come_from_the_server_never_from_the_post(self, boss, pref):
        # A hand-written POST can choose *which* fields, never what they become.
        restore(boss, pref, pref.first_edit, ["value"], value="pwned")
        pref.refresh_from_db()
        assert pref.value == "light"

    def test_a_field_that_is_not_restorable_is_ignored_when_posted(self, boss, pref):
        response = restore(boss, pref, pref.first_edit, ["updated_at", "key"])
        assert any("at least one field" in message for message in messages_of(response))
        assert entries_of(pref).count() == 2

    def test_a_record_that_moved_since_the_page_was_drawn_is_not_restored(self, boss, pref):
        page = boss.get(url(pref, "history"), {"entry": pref.first_edit.pk})
        # A colleague saves in between. Preference's ``updated_at`` is its lock.
        edit_preference(boss, pref, key="theme-2")
        response = boss.post(
            url(pref, "history"),
            {
                "entry": pref.first_edit.pk,
                "fields": ["value"],
                "version": page.context["version"],
                "fingerprint": page.context["fingerprint"],
            },
        )
        pref.refresh_from_db()
        assert (pref.key, pref.value) == ("theme-2", "sepia")
        assert any("changed" in message for message in messages_of(response))

    def test_a_stale_version_token_conflicts_inside_the_save(self, boss, pref):
        page = boss.get(url(pref, "history"), {"entry": pref.first_edit.pk})
        response = boss.post(
            url(pref, "history"),
            {
                "entry": pref.first_edit.pk,
                "fields": ["value"],
                "version": "2001-01-01T00:00:00+00:00",
                "fingerprint": page.context["fingerprint"],
            },
        )
        pref.refresh_from_db()
        assert pref.value == "sepia"
        assert any("Their change has been kept" in message for message in messages_of(response))

    def test_a_model_without_a_version_column_is_guarded_by_the_fingerprint(self, boss):
        ada = Author.objects.create(name="Ada", email="ada@example.com")
        order = Order.objects.create(number="A-1", customer=ada, status="pending", total="5.00")
        data = {"number": "A-1", "customer": ada.pk, "status": "paid", "total": "5.00"}
        boss.post(url(order, "change"), data)
        first = entries_of(order).get()
        boss.post(url(order, "change"), {**data, "status": "shipped"})
        order.refresh_from_db()
        page = boss.get(url(order, "history"), {"entry": first.pk})
        Order.objects.filter(pk=order.pk).update(total="9.00")
        boss.post(
            url(order, "history"),
            {
                "entry": first.pk,
                "fields": ["status"],
                "version": "",
                "fingerprint": page.context["fingerprint"],
            },
        )
        order.refresh_from_db()
        assert order.status == "shipped"
        assert entries_of(order).count() == 2

    def test_decimals_choices_and_datetimes_round_trip(self, boss):
        from tests.testapp.models import Release

        ada = Author.objects.create(name="Ada", email="ada@example.com")
        order = Order.objects.create(number="A-1", customer=ada, status="pending", total="5.00")
        data = {"number": "A-1", "customer": ada.pk, "status": "paid", "total": "7.50"}
        boss.post(url(order, "change"), data)
        first = entries_of(order).get()
        boss.post(url(order, "change"), {**data, "status": "shipped", "total": "12"})
        order.refresh_from_db()
        assert restore(boss, order, first, ["status", "total"]).status_code == 302
        order.refresh_from_db()
        assert (order.status, str(order.total)) == ("paid", "7.50")

        release = Release.objects.create(version="1.0")
        boss.post(url(release, "change"), {"version": "1.0", "published_at": "2026-03-01T09:30"})
        first = entries_of(release).get()
        boss.post(url(release, "change"), {"version": "1.0", "published_at": "2026-04-01T10:00"})
        release.refresh_from_db()
        assert restore(boss, release, first, ["published_at"]).status_code == 302
        release.refresh_from_db()
        assert timezone.localtime(release.published_at).strftime("%Y-%m-%d %H:%M") == (
            "2026-03-01 09:30"
        )

    def test_an_old_value_the_form_no_longer_accepts_is_reported_not_saved(self, boss, pref):
        VantageLogEntry.objects.filter(pk=pref.first_edit.pk).update(
            changes={"value": {"from": "dark", "to": "v" * 300}}
        )
        second = entries_of(pref).latest("created_at", "pk")
        VantageLogEntry.objects.filter(pk=second.pk).update(
            changes={"value": {"from": "v" * 300, "to": "sepia"}}
        )
        response = restore(boss, pref, pref.first_edit, ["value"])
        assert response.status_code == 422
        assert response.context["restore_errors"]
        pref.refresh_from_db()
        assert pref.value == "sepia"


# ── configuration ───────────────────────────────────────────────────────────


class TestConfiguration:
    def test_history_false_removes_the_page(self, boss, pref):
        panel = site.get_panel(Preference)
        panel.history = False
        try:
            assert boss.get(url(pref, "history")).status_code == 404
            assert url(pref, "history") not in boss.get(url(pref, "detail")).content.decode()
        finally:
            panel.history = None

    def test_history_view_keeps_the_page_without_restore(self, boss, pref):
        panel = site.get_panel(Preference)
        panel.history = "view"
        try:
            page = boss.get(url(pref, "history"), {"entry": pref.first_edit.pk})
            assert page.status_code == 200 and not page.context["offer_restore"]
            posted = boss.post(
                url(pref, "history"), {"entry": pref.first_edit.pk, "fields": "value"}
            )
            assert posted.status_code == 403
        finally:
            panel.history = None

    def test_the_page_follows_the_audit_log_switch(self, boss, pref):
        with override_settings(VANTAGE={"AUDIT_LOG": False}):
            assert boss.get(url(pref, "history")).status_code == 404

    @pytest.mark.parametrize("value", ["yes", 1, ["view"]])
    def test_a_bad_value_is_a_check_error(self, value):
        from vantage.panel_checks import ModelPanelChecks

        panel = site.get_panel(Archive)
        panel.history = value
        try:
            ids = [error.id for error in ModelPanelChecks().check(panel)]
        finally:
            panel.history = None
        assert "vantage.E091" in ids

    def test_asking_for_history_without_an_audit_log_is_a_check_error(self):
        from vantage.panel_checks import ModelPanelChecks

        panel = site.get_panel(Archive)
        panel.history = True
        try:
            with override_settings(VANTAGE={"AUDIT_LOG": False}):
                ids = [error.id for error in ModelPanelChecks().check(panel)]
        finally:
            panel.history = None
        assert "vantage.E092" in ids

    def test_a_valid_value_passes_the_checks(self):
        from vantage.panel_checks import ModelPanelChecks

        panel = site.get_panel(Post)
        for value in (None, True, False, "view"):
            panel.history = value
            try:
                ids = [error.id for error in ModelPanelChecks().check(panel)]
            finally:
                panel.history = None
            assert not {"vantage.E091", "vantage.E092"} & set(ids)
