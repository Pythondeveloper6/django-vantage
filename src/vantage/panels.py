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

from vantage import inference, listprefs
from vantage.conf import vantage_settings
from vantage.optimizer import plan_related

if TYPE_CHECKING:
    from vantage.sites import VantageSite

#: A small nicety: common model names get a fitting sidebar icon for free. Anything
#: not listed falls back to a neutral mark, and ``icon`` overrides it.
GUESSED_ICONS: dict[str, str] = {
    "user": "users",
    "group": "shield",
    "permission": "shield",
    "post": "file-text",
    "article": "file-text",
    "page": "file-text",
    "comment": "message-square",
    "order": "shopping-cart",
    "product": "shopping-cart",
    "tag": "tag",
    "category": "folder",
    "archive": "archive",
}


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
    related_tabs: bool | None = None

    # ── behaviour ───────────────────────────────────────────────────────────
    #
    # There is deliberately no ``permissions`` attribute. Access is answered in one
    # place — :class:`~vantage.permissions.PanelPermissions` — and a panel has its say
    # by defining ``has_<action>_permission(request, obj=None)``, which
    # :meth:`~vantage.permissions.PanelPermissions.panel_override` consults before the
    # superuser bypass. A second declarative mechanism for the same decision is how
    # two answers to "may this user delete?" end up disagreeing. Setting it is caught
    # by ``vantage.E050`` rather than ignored.
    actions: list[Any] | None = None
    row_actions: list[Any] | None = None

    #: The data quality audit: ``None`` runs every check, ``False`` leaves this model
    #: out, a list of check names (``["nulls", "stale"]``) skips just those.
    data_quality: bool | list[str] | None = None

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

    def get_patterns(self):
        """Modelling conventions detected on this model, computed once.

        Detection is pure but walks the field list several times, and panels are
        long-lived singletons, so it is memoised on the instance rather than redone
        per request.
        """
        cached = getattr(self, "_patterns", None)
        if cached is None:
            from vantage.patterns import detect_all

            cached = detect_all(self)
            self._patterns = cached
        return cached

    def get_trash_queryset(self, request):
        """Soft-deleted rows.

        Starts from ``_base_manager`` when the model's default manager already hides
        them: a trash view built on ``get_queryset()`` would filter a queryset that
        can never contain what it is looking for, and silently show nothing.
        """
        pattern = self.get_patterns().soft_delete
        if pattern is None:
            return self.model._default_manager.none()
        base = (
            self.model._base_manager.all()
            if pattern.manager_hides_trashed
            else self.get_queryset(request)
        )
        return base.filter(pattern.trashed)

    @property
    def list_url_name(self) -> str:
        return f"vantage:{self.slug}-list"

    @property
    def count(self) -> int:
        return self.model._default_manager.count()

    def get_icon(self) -> str:
        """An icon name, guessed from the model name when none was declared."""
        if self.icon:
            return self.icon
        return GUESSED_ICONS.get(self.opts.model_name, "box")

    # ── template slots ──────────────────────────────────────────────────────

    def slot(self, name: str, context: dict | None = None):
        """HTML for the named template slot, on this model's pages only.

        The per-model half of :mod:`vantage.slots`. Registering a contribution with
        ``model=`` covers content the *project* owns; this covers content the *panel*
        owns, which is the case where naming the model twice is pure ceremony. Return
        ``None`` to contribute nothing.

        The default is not "nothing": it is whatever Vantage itself fills for this
        panel, which today is the ``row_actions`` buttons and the duplicate page's
        notes. That logic lives in :func:`vantage.records.panel_slot` rather than here,
        so overriding this method without calling ``super()`` costs a panel author only
        their own contribution and is a decision they can see.

        Output is escaped unless marked safe, and an exception here is logged to the
        ``vantage.slots`` logger and skipped rather than taking the page down.
        """
        from vantage.records import panel_slot

        return panel_slot(self, name, context)

    # ── resolved configuration ──────────────────────────────────────────────
    #
    # Each getter falls back to inference independently. This is what makes
    # setting ``list_display`` override the columns and nothing else — the rule
    # that keeps partial configuration from silently costing a user the rest of
    # the intelligence. Override a getter for logic that inference cannot express.

    def get_list_display(self, request=None) -> list[str]:
        stored = listprefs.override(self, request, "list_display")
        if stored is not None:
            return list(stored)
        if self.list_display is not None:
            return list(self.list_display)
        return inference.infer_list_display(self.model)

    def get_list_link(self, request=None) -> str:
        if self.list_link is not None:
            return self.list_link
        # An explicit list_display — or a UI override of one — without an explicit
        # list_link should link its first column, not a column inference would have
        # chosen but the user then hid.
        inferred = inference.infer_list_link(self.model)
        if self.list_display or listprefs.override(self, request, "list_display"):
            columns = self.get_list_display(request)
            return inferred if inferred in columns else columns[0]
        return inferred

    def get_list_filter(self, request=None) -> list[str]:
        stored = listprefs.override(self, request, "list_filter")
        if stored is not None:
            return list(stored)
        if self.list_filter is not None:
            return list(self.list_filter)
        return inference.infer_list_filter(self.model)

    def get_list_search(self, request=None) -> list[str]:
        stored = listprefs.override(self, request, "list_search")
        if stored is not None:
            return list(stored)
        if self.list_search is not None:
            return list(self.list_search)
        return inference.infer_list_search(self.model)

    def get_ordering(self, request=None) -> list[str]:
        if self.ordering is not None:
            return list(self.ordering)
        return inference.infer_ordering(self.model)

    def get_related_tabs(self, related_count: int | None = None) -> bool:
        """Whether the detail page groups reverse relations into a tab strip.

        Inferred per-attribute like everything else: a tab strip is worth its cost
        once there is a choice to make, so two or more related blocks become tabs and
        a single one stays the plain card it already was — a one-tab tab bar is a
        control that cannot do anything. ``related_tabs = True`` asserts tabs even for
        one relation; ``False`` keeps every relation stacked, which is the right answer
        for a record whose children are meant to be read together.
        """
        if self.related_tabs is not None:
            return bool(self.related_tabs)
        return related_count is not None and related_count > 1

    def get_per_page(self, request=None) -> int:
        stored = listprefs.override(self, request, "per_page")
        if stored is not None:
            return stored
        if self.list_per_page is not None:
            return self.list_per_page
        return vantage_settings.DEFAULT_PER_PAGE

    # ── overridable hooks ───────────────────────────────────────────────────

    def get_queryset(self, request):
        """The base queryset for every view. Override to scope rows per tenant/user."""
        return self.model._default_manager.all()

    def _list_paths(self, request=None) -> list[str]:
        """Every path the list page will touch: what optimisation is derived from."""
        return [*self.get_list_display(request), *self.get_list_search(request)]

    def get_list_select_related(self, request=None) -> list[str]:
        """The joins the list page makes: derived, **plus** anything declared.

        ``list_select_related`` adds to the derived set and never replaces it. Replacing
        would make the attribute a foot-gun: a hand-maintained list goes stale the
        moment a column is added, and the symptom is an N+1 that the page used to be
        immune to. Declaring joins is still useful for what rendering cannot see — a
        relation touched by ``__str__``, by a ``get_queryset`` override, or by a panel
        method column — which is also what an adopted ``ModelAdmin`` was expressing.
        """
        select, _ = plan_related(self.model, self._list_paths(request))
        return sorted(select | {str(path) for path in (self.list_select_related or ())})

    def get_list_queryset(self, request):
        """The list-page queryset, with query optimisation already applied.

        The optimisation is derived from what the page actually renders, so a column
        added to ``list_display`` cannot reintroduce an N+1 — the promise the test
        suite asserts with ``assertNumQueries``.
        """
        queryset = self.get_queryset(request)
        _, prefetch = plan_related(self.model, self._list_paths(request))
        select = self.get_list_select_related(request)
        if select:
            queryset = queryset.select_related(*select)
        if prefetch:
            queryset = queryset.prefetch_related(*sorted(prefetch))
        ordering = self.get_ordering(request)
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset
