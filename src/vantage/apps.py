from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class VantageConfig(AppConfig):
    name = "vantage"
    verbose_name = "Vantage"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from vantage import checks  # noqa: F401  (registers system checks)
        from vantage.sites import site

        # Explicit configuration first, so it always wins over inference.
        autodiscover_modules("panel", register_to=site)
        site.registry.autoregister()
