"""Autodiscovered by Vantage at startup.

Almost every model here is left to pure inference on purpose. ``Archive`` is the one
exception, because the action API needs a panel to hang actions on. Pages are
registered here rather than in a test module because they must exist before the
urlconf is built — the same constraint real projects have.
"""

from tests.testapp import pages
from tests.testapp.models import Archive
from vantage import ModelPanel, action, register, site


@register(Archive)
class ArchivePanel(ModelPanel):
    """The only configured panel here — it exists to exercise the action API.

    Every other model in this app stays on pure inference.
    """

    #: Built-ins by name, then two of the panel's own actions. The mix is the fixture:
    #: a built-in resolves to a link at a page that already exists, an ``@action``
    #: resolves to a button that posts, and ``purge`` carries a permission the
    #: built-ins do not so the per-page permission filter has something to remove.
    row_actions = ["view", "edit", "duplicate", "restore", "purge", "delete"]

    @action("Restore", icon="check")
    def restore(self, request, queryset):
        return f"Restored {queryset.update(deleted_at=None)}."

    @action("Purge", destructive=True, permission="delete", max_rows=3)
    def purge(self, request, queryset):
        deleted, _ = queryset.delete()
        return f"Purged {deleted}."


for page in (
    pages.RevenuePage,
    pages.BareMinimumPage,
    pages.GatedPage,
    pages.CustomPathPage,
):
    site.register_page(page)
