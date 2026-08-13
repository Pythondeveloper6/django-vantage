from django.apps import AppConfig
from django.core import checks
from django.utils.module_loading import autodiscover_modules
from django.utils.translation import gettext_lazy as _


class SimpleVantageConfig(AppConfig):
    """Vantage without autodiscovery, for projects that register explicitly.

    Use it with ``INSTALLED_APPS = ["vantage.apps.SimpleVantageConfig", ...]``.
    """

    name = "vantage"
    label = "vantage"
    verbose_name = _("Vantage")
    default_auto_field = "django.db.models.BigAutoField"
    default_site = "vantage.sites.VantageSite"

    def ready(self) -> None:
        from vantage.checks import check_panels, check_vantage_settings
        from vantage.nav import check_nav

        checks.register(check_vantage_settings, "vantage")
        checks.register(check_panels, "vantage")
        checks.register(check_nav, "vantage")


class VantageConfig(SimpleVantageConfig):
    """The default: discovers ``panel.py`` in every installed app."""

    default = True

    def ready(self) -> None:
        super().ready()
        from vantage.pages import SchemaPage
        from vantage.sites import site

        # Built-in pages register before autodiscovery so a project can unregister
        # or replace one from its own panel.py.
        site.register_page(SchemaPage)

        # Explicit configuration first, so it always wins over inference.
        # ``autodiscover_modules`` snapshots and restores the registry if a
        # panel.py raises, and re-raises only when the module actually exists —
        # so a genuine bug in a user's panel.py is loud rather than making their
        # models silently vanish.
        autodiscover_modules("panel", register_to=site)
        site.registry.autoregister()
