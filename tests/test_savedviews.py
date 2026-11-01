"""Saved views: what they remember, who may change them, and what they must never do.

Four properties carry the feature, and each has a test named after the failure it
prevents rather than after the code it calls.

**A shared view is never autosaved.** Airtable's whole personal/locked feature line
exists because collaborative views autosave and one person's filter silently changed
everyone's screen.

**Save degrades to Save-as when the view is immutable to you.** One rule covering four
different permission situations, so nobody has to learn which one they are in.

**A renamed field renders broken rather than being dropped.** Silently dropping a
predicate shows people more rows than they asked for and tells them nothing.

**A saved view cannot widen access.** The stored query string is user data; the rows
are filtered by the permission layer on every request, after hydration.
"""

import pytest
from django.contrib.auth.models import Group, User
from django.test import RequestFactory
from django.urls import path, reverse

from tests.testapp.models import Author, Order
from vantage import auditviews, savedviews
from vantage.models import SavedView, SavedViewGrant, SavedViewUserState
from vantage.permissions import PanelPermissions
from vantage.sites import site
from vantage.urls import build

# Mounted from the same ``build_urls`` helpers the real urlconf is asked to call, so
# the routes these tests exercise are the routes that will ship.
urlpatterns = [
    path(
        "admin/",
        (
            build(site) + savedviews.build_urls(site) + auditviews.build_urls(site),
            "vantage",
            "vantage",
        ),
    )
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def owner():
    return User.objects.create_superuser("owner", "owner@example.com", "x")


@pytest.fixture
def colleague():
    return User.objects.create_superuser("colleague", "c@example.com", "x")


@pytest.fixture
def as_owner(client, owner):
    client.force_login(owner)
    return client


@pytest.fixture
def order_panel():
    return site.get_panel(Order)


@pytest.fixture
def orders():
    customer = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    return [
        Order.objects.create(
            number=f"SO-{index}",
            customer=customer,
            status=Order.Status.PAID if index % 2 else Order.Status.PENDING,
            total=index * 10,
        )
        for index in range(1, 5)
    ]


def make_view(owner, model=Order, **kwargs):
    defaults = {
        "name": "Paid orders",
        "model_label": model._meta.label_lower,
        "owner": owner,
        "state": {"version": 1, "filters": {"status": ["paid"]}, "q": "", "sort": ""},
    }
    defaults.update(kwargs)
    return SavedView.objects.create(**defaults)


def request_as(user, path="/admin/testapp/order/", data=None):
    request = RequestFactory().get(path, data or {})
    request.user = user
    return request


def list_url(model=Order):
    return reverse(f"vantage:{model._meta.label_lower.replace('.', '-')}-list")


# ── round trip ──────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_capture_keeps_only_what_the_panels_filters_own(self, order_panel, owner):
        request = request_as(
            owner,
            data={
                "status": "paid",
                "q": "SO",
                "sort": "-total",
                "page": "3",
                "export": "csv",
                "nonsense": "1",
            },
        )
        state = savedviews.capture(order_panel, request.GET)

        assert state["filters"] == {"status": ["paid"]}
        assert state["q"] == "SO"
        assert state["sort"] == "-total"
        # Page and export are about this visit, not about this view. Storing them
        # would make "reload my view" reopen page three of a CSV download.
        assert "page" not in state["filters"]
        assert "nonsense" not in state["filters"]

    def test_a_view_round_trips_its_filters_through_the_url(self, as_owner, owner, orders):
        view = make_view(owner)

        response = as_owner.get(savedviews.apply_url(view))
        assert response.status_code == 302
        assert f"view={view.pk}" in response["Location"]
        assert "status=paid" in response["Location"]

        body = as_owner.get(response["Location"]).content.decode()
        assert "SO-1" in body  # paid
        assert "SO-2" not in body  # pending

    def test_saving_from_the_list_page_stores_the_current_filters(self, as_owner, owner, orders):
        response = as_owner.post(
            reverse("vantage:saved-views"),
            {
                "intent": "save_as",
                "model": "testapp.order",
                "name": "Paid only",
                "next": f"{list_url()}?status=paid&sort=-total&page=2",
            },
        )
        assert response.status_code == 302

        view = SavedView.objects.get(name="Paid only")
        assert view.state["filters"] == {"status": ["paid"]}
        assert view.state["sort"] == "-total"
        assert view.owner == owner
        # The state is re-derived from the URL, never taken from the POST body.
        assert "page" not in view.state["filters"]

    def test_a_relative_date_is_stored_as_a_duration_not_a_resolved_date(self, order_panel, owner):
        """So "last 7 days" still means the last seven days a month from now."""
        state = savedviews.capture(order_panel, request_as(owner, data={"created_at": "P7D"}).GET)
        assert state["filters"]["created_at"] == ["P7D"]

    def test_hydrating_produces_the_query_string_the_list_page_reads(self, order_panel, owner):
        view = make_view(
            owner, state={"filters": {"status": ["paid", "pending"]}, "sort": "-total"}
        )
        hydrated = savedviews.hydrate(order_panel, view.state)

        assert "status=paid" in hydrated.query
        assert "status=pending" in hydrated.query
        assert "sort=-total" in hydrated.query
        assert hydrated.broken == []


# ── autosave ────────────────────────────────────────────────────────────────


class TestAutosave:
    """The rule that the rest of the sharing model rests on."""

    def test_a_shared_view_is_never_autosaved(self, order_panel, owner, orders):
        view = make_view(
            owner,
            visibility=SavedView.Visibility.SHARED,
            edit_scope=SavedView.EditScope.EVERYONE,
        )
        before = dict(view.state)

        wrote = savedviews.autosave(request_as(owner), order_panel, view, {"status": ["pending"]})

        assert wrote is False
        view.refresh_from_db()
        assert view.state == before
        assert view.version == 1

    def test_a_shared_view_is_not_autosaved_even_for_its_own_owner(self, order_panel, owner):
        """The owner is exactly the person whose habits would land on other screens."""
        view = make_view(owner, visibility=SavedView.Visibility.SHARED)
        assert view.autosaves is False
        assert (
            savedviews.autosave(request_as(owner), order_panel, view, {"status": ["pending"]})
            is False
        )

    def test_a_locked_view_is_never_autosaved(self, order_panel, owner):
        view = make_view(owner, locked=True)
        assert view.autosaves is False
        assert (
            savedviews.autosave(request_as(owner), order_panel, view, {"status": ["pending"]})
            is False
        )

    def test_a_private_owner_only_view_does_autosave(self, order_panel, owner):
        view = make_view(owner)
        assert view.autosaves is True

        assert savedviews.autosave(request_as(owner), order_panel, view, {"status": ["pending"]})

        view.refresh_from_db()
        assert view.state["filters"] == {"status": ["pending"]}
        assert view.version == 2

    def test_autosave_does_not_write_when_nothing_changed(self, order_panel, owner):
        view = make_view(owner)
        assert (
            savedviews.autosave(request_as(owner), order_panel, view, {"status": ["paid"]}) is False
        )
        view.refresh_from_db()
        assert view.version == 1

    def test_opening_a_shared_view_and_changing_a_filter_leaves_it_alone(
        self, client, owner, colleague, orders
    ):
        """The end-to-end version, through the real list page."""
        view = make_view(
            owner,
            visibility=SavedView.Visibility.SHARED,
            edit_scope=SavedView.EditScope.EVERYONE,
        )
        client.force_login(colleague)

        client.get(f"{list_url()}?view={view.pk}&status=pending")

        view.refresh_from_db()
        assert view.state["filters"] == {"status": ["paid"]}
        assert view.version == 1


# ── save / save as ──────────────────────────────────────────────────────────


class TestSaveDegradesToSaveAs:
    def test_save_for_the_owner_of_an_unlocked_private_view(self, owner):
        view = make_view(owner)
        assert savedviews.save_mode(view, request_as(owner)) == "save"

    def test_save_as_for_someone_elses_view(self, owner, colleague):
        view = make_view(owner, visibility=SavedView.Visibility.SHARED)
        assert savedviews.save_mode(view, request_as(colleague)) == "save_as"

    def test_save_as_for_a_locked_view_even_for_its_creator(self, owner):
        """Locking freezes the configuration and binds the creator too."""
        view = make_view(owner, locked=True)
        assert savedviews.save_mode(view, request_as(owner)) == "save_as"

    def test_save_as_for_a_viewer_grant_and_save_for_an_editor_grant(self, owner, colleague):
        view = make_view(
            owner,
            visibility=SavedView.Visibility.SHARED,
            edit_scope=SavedView.EditScope.EDITORS,
        )
        grant = SavedViewGrant.objects.create(
            view=view, user=colleague, role=SavedViewGrant.Role.VIEWER
        )
        assert savedviews.save_mode(view, request_as(colleague)) == "save_as"

        grant.role = SavedViewGrant.Role.EDITOR
        grant.save()
        assert savedviews.save_mode(view, request_as(colleague)) == "save"

    def test_a_group_grant_works_the_same_way(self, owner, colleague):
        editors = Group.objects.create(name="Editors")
        colleague.groups.add(editors)
        view = make_view(
            owner,
            visibility=SavedView.Visibility.SHARED,
            edit_scope=SavedView.EditScope.EDITORS,
        )
        SavedViewGrant.objects.create(view=view, group=editors, role=SavedViewGrant.Role.EDITOR)
        assert savedviews.save_mode(view, request_as(colleague)) == "save"

    def test_save_as_when_no_view_is_open(self, owner):
        assert savedviews.save_mode(None, request_as(owner)) == "save_as"

    def test_a_stale_page_that_posts_save_on_an_immutable_view_is_refused(
        self, client, owner, colleague, orders
    ):
        view = make_view(owner, locked=True, visibility=SavedView.Visibility.SHARED)
        client.force_login(colleague)

        response = client.post(
            reverse("vantage:saved-views"),
            {"intent": "save", "view": view.pk, "next": f"{list_url()}?status=pending"},
        )

        assert response.status_code == 403
        view.refresh_from_db()
        assert view.state["filters"] == {"status": ["paid"]}

    def test_the_strip_offers_save_as_when_the_view_is_immutable(
        self, client, owner, colleague, orders
    ):
        view = make_view(owner, locked=True, visibility=SavedView.Visibility.SHARED)
        client.force_login(colleague)

        context = savedviews.list_context(
            request_as(colleague, data={"view": str(view.pk), "status": "pending"}),
            site.get_panel(Order),
        )
        assert context["saved_view_mode"] == "save_as"
        assert context["saved_view_can_save"] is False


# ── optimistic locking ──────────────────────────────────────────────────────


class TestOptimisticLocking:
    def test_a_stale_version_is_refused_rather_than_overwriting(self, client, owner, orders):
        view = make_view(
            owner,
            visibility=SavedView.Visibility.SHARED,
            edit_scope=SavedView.EditScope.EVERYONE,
        )
        # Somebody else saved in the meantime.
        SavedView.objects.filter(pk=view.pk).update(
            version=2, state={"filters": {"status": ["refunded"]}}
        )
        client.force_login(owner)

        response = client.post(
            reverse("vantage:saved-views"),
            {
                "intent": "save",
                "view": view.pk,
                "version": "1",
                "next": f"{list_url()}?status=pending",
            },
            follow=True,
        )

        view.refresh_from_db()
        assert view.state["filters"] == {"status": ["refunded"]}
        assert "changed by someone else" in response.content.decode()

    def test_the_current_version_saves_and_bumps(self, client, owner, orders):
        view = make_view(
            owner,
            visibility=SavedView.Visibility.SHARED,
            edit_scope=SavedView.EditScope.EVERYONE,
        )
        client.force_login(owner)

        client.post(
            reverse("vantage:saved-views"),
            {
                "intent": "save",
                "view": view.pk,
                "version": str(view.version),
                "next": f"{list_url()}?status=pending",
            },
        )

        view.refresh_from_db()
        assert view.state["filters"] == {"status": ["pending"]}
        assert view.version == 2


# ── broken references ───────────────────────────────────────────────────────


class TestBrokenReferencesAreRenderedNotDropped:
    def test_a_renamed_field_renders_broken(self, order_panel, owner):
        view = make_view(owner, state={"filters": {"stauts_renamed": ["paid"]}})

        hydrated = savedviews.hydrate(order_panel, view.state)

        assert hydrated.query == ""
        assert len(hydrated.broken) == 1
        broken = hydrated.broken[0]
        assert broken.parameter == "stauts_renamed"
        assert broken.values == ["paid"]
        assert "no longer exists" in broken.reason
        assert hydrated.widened is True

    def test_a_field_that_is_no_longer_a_filter_says_so_differently(self, order_panel, owner):
        """A schema change and a panel edit read identically to the code and not at
        all to the person, so they get different sentences."""
        view = make_view(owner, state={"filters": {"number": ["SO-1"]}})
        broken = savedviews.hydrate(order_panel, view.state).broken[0]
        assert "no longer one of this page's filters" in broken.reason

    def test_the_surviving_predicates_are_still_applied(self, order_panel, owner):
        view = make_view(owner, state={"filters": {"status": ["paid"], "gone_away": ["1"]}})
        hydrated = savedviews.hydrate(order_panel, view.state)
        assert "status=paid" in hydrated.query
        assert len(hydrated.broken) == 1

    def test_a_stale_sort_is_reported_without_the_breadth_warning(self, order_panel, owner):
        view = make_view(owner, state={"filters": {}, "sort": "gone_away"})
        hydrated = savedviews.hydrate(order_panel, view.state)
        assert hydrated.broken[0].widens is False
        assert hydrated.widened is False

    def test_the_banner_warns_that_results_may_be_broader_than_intended(
        self, client, owner, orders
    ):
        from django.template.loader import render_to_string

        view = make_view(owner, state={"filters": {"gone_away": ["1"]}})
        client.force_login(owner)

        context = savedviews.list_context(
            request_as(owner, data={"view": str(view.pk)}), site.get_panel(Order)
        )
        html = render_to_string(
            "vantage/partials/saved_views.html", {**context, "request": request_as(owner)}
        )

        assert "broader than intended" in html
        # The pill is rendered, struck through, never removed.
        assert "gone_away" in html
        assert "v-pill-broken" in html

    def test_validation_happens_on_load_not_on_save(self, client, owner, orders):
        """Saving a view whose fields all exist must not be the moment they are checked.

        The field is renamed *after* the view is written, which is the only order in
        which this ever happens in real life — so a save-time check would have passed
        and the load-time check is the one that has to catch it.
        """
        client.force_login(owner)
        client.post(
            reverse("vantage:saved-views"),
            {
                "intent": "save_as",
                "model": "testapp.order",
                "name": "Paid",
                "next": f"{list_url()}?status=paid",
            },
        )
        view = SavedView.objects.get(name="Paid")

        # The schema moves under it.
        view.state = {"filters": {"status_renamed": ["paid"]}}
        view.save()

        assert savedviews.hydrate(site.get_panel(Order), view.state).broken


# ── permissions ─────────────────────────────────────────────────────────────


class PaidOrdersOnly(PanelPermissions):
    """Row scoping, so the "cannot widen access" claim has something to widen past.

    ``superuser_bypass`` is off because these fixtures are superusers and the point of
    the test is the queryset-level narrowing, not the bypass.
    """

    superuser_bypass = False

    def has_model_perm(self, request, model, action):
        return bool(getattr(request, "user", None) and request.user.is_authenticated)

    def has_object_perm(self, request, model, action, obj):
        return self.has_model_perm(request, model, action)

    def filter_queryset(self, request, queryset, action="view"):
        if queryset.model is Order:
            return queryset.filter(status=Order.Status.PAID)
        return queryset


class TestAViewCannotWidenAccess:
    def test_row_permissions_are_re_applied_after_hydrating(self, client, colleague, orders):
        """URL filter state has zero security value. This is the test that says so.

        The view stores *no* filters at all, so everything it shows comes from the
        permission layer — which is exactly the case a stored query string could
        widen if the layer were skipped after hydration.
        """
        view = make_view(
            owner=None,
            name="Everything",
            state={"filters": {}, "q": "", "sort": ""},
            visibility=SavedView.Visibility.SHARED,
        )

        client.force_login(colleague)
        with _permissions(PaidOrdersOnly):
            body = client.get(savedviews.apply_url(view), follow=True).content.decode()

        assert "SO-1" in body  # paid
        assert "SO-3" in body  # paid
        assert "SO-2" not in body  # pending, and never reachable through a view
        assert "SO-4" not in body

    def test_a_view_for_a_model_you_cannot_see_is_not_offered(self, client, colleague, owner):
        make_view(owner, visibility=SavedView.Visibility.SHARED)
        plain = User.objects.create_user("plain", password="x", is_staff=True)

        assert savedviews.visible_views(request_as(plain), site.get_panel(Order)).count() == 0

    def test_applying_a_view_for_a_model_you_cannot_see_is_refused(self, client, owner):
        view = make_view(owner, visibility=SavedView.Visibility.SHARED)
        plain = User.objects.create_user("plain", password="x", is_staff=True)
        client.force_login(plain)

        assert client.get(savedviews.apply_url(view)).status_code == 403

    def test_a_private_view_is_not_visible_to_anyone_else(self, owner, colleague):
        make_view(owner)
        assert savedviews.visible_views(request_as(colleague), site.get_panel(Order)).count() == 0
        assert savedviews.visible_views(request_as(owner), site.get_panel(Order)).count() == 1

    def test_a_grant_makes_a_private_view_reachable(self, owner, colleague):
        view = make_view(owner)
        SavedViewGrant.objects.create(view=view, user=colleague)
        assert savedviews.can_use(view, request_as(colleague)) is True
        assert savedviews.visible_views(request_as(colleague), site.get_panel(Order)).count() == 1

    def test_applying_a_private_view_you_were_not_granted_is_refused(
        self, client, owner, colleague
    ):
        view = make_view(owner)
        client.force_login(colleague)
        assert client.get(savedviews.apply_url(view)).status_code == 403


# ── divergence ──────────────────────────────────────────────────────────────


class TestDivergence:
    def test_a_pristine_url_does_not_read_as_diverged(self, owner, orders):
        view = make_view(owner)
        context = savedviews.list_context(
            request_as(owner, data={"view": str(view.pk), "status": "paid"}),
            site.get_panel(Order),
        )
        assert context["saved_view_diverged"] is False

    def test_value_order_within_one_filter_is_not_divergence(self, owner, orders):
        view = make_view(owner, state={"filters": {"status": ["paid", "pending"]}})
        request = RequestFactory().get(
            f"/admin/testapp/order/?view={view.pk}&status=pending&status=paid"
        )
        request.user = owner
        assert (
            savedviews.list_context(request, site.get_panel(Order))["saved_view_diverged"] is False
        )

    def test_a_changed_filter_reads_as_diverged(self, owner, orders):
        view = make_view(owner, visibility=SavedView.Visibility.SHARED)
        context = savedviews.list_context(
            request_as(owner, data={"view": str(view.pk), "status": "pending"}),
            site.get_panel(Order),
        )
        assert context["saved_view_diverged"] is True

    def test_the_f_parameter_marks_a_url_as_diverged(self, owner, orders):
        view = make_view(owner, visibility=SavedView.Visibility.SHARED)
        context = savedviews.list_context(
            request_as(owner, data={"view": str(view.pk), "status": "paid", "f": "status=paid"}),
            site.get_panel(Order),
        )
        assert context["saved_view_diverged"] is True

    def test_an_override_is_expanded_by_the_apply_route(self, as_owner, owner, orders):
        view = make_view(owner)
        response = as_owner.get(f"{savedviews.apply_url(view)}?f=status%3Dpending")

        location = response["Location"]
        assert "status=pending" in location
        assert "status=paid" not in location
        # The marker survives, so the page it lands on reads as diverged.
        assert "f=" in location

    def test_an_override_cannot_smuggle_in_a_parameter_the_filters_do_not_own(
        self, as_owner, owner, orders
    ):
        view = make_view(owner)
        response = as_owner.get(f"{savedviews.apply_url(view)}?f=nonsense%3D1")
        assert "nonsense" not in response["Location"].split("&f=")[0]

    def test_resetting_returns_to_the_saved_state(self, as_owner, owner, orders):
        view = make_view(owner, visibility=SavedView.Visibility.SHARED)
        context = savedviews.list_context(
            request_as(owner, data={"view": str(view.pk), "status": "pending"}),
            site.get_panel(Order),
        )
        assert context["saved_view_reset_url"] == savedviews.apply_url(view)


# ── the rest of the write endpoint ──────────────────────────────────────────


class TestWriteEndpoint:
    def test_deleting_a_view_drops_the_view_parameter_from_the_redirect(
        self, as_owner, owner, orders
    ):
        view = make_view(owner)
        response = as_owner.post(
            reverse("vantage:saved-views"),
            {"intent": "delete", "view": view.pk, "next": f"{list_url()}?view={view.pk}"},
        )
        assert not SavedView.objects.exists()
        assert "view=" not in response["Location"]

    def test_you_cannot_delete_someone_elses_view(self, client, owner, colleague):
        view = make_view(owner, visibility=SavedView.Visibility.SHARED)
        client.force_login(colleague)
        response = client.post(
            reverse("vantage:saved-views"),
            {"intent": "delete", "view": view.pk, "next": list_url()},
        )
        assert response.status_code == 403
        assert SavedView.objects.filter(pk=view.pk).exists()

    def test_an_editor_may_change_a_view_but_not_delete_it(self, client, owner, colleague):
        """Deleting takes everyone's defaults and report subscriptions with it."""
        view = make_view(
            owner,
            visibility=SavedView.Visibility.SHARED,
            edit_scope=SavedView.EditScope.EVERYONE,
        )
        client.force_login(colleague)
        assert savedviews.can_edit(view, request_as(colleague))

        response = client.post(
            reverse("vantage:saved-views"),
            {"intent": "delete", "view": view.pk, "next": list_url()},
        )
        assert response.status_code == 403
        assert SavedView.objects.filter(pk=view.pk).exists()

    @pytest.mark.parametrize(
        "target", ["https://evil.example/", "//evil.example/x", "http://evil.example/?view=1"]
    )
    def test_next_cannot_send_the_person_off_site(self, as_owner, owner, target):
        view = make_view(owner)
        for intent in ("default", "clear_default", "delete"):
            response = as_owner.post(
                reverse("vantage:saved-views"),
                {"intent": intent, "view": view.pk, "next": target},
            )
            assert response.status_code == 302
            assert "evil.example" not in response["Location"]
            assert response["Location"].startswith("/")

    def test_making_a_view_default_is_personal(self, client, owner, colleague):
        view = make_view(owner, visibility=SavedView.Visibility.SHARED)
        client.force_login(colleague)
        client.post(
            reverse("vantage:saved-views"),
            {"intent": "default", "view": view.pk, "next": list_url()},
        )

        state = SavedViewUserState.objects.get(view=view, user=colleague)
        assert state.is_default is True
        # Nobody else's screen moved.
        assert not SavedViewUserState.objects.filter(user=owner).exists()
        assert savedviews.default_view_for(request_as(colleague), site.get_panel(Order)) == view
        assert savedviews.default_view_for(request_as(owner), site.get_panel(Order)) is None

    def test_only_one_default_per_person_per_model(self, as_owner, owner):
        first = make_view(owner, name="First")
        second = make_view(owner, name="Second")
        request = request_as(owner)
        savedviews.set_default(request, first)
        savedviews.set_default(request, second)

        assert SavedViewUserState.objects.filter(user=owner, is_default=True).count() == 1
        assert savedviews.default_view_for(request, site.get_panel(Order)) == second

    def test_an_unknown_intent_is_refused(self, as_owner):
        response = as_owner.post(
            reverse("vantage:saved-views"), {"intent": "drop_table", "next": list_url()}
        )
        assert response.status_code == 403

    def test_saving_without_a_name_says_so_rather_than_creating_a_blank(self, as_owner):
        response = as_owner.post(
            reverse("vantage:saved-views"),
            {"intent": "save_as", "model": "testapp.order", "name": "  ", "next": list_url()},
            follow=True,
        )
        assert not SavedView.objects.exists()
        assert "needs a name" in response.content.decode()

    def test_saving_over_your_own_name_replaces_rather_than_failing(self, as_owner, owner):
        payload = {
            "intent": "save_as",
            "model": "testapp.order",
            "name": "Mine",
            "next": f"{list_url()}?status=paid",
        }
        as_owner.post(reverse("vantage:saved-views"), payload)
        as_owner.post(
            reverse("vantage:saved-views"), {**payload, "next": f"{list_url()}?status=pending"}
        )

        view = SavedView.objects.get(name="Mine")
        assert view.state["filters"] == {"status": ["pending"]}

    def test_two_people_may_use_the_same_name(self, client, owner, colleague):
        make_view(owner, name="Mine")
        client.force_login(colleague)
        client.post(
            reverse("vantage:saved-views"),
            {
                "intent": "save_as",
                "model": "testapp.order",
                "name": "Mine",
                "next": f"{list_url()}?status=pending",
            },
        )
        assert SavedView.objects.filter(name="Mine").count() == 2


# ── the strip ───────────────────────────────────────────────────────────────


class TestStrip:
    def test_the_partial_renders_nothing_without_the_context(self):
        from django.template.loader import render_to_string

        assert render_to_string("vantage/partials/saved_views.html", {}).strip() == ""

    def test_the_strip_lists_the_views_this_person_may_open(self, owner, colleague, orders):
        from django.template.loader import render_to_string

        make_view(owner, name="Mine")
        make_view(colleague, name="Theirs")
        make_view(colleague, name="Shared", visibility=SavedView.Visibility.SHARED)

        request = request_as(owner)
        context = savedviews.list_context(request, site.get_panel(Order))
        html = render_to_string(
            "vantage/partials/saved_views.html", {**context, "request": request}
        )

        assert "Mine" in html
        assert "Shared" in html
        assert "Theirs" not in html

    def test_the_strip_is_a_no_op_when_the_routes_are_not_mounted(self, owner, orders):
        from unittest import mock

        from django.urls import NoReverseMatch

        with mock.patch("vantage.savedviews.reverse", side_effect=NoReverseMatch):
            assert savedviews.list_context(request_as(owner), site.get_panel(Order)) == {}


# ── helpers ─────────────────────────────────────────────────────────────────


def _permissions(cls):
    """Point ``VANTAGE["PERMISSIONS"]`` at a class for the duration of a block."""
    from django.test import override_settings

    return override_settings(VANTAGE={"PERMISSIONS": f"{cls.__module__}.{cls.__qualname__}"})
