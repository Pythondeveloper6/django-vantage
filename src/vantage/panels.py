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

    def __init__(self, model: type[models.Model], site: VantageSite | None = None) -> None:
        self.model = model
        self.site = site
        self.opts = model._meta
        if self.search_fields is not None and self.list_search is None:
            self.list_search = self.search_fields

    def __repr__(self) -> str:
        return f"<{type(self).__name__} for {self.opts.label}>"

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

    # ── overridable hooks ───────────────────────────────────────────────────

    def get_queryset(self, request):
        """The base queryset for every view. Override to scope rows per tenant/user."""
        return self.model._default_manager.all()
