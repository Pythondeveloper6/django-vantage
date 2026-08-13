"""``ModelPanel`` — the per-model configuration class.

Every attribute defaults to ``None``, meaning *infer it*. Setting one attribute
overrides that attribute and nothing else: configuring ``list_display`` must never
switch off inference for filters, search or widgets. That rule is the package's most
important ergonomic guarantee, and it is why inference is resolved per-attribute
rather than as a single all-or-nothing fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import models

from vantage import inference
from vantage.conf import vantage_settings
from vantage.optimizer import plan_related

if TYPE_CHECKING:
    from vantage.sites import VantageSite


class ModelPanel:
    """Configuration for one model's presence in the panel."""

    # ── identity ────────────────────────────────────────────────────────────
    label: str | None = None
    label_plural: str | None = None
    icon: str | None = None
    group: str | None = None
    order: int = 0
    description: str | None = None

    # ── list page ───────────────────────────────────────────────────────────
    list_display: list[str] | None = None
    list_link: str | None = None
    list_filter: list[str] | None = None
    list_search: list[str] | None = None
    list_editable: list[str] | None = None
    list_per_page: int | None = None
    list_select_related: list[str] | None = None
    ordering: list[str] | None = None

    # ── list analytics ──────────────────────────────────────────────────────
    list_stats: list[Any] | None = None
    list_charts: list[Any] | None = None
    list_aggregates: dict[str, Any] | None = None

    # ── detail page ─────────────────────────────────────────────────────────
    fields: list[str] | None = None
    exclude: list[str] | None = None
    layout: Any | None = None
    readonly_fields: list[str] | None = None
    inlines: list[Any] | None = None
    related_tabs: bool = True

    # ── behaviour ───────────────────────────────────────────────────────────
    actions: list[Any] | None = None
    row_actions: list[Any] | None = None
    permissions: Any | None = None

    #: Accepting ``search_fields`` as an alias keeps muscle memory from
    #: ``django.contrib.admin`` intact.
    search_fields: list[str] | None = None

    #: Swap in a subclass to extend validation for your own panel base class.
    checks_class: type | None = None

    def __init__(self, model: type[models.Model], site: VantageSite | None = None) -> None:
        self.model = model
        self.site = site
        self.opts = model._meta
        if self.search_fields is not None and self.list_search is None:
            self.list_search = self.search_fields

    def __repr__(self) -> str:
        return f"<{type(self).__name__} for {self.opts.label}>"

    def check(self, **kwargs) -> list:
        """Validate this panel's configuration. Called by ``manage.py check``."""
        from vantage.panel_checks import ModelPanelChecks

        return (self.checks_class or ModelPanelChecks)().check(self, **kwargs)

    # ── identity helpers ────────────────────────────────────────────────────

    def get_label(self) -> str:
        return self.label or str(self.opts.verbose_name).capitalize()

    def get_label_plural(self) -> str:
        return self.label_plural or str(self.opts.verbose_name_plural).capitalize()

    def get_group(self) -> str:
        return self.group or str(self.opts.app_config.verbose_name)

    @property
    def slug(self) -> str:
        """URL segment identifying this panel: ``testapp.post`` -> ``testapp-post``."""
        return f"{self.opts.app_label}-{self.opts.model_name}"

    # ── resolved configuration ──────────────────────────────────────────────
    #
    # Each getter falls back to inference independently. This is what makes
    # setting ``list_display`` override the columns and nothing else — the rule
    # that keeps partial configuration from silently costing a user the rest of
    # the intelligence. Override a getter for logic that inference cannot express.

    def get_list_display(self, request=None) -> list[str]:
        if self.list_display is not None:
            return list(self.list_display)
        return inference.infer_list_display(self.model)

    def get_list_link(self, request=None) -> str:
        if self.list_link is not None:
            return self.list_link
        # An explicit list_display without an explicit list_link should link its
        # first column, not a column inference would have chosen but the user
        # then removed.
        if self.list_display:
            inferred = inference.infer_list_link(self.model)
            return inferred if inferred in self.list_display else self.list_display[0]
        return inference.infer_list_link(self.model)

    def get_list_filter(self, request=None) -> list[str]:
        if self.list_filter is not None:
            return list(self.list_filter)
        return inference.infer_list_filter(self.model)

    def get_list_search(self, request=None) -> list[str]:
        if self.list_search is not None:
            return list(self.list_search)
        return inference.infer_list_search(self.model)

    def get_ordering(self, request=None) -> list[str]:
        if self.ordering is not None:
            return list(self.ordering)
        return inference.infer_ordering(self.model)

    def get_per_page(self, request=None) -> int:
        if self.list_per_page is not None:
            return self.list_per_page
        return vantage_settings.DEFAULT_PER_PAGE

    # ── overridable hooks ───────────────────────────────────────────────────

    def get_queryset(self, request):
        """The base queryset for every view. Override to scope rows per tenant/user."""
        return self.model._default_manager.all()

    def get_list_queryset(self, request):
        """The list-page queryset, with query optimisation already applied.

        The optimisation is derived from what the page actually renders, so a column
        added to ``list_display`` cannot reintroduce an N+1 — the promise the test
        suite asserts with ``assertNumQueries``.
        """
        queryset = self.get_queryset(request)
        paths = [*self.get_list_display(request), *self.get_list_search(request)]
        select, prefetch = plan_related(self.model, paths)
        if select:
            queryset = queryset.select_related(*sorted(select))
        if prefetch:
            queryset = queryset.prefetch_related(*sorted(prefetch))
        ordering = self.get_ordering(request)
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset
