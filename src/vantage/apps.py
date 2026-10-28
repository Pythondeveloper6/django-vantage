from django.apps import AppConfig
from django.apps import apps as django_apps
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
        from vantage.checks import (
            DEPLOYMENT_CHECKS,
            check_panels,
            check_two_factor,
            check_vantage_settings,
        )
        from vantage.contrib.django_admin import check_admin_adapter
        from vantage.nav import check_nav
        from vantage.plugins import check_plugins
        from vantage.tenancy import check_tenancy

        checks.register(check_vantage_settings, "vantage")
        checks.register(check_panels, "vantage")
        checks.register(check_two_factor, "vantage")
        checks.register(check_nav, "vantage")
        # Tenant scoping fails closed, which is safe and invisible. This is the half
        # that makes it usable: it names every model whose pages would show nothing.
        checks.register(check_tenancy, "vantage")
        # Registered even without autodiscovery: a plugin registered by hand, or an
        # admin adopted by hand, still has to report what it could not do. Neither
        # import reaches django.contrib.admin — the adapter is lazy about it.
        checks.register(check_plugins, "vantage")
        checks.register(check_admin_adapter, "vantage")

        # vantage.reveal_sensitive, created after migrate rather than by a migration so
        # the feature costs a project no schema change. See vantage.sensitive.
        from django.db.models.signals import post_migrate

        from vantage.sensitive import ensure_reveal_permission

        post_migrate.connect(
            ensure_reveal_permission, sender=self, dispatch_uid="vantage_reveal_permission"
        )
        # Primes the two-factor answer for a fresh session at sign-in, so the panel's
        # first page does not have to query for it. Any login view, not only ours.
        from django.contrib.auth.signals import user_logged_in

        from vantage.twofactor import on_logged_in

        user_logged_in.connect(on_logged_in, dispatch_uid="vantage_two_factor_login")
        # deploy=True keeps these out of the everyday `manage.py check`. Every one of
        # them is correct on a laptop, and a check that is wrong in development is a
        # check everybody learns to scroll past.
        for check in DEPLOYMENT_CHECKS:
            checks.register(check, "vantage", deploy=True)


class VantageConfig(SimpleVantageConfig):
    """The default: discovers ``panel.py`` in every installed app."""

    default = True

    def ready(self) -> None:
        super().ready()
        from vantage.pages import (
            DataQualityPage,
            HealthPage,
            MyReportsPage,
            ReportsPage,
            RolesPage,
            SchemaPage,
            WebhooksPage,
        )
        from vantage.plugins import load_plugins
        from vantage.sites import site

        # Built-in pages register before autodiscovery so a project can unregister
        # or replace one from its own panel.py.
        site.register_page(SchemaPage)
        site.register_page(HealthPage)
        site.register_page(DataQualityPage)
        site.register_page(WebhooksPage)
        site.register_page(MyReportsPage)
        site.register_page(ReportsPage)
        # Groups are a contrib.auth table, and a project without it has no roles.
        if django_apps.is_installed("django.contrib.auth"):
            site.register_page(RolesPage)

        # Plugins next, and before autodiscovery for the same reason: a project's
        # panel.py is its last word, so it must be able to unregister or replace
        # anything an installed package added. Nothing here can raise — a plugin
        # that fails is reported by manage.py check, not by breaking manage.py.
        load_plugins(site)

        # Explicit configuration first, so it always wins over inference.
        # ``autodiscover_modules`` snapshots and restores the registry if a
        # panel.py raises, and re-raises only when the module actually exists —
        # so a genuine bug in a user's panel.py is loud rather than making their
        # models silently vanish.
        autodiscover_modules("panel", register_to=site)
        site.registry.autoregister()
