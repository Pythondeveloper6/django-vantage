"""Walk every route the panel generates, for real, through the test client.

This file exists because of a specific failure mode: a view gets written, its
``path()`` entry never gets added to ``vantage.urls`` (or to one of the ``build_urls``
helpers it delegates to), and nothing notices — because every other test reaches the
view *directly* (``View.as_view()(request)`` or a plain function call) rather than
through ``reverse()`` and the test client. Thousands of passing unit tests prove the
view works. None of them prove anyone can ever reach it.

So this suite does the dumbest possible thing that actually proves that: it asks the
mounted urlconf what routes exist, builds the most plausible request it can for each
one, and fires it through ``Client.get()``. Two rules keep it honest:

**Routes are discovered, never listed.** A hand-maintained list of route names goes
stale the same way the urlconf itself does — the route added last week is exactly the
one a maintainer forgets to add to the list. ``_iter_requests`` below walks the actual
``URLResolver`` the mounted ``VantageSite`` produced, so a newly added ``path()`` entry
is exercised the moment it exists, with no line to remember to add here. The one
exception is deliberate: the seven conventional per-panel route names
(``-list``/``-add``/``-change``/``-delete``/``-detail``/``-restore``/``-prefs``) are
named explicitly in ``CONVENTIONAL_SUFFIXES`` because the contract itself — "every
panel gets these seven" — is what a missing one would violate, and asserting a
contract by name is different from re-deriving a route list by hand.

**A response is judged by its class, not its code.** 200, 302, 403, 404 and 405 are all
legitimate answers depending on who is asking and what they are asking for — a
POST-only view answering 405 to a GET is correct, not broken. What is never legitimate
is a 500, an unhandled exception, a ``NoReverseMatch``, or a page that leaks its own
template source (``{{``, ``{%`` or a multi-line ``{# #}`` comment, which — unlike a
single-line one — renders as visible text rather than vanishing; that exact bug has
reached the header of every page in this project before).

Every walk runs twice: once as a superuser, once as a staff user holding no model
permissions at all. The second pass is the one that has caught real bugs here — a
dashboard that went blank for a restricted user because a row cap was applied before
the permission filter ran, not after.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import NoReverseMatch, get_resolver, reverse
from django.urls.resolvers import URLResolver
from model_bakery import baker

from vantage.models import SavedView, VantageJob
from vantage.sites import site as vantage_site

#: The seven URL names ``vantage.urls.build()`` mints for every registered panel,
#: unconditionally. Spelled out because the contract itself is what a regression here
#: would break — see the module docstring for why this is not the same mistake as a
#: hand-written route list.
CONVENTIONAL_SUFFIXES = ("list", "add", "change", "delete", "detail", "restore", "prefs")

#: Literal template syntax that must never survive into a rendered response.
LEAK_TOKENS = ("{#", "{%", "{{")


def _vantage_resolver() -> URLResolver:
    """The URLResolver ``vantage.urls.build()`` produced.

    Found by namespace rather than by assuming a mount point (``"admin/"`` in this
    project's own ``tests/urls.py``, anything a real project chooses) — the same
    "ask the urlconf" principle the rest of this file follows.
    """
    for entry in get_resolver().url_patterns:
        if isinstance(entry, URLResolver) and entry.namespace == "vantage":
            return entry
    raise AssertionError(
        "No mounted urlconf entry carries the 'vantage' namespace — "
        "is tests.urls still wiring in vantage.site.urls?"
    )


def _prefix_pk_map(panels, instances) -> dict[str, object]:
    """``"testapp/post/"`` -> the pk of this run's one ``Post`` row, for every panel.

    Every per-panel route ``vantage.urls`` mounts is prefixed exactly this way — see
    its ``build()`` — so a route's own path text says which model's primary key it
    wants, without this test having to know the route's name.
    """
    return {f"{p.opts.app_label}/{p.opts.model_name}/": instances[p.model].pk for p in panels}


def _iter_requests(panels, instances, job, saved_view):
    """Every request this test will make: ``(description, url_name, kwargs)``.

    Walks the mounted resolver rather than naming routes, so a route added after this
    file was written is still walked. A route whose parameters this function does not
    recognise is not silently skipped — it is yielded with ``url_name=None``, which
    ``_walk`` turns into a loud, named failure telling a future maintainer exactly
    which route needs teaching.
    """
    prefix_pk = _prefix_pk_map(panels, instances)

    for entry in _vantage_resolver().url_patterns:
        if not entry.name:
            continue
        name = f"vantage:{entry.name}"
        route = str(entry.pattern)
        converters = getattr(entry.pattern, "converters", {})

        if "app_label" in converters and "model_name" in converters:
            # The JSON API is parameterised by model rather than looped per panel in
            # the urlconf itself (see vantage.api.build_urls) — so loop here instead,
            # once per registered panel, rather than firing one request that only ever
            # exercises whichever model happens to be named.
            for panel in panels:
                kwargs = {
                    "app_label": panel.opts.app_label,
                    "model_name": panel.opts.model_name,
                }
                if "pk" in converters:
                    kwargs["pk"] = instances[panel.model].pk
                yield f"{name} [{panel.opts.label_lower}]", name, kwargs
            continue

        kwargs: dict[str, object] = {}
        unresolved: list[str] = []
        for param in converters:
            if param == "pk":
                owner = next((prefix for prefix in prefix_pk if route.startswith(prefix)), None)
                if owner is not None:
                    kwargs["pk"] = prefix_pk[owner]
                elif route.startswith("_/jobs/"):
                    kwargs["pk"] = job.pk
                elif route.startswith("_/views/"):
                    kwargs["pk"] = saved_view.pk
                else:
                    unresolved.append(param)
            elif param == "token":
                # The import wizard's session token. Deliberately not a real one: an
                # expired/unknown token is a supported, non-crashing outcome (a
                # redirect back to step one), and exercising that path is as valid a
                # smoke check as a happy one.
                kwargs["token"] = "smoke-test-token"
            else:
                unresolved.append(param)

        if unresolved:
            yield f"{name} [{route}]", None, unresolved
        else:
            yield name, name, kwargs


def _walk(client, requests, *, who: str, failures: list[str]) -> None:
    """Fire every request in ``requests`` and append a description of anything that
    must never happen to ``failures``: a route this test cannot build, a name that
    does not reverse, an unhandled exception, a 500, or leaked template syntax."""
    for label, name, kwargs in requests:
        if name is None:
            failures.append(
                f"{label}: this test does not know how to fill parameter(s) {kwargs} — "
                f"teach _iter_requests about it"
            )
            continue

        try:
            url = reverse(name, kwargs=kwargs)
        except NoReverseMatch as exc:
            failures.append(f"{label} ({who}): does not reverse — {exc!r}")
            continue

        try:
            response = client.get(url)
        except Exception as exc:  # a crashing route is exactly what this test hunts for
            failures.append(f"{label} ({who}): GET {url} raised {type(exc).__name__}: {exc}")
            continue

        if response.status_code >= 500:
            failures.append(f"{label} ({who}): GET {url} returned {response.status_code}")
            continue

        content_type = response.get("Content-Type", "")
        if content_type.startswith("text/html"):
            body = response.content.decode("utf-8", errors="replace")
            leaked = [token for token in LEAK_TOKENS if token in body]
            if leaked:
                failures.append(
                    f"{label} ({who}): GET {url} leaked template syntax {leaked} "
                    f"into the rendered page"
                )


@pytest.fixture
def world(db):
    """One instance of every registered model, two users, and the two extra rows that
    live outside the panel registry but still have their own routes — a background
    job and a saved view, both in ``vantage.registry.DEFAULT_EXCLUDE``.

    Built fresh inside pytest-django's per-test transaction, so nothing here survives
    into another test and nothing here depends on one having run first.
    """
    user_model = get_user_model()
    superuser = user_model.objects.create_superuser("smoke-super", "smoke-super@example.com", "x")
    # Staff, and deliberately nothing else: no permission is granted. This is the
    # account that has caught real bugs — see the module docstring.
    staff = user_model.objects.create_user("smoke-staff", password="x", is_staff=True)

    panels = list(vantage_site.panels())
    instances = {panel.model: baker.make(panel.model) for panel in panels}

    # Whichever panel sorts first is as good as any other; the point is a real,
    # registered model label, not a specific one.
    anchor = panels[0].opts.label_lower
    job = baker.make(
        VantageJob,
        actor=superuser,
        resource_key=anchor,
        action_name="smoke",
        status=VantageJob.Status.SUCCEEDED,
    )
    saved_view = baker.make(
        SavedView,
        model_label=anchor,
        owner=superuser,
        visibility=SavedView.Visibility.SHARED,
        edit_scope=SavedView.EditScope.EVERYONE,
    )

    return {
        "panels": panels,
        "instances": instances,
        "superuser": superuser,
        "staff": staff,
        "job": job,
        "saved_view": saved_view,
    }


class TestConventionalPanelRoutes:
    """The seven names ``vantage.urls`` promises every registered panel."""

    def test_every_panel_carries_its_seven_conventional_routes(self, world):
        failures = []
        for panel in world["panels"]:
            pk = world["instances"][panel.model].pk
            for suffix in CONVENTIONAL_SUFFIXES:
                name = f"vantage:{panel.slug}-{suffix}"
                needs_pk = suffix in ("change", "delete", "restore", "detail")
                try:
                    reverse(name, kwargs={"pk": pk} if needs_pk else {})
                except NoReverseMatch as exc:
                    failures.append(f"{name}: does not reverse — {exc!r}")
        assert not failures, "\n".join(failures)


class TestPanelListPages:
    """Assert every registered panel has its list route reachable."""

    def test_every_registered_panel_list_page_is_reachable(self, world, client):
        client.force_login(world["superuser"])
        failures = []
        for panel in world["panels"]:
            url = reverse(panel.list_url_name)
            response = client.get(url)
            if response.status_code != 200:
                failures.append(
                    f"{panel.list_url_name}: expected 200 for a superuser, "
                    f"got {response.status_code}"
                )
        assert not failures, "\n".join(failures)


class TestFullRouteWalk:
    """Every route the mounted resolver knows about, fetched for real, twice."""

    def test_as_a_superuser(self, world, client):
        client.force_login(world["superuser"])
        requests = list(
            _iter_requests(world["panels"], world["instances"], world["job"], world["saved_view"])
        )
        failures: list[str] = []
        _walk(client, requests, who="superuser", failures=failures)
        assert not failures, f"{len(failures)} route(s) failed:\n" + "\n".join(failures)

    def test_as_a_staff_user_with_no_model_permissions(self, world, client):
        """No grant of any kind — the pass that has caught real bugs.

        A route may legitimately answer 403 (or 404, if it hides existence rather than
        confirming and refusing it) here; it must never answer 500, raise, or render a
        page with its own template syntax leaking through.
        """
        client.force_login(world["staff"])
        requests = list(
            _iter_requests(world["panels"], world["instances"], world["job"], world["saved_view"])
        )
        failures: list[str] = []
        _walk(client, requests, who="restricted staff", failures=failures)
        assert not failures, f"{len(failures)} route(s) failed:\n" + "\n".join(failures)


class TestTargetedRegressions:
    """Two bugs that landed while this file was stalled, neither of which the plain GET
    walk above would have caught: one needs a POST body, the other a query string.

    ``TestFullRouteWalk`` only ever fires a bare ``GET`` on each reversed route, so a
    view that only misbehaves once it is *given something to act on* sails through it
    clean. These two tests exist because both bugs shipped exactly that way.
    """

    def test_saving_the_topbar_layout_does_not_500(self, world, client):
        """``vantage.topbar.save_layout`` used to raise ``LookupError`` unconditionally
        before migration 0009 added ``NavConfiguration.topbar`` — so the settings
        screen's save button 500'd on first use, and no GET-only test could have caught
        it: the route only fails once something is actually saved to it. Migration 0009
        is what fixes this; this test is what would have caught it not being applied,
        or the column being dropped again by a future migration.
        """
        client.force_login(world["superuser"])
        url = reverse("vantage:topbar")
        response = client.post(url, {"banner": "on", "visible": ["search", "theme", "user"]})
        assert response.status_code in (200, 302), (
            f"POST {url} (saving the top bar layout) returned {response.status_code}; "
            "expected a redirect back to the same page. This is the exact shape of the "
            "LookupError regression migration 0009 fixed — see vantage.topbar.save_layout."
        )
        # Follow through and confirm the page that renders the saved arrangement is
        # itself clean, not just that the write didn't raise.
        follow = client.get(url)
        assert follow.status_code == 200
        body = follow.content.decode("utf-8", errors="replace")
        leaked = [token for token in LEAK_TOKENS if token in body]
        assert not leaked, f"GET {url} leaked template syntax {leaked} after a save"

    def test_api_fields_param_refuses_unknown_names_with_400(self, world, client):
        """``?fields=`` used to be split on commas and walked with ``resolve_path``,
        which knows Django's whole ``_meta`` graph rather than this panel's own
        registry — so ``?fields=customer__api_token`` (or any other relation path the
        panel never registered) returned it. It now must be refused outright, with a
        400 naming the field as unknown rather than a crash or, worse, silent data.
        """
        client.force_login(world["superuser"])
        panel = world["panels"][0]
        url = reverse(
            "vantage:api-list",
            kwargs={"app_label": panel.opts.app_label, "model_name": panel.opts.model_name},
        )
        response = client.get(url, {"fields": "not-a-real-field__nested__path"})
        assert response.status_code == 400, (
            f"GET {url}?fields=not-a-real-field__nested__path returned "
            f"{response.status_code}, not 400 — an unknown/relation field name in "
            "?fields= must be refused, not walked or silently dropped."
        )
        assert response["Content-Type"].startswith("application/json"), (
            "the API's error responses must be JSON, not an HTML error page"
        )
