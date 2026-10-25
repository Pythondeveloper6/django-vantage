"""Plugins: what ``pip install vantage-something`` is allowed to do to a project.

The tests that matter are the failure ones. A plugin system whose happy path works is
easy; the question is whether a broken third-party package can stop ``manage.py`` from
running, whether a second ``ready()`` duplicates everything, and whether a project can
switch a plugin off without uninstalling it.

Entry points are simulated with a fake ``EntryPoint`` rather than by installing a
distribution: the code under test is what Vantage does with what it discovers, and a
real ``pip install`` inside a test suite would be testing ``importlib.metadata``.
"""

from types import SimpleNamespace

import pytest
from django.test import override_settings

from tests.testapp.models import Archive
from vantage import plugins, slots
from vantage.actions import Action, resolve_actions
from vantage.pages import Page
from vantage.panels import ModelPanel
from vantage.sites import VantageSite


class FakeEntryPoint:
    """Stands in for ``importlib.metadata.EntryPoint``: a name, a value, a ``load``."""

    def __init__(self, name, target, value=None):
        self.name = name
        self.target = target
        self.value = value or f"fake_pkg:{getattr(target, '__name__', target)}"

    def load(self):
        if isinstance(self.target, Exception):
            raise self.target
        return self.target


@pytest.fixture
def entry_points(monkeypatch):
    """Install a set of fake entry points for the duration of one test."""

    def install(*points):
        monkeypatch.setattr(plugins, "_entry_points", lambda group: list(points))

    return install


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    plugins.reset()
    slots.clear()
    yield
    plugins.reset()
    slots.clear()


@pytest.fixture
def site():
    """A throwaway site, so a plugin's registrations never leak into the real one."""
    return VantageSite(name="plugin-test")


# ── the plugins used below ──────────────────────────────────────────────────


class QueuePage(Page):
    title = "Queue"
    path = "_/queue/"

    def get_context(self, request):
        return {"depth": 0}


def retry_all(panel, request, queryset):
    return f"Retried {queryset.count()}."


class QueuePlugin(plugins.VantagePlugin):
    """What a real plugin looks like: a page, an action, a slot contribution."""

    name = "queue"
    version = "1.2.0"

    def register(self, site):
        site.register_page(QueuePage)
        panel = site.get_panel(Archive)
        panel.actions = [
            *(panel.actions or []),
            Action(func=retry_all, name="retry_all", label="Retry all"),
        ]
        slots.add("dashboard_top", render=lambda ctx: "queue depth: 0")


class BrokenPlugin(plugins.VantagePlugin):
    name = "broken"

    def register(self, site):
        raise RuntimeError("no Celery broker configured")


# ── tests ───────────────────────────────────────────────────────────────────


class TestDiscovery:
    def test_a_plugin_registers_a_page_and_an_action(self, site, entry_points):
        site.register(Archive)
        entry_points(FakeEntryPoint("queue", QueuePlugin))

        loaded = plugins.load_plugins(site)

        assert [p.get_name() for p in loaded] == ["queue"]
        assert QueuePage in site.pages()
        names = [action.name for action in resolve_actions(site.get_panel(Archive))]
        assert "retry_all" in names
        assert slots.render_slot("dashboard_top") == "queue depth: 0"

    def test_the_action_a_plugin_added_actually_runs(self, site, entry_points, db):
        site.register(Archive)
        entry_points(FakeEntryPoint("queue", QueuePlugin))
        plugins.load_plugins(site)

        panel = site.get_panel(Archive)
        action = next(a for a in resolve_actions(panel) if a.name == "retry_all")
        result = action.run(panel, None, Archive.objects.all())
        assert result.message == "Retried 0."

    def test_a_plugin_class_an_instance_and_a_function_all_work(self, site, entry_points):
        registered = []

        def setup(site):
            registered.append("function")

        class Instance(plugins.VantagePlugin):
            name = "instance"

            def register(self, site):
                registered.append("instance")

        entry_points(
            FakeEntryPoint("setup", setup),
            FakeEntryPoint("instance", Instance()),
        )
        plugins.load_plugins(site)
        assert sorted(registered) == ["function", "instance"]

    def test_loaded_plugins_are_reported_per_site(self, site, entry_points):
        other = VantageSite(name="other")
        site.register(Archive)
        entry_points(FakeEntryPoint("queue", QueuePlugin))
        plugins.load_plugins(site)
        assert [p.get_name() for p in plugins.loaded_plugins(site)] == ["queue"]
        assert plugins.loaded_plugins(other) == []

    def test_version_metadata_survives(self, site, entry_points):
        site.register(Archive)
        entry_points(FakeEntryPoint("queue", QueuePlugin))
        plugin = plugins.load_plugins(site)[0]
        assert (plugin.get_name(), plugin.version) == ("queue", "1.2.0")


class TestIdempotence:
    def test_loading_twice_registers_once(self, site, entry_points):
        """``ready()`` runs more than once. Three copies of a page is the bug."""
        site.register(Archive)
        entry_points(FakeEntryPoint("queue", QueuePlugin))

        first = plugins.load_plugins(site)
        second = plugins.load_plugins(site)

        assert len(first) == 1
        assert second == []
        assert site.pages().count(QueuePage) == 1
        assert len(plugins.loaded_plugins(site)) == 1

    def test_explicit_registration_after_discovery_is_not_a_duplicate(self, site, entry_points):
        site.register(Archive)
        entry_points(FakeEntryPoint("queue", QueuePlugin))
        plugins.load_plugins(site)

        assert plugins.register_plugin(QueuePlugin, site) is None
        assert site.pages().count(QueuePage) == 1


class TestExplicitRegistration:
    def test_a_project_can_register_a_plugin_by_hand(self, site):
        site.register(Archive)
        plugin = plugins.register_plugin(QueuePlugin, site)
        assert plugin is not None
        assert QueuePage in site.pages()

    def test_registering_a_non_plugin_is_reported_not_raised(self, site):
        assert plugins.register_plugin(object(), site) is None
        assert [err.name for err in plugins.plugin_errors()]

    def test_a_broken_explicit_plugin_is_reported(self, site):
        assert plugins.register_plugin(BrokenPlugin, site) is None
        assert [err.name for err in plugins.plugin_errors()] == ["broken"]


class TestFailureIsNeverFatal:
    def test_a_plugin_that_raises_on_load_does_not_propagate(self, site, entry_points):
        entry_points(FakeEntryPoint("broken", ImportError("no module named celery")))
        assert plugins.load_plugins(site) == []

    def test_a_broken_plugin_surfaces_as_a_check_error(self, site, entry_points):
        entry_points(FakeEntryPoint("broken", ImportError("no module named celery")))
        plugins.load_plugins(site)

        errors = plugins.check_plugins(None)
        assert [err.id for err in errors] == ["vantage.E030"]
        assert "'broken'" in errors[0].msg
        assert "no module named celery" in errors[0].hint
        assert "PLUGINS_EXCLUDE" in errors[0].hint

    def test_a_plugin_whose_register_hook_raises_is_reported(self, site, entry_points):
        entry_points(FakeEntryPoint("broken", BrokenPlugin))
        plugins.load_plugins(site)
        errors = plugins.check_plugins(None)
        assert len(errors) == 1
        assert "no Celery broker configured" in errors[0].hint

    def test_a_broken_plugin_does_not_stop_a_working_one(self, site, entry_points):
        site.register(Archive)
        entry_points(
            FakeEntryPoint("broken", BrokenPlugin),
            FakeEntryPoint("queue", QueuePlugin),
        )
        loaded = plugins.load_plugins(site)
        assert [p.get_name() for p in loaded] == ["queue"]
        assert len(plugins.check_plugins(None)) == 1

    def test_a_failed_plugin_is_not_retried_into_a_duplicate(self, site, entry_points):
        """A hook that raised halfway already registered whatever it managed to."""
        entry_points(FakeEntryPoint("broken", BrokenPlugin))
        plugins.load_plugins(site)
        plugins.load_plugins(site)
        assert len(plugins.check_plugins(None)) == 1

    def test_no_errors_means_no_check_messages(self, site, entry_points):
        entry_points()
        plugins.load_plugins(site)
        assert plugins.check_plugins(None) == []


class TestAllowlist:
    def points(self, site, entry_points):
        site.register(Archive)
        entry_points(
            FakeEntryPoint("queue", QueuePlugin),
            FakeEntryPoint("audit", make_plugin("audit")),
        )

    @override_settings(VANTAGE={"PLUGINS": ["audit"]})
    def test_an_allowlist_loads_only_what_it_names(self, site, entry_points):
        self.points(site, entry_points)
        loaded = plugins.load_plugins(site)
        assert [p.get_name() for p in loaded] == ["audit"]
        assert QueuePage not in site.pages()

    @override_settings(VANTAGE={"PLUGINS": []})
    def test_an_empty_allowlist_loads_nothing(self, site, entry_points):
        self.points(site, entry_points)
        assert plugins.load_plugins(site) == []

    @override_settings(VANTAGE={"PLUGINS_EXCLUDE": ["queue"]})
    def test_a_denylist_skips_a_plugin_that_is_installed(self, site, entry_points):
        self.points(site, entry_points)
        assert [p.get_name() for p in plugins.load_plugins(site)] == ["audit"]

    @override_settings(VANTAGE={"PLUGINS": ["queue"], "PLUGINS_EXCLUDE": ["queue"]})
    def test_the_denylist_wins(self, site, entry_points):
        """Turning something off is the urgent operation; it must not need two edits."""
        self.points(site, entry_points)
        assert plugins.load_plugins(site) == []

    @override_settings(VANTAGE={"PLUGINS_EXCLUDE": ["broken"]})
    def test_excluding_a_broken_plugin_avoids_importing_it_at_all(self, site, entry_points):
        entry_points(FakeEntryPoint("broken", ImportError("boom")))
        assert plugins.load_plugins(site) == []
        assert plugins.check_plugins(None) == []

    def test_no_setting_loads_everything(self, site, entry_points):
        self.points(site, entry_points)
        assert len(plugins.load_plugins(site)) == 2

    @override_settings(VANTAGE={"PLUGINS": ["queue", "typo-here"]})
    def test_an_allowlist_entry_nothing_provides_is_a_warning(self, site, entry_points):
        self.points(site, entry_points)
        plugins.load_plugins(site)
        warnings = [m for m in plugins.check_plugins(None) if m.id == "vantage.W030"]
        assert len(warnings) == 1
        assert "typo-here" in warnings[0].msg

    def test_no_warning_before_discovery_has_run(self, site):
        assert plugins.check_plugins(None) == []


class TestSettingsAreOptional:
    """The keys may not be in ``conf.DEFAULTS`` yet; discovery must not care."""

    def test_reading_an_unknown_setting_falls_back(self, site, entry_points):
        entry_points(FakeEntryPoint("queue", QueuePlugin))
        site.register(Archive)
        assert plugins._setting("PLUGINS") is None or isinstance(plugins._setting("PLUGINS"), list)
        assert len(plugins.load_plugins(site)) == 1


class TestPluginBase:
    def test_the_base_register_hook_does_nothing(self, site):
        assert plugins.VantagePlugin().register(site) is None

    def test_the_name_defaults_to_the_class_name(self):
        class Unnamed(plugins.VantagePlugin):
            pass

        assert Unnamed.get_name() == "Unnamed"

    def test_a_plugin_can_contribute_a_panel(self, site, entry_points):
        class ArchivePanel(ModelPanel):
            list_display = ["id"]

        def setup(site):
            site.register(Archive, ArchivePanel)

        entry_points(FakeEntryPoint("panels", setup))
        plugins.load_plugins(site)
        assert isinstance(site.get_panel(Archive), ArchivePanel)


def make_plugin(plugin_name: str):
    """A minimal plugin class, for tests that only care that it loaded."""
    return type(
        f"{plugin_name.title()}Plugin",
        (plugins.VantagePlugin,),
        {"name": plugin_name, "register": lambda self, site: None},
    )


def test_a_site_is_not_kept_alive_by_its_plugins(entry_points):
    """The registry is weak on purpose: test sites must not accumulate forever."""
    import gc

    entry_points(FakeEntryPoint("audit", make_plugin("audit")))
    scratch = VantageSite(name="scratch")
    plugins.load_plugins(scratch)
    assert plugins.loaded_plugins(scratch)

    tracked = SimpleNamespace(count=len(plugins._loaded))
    del scratch
    gc.collect()
    assert len(plugins._loaded) < tracked.count
