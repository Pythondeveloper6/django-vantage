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

    # ── alternative list views ──────────────────────────────────────────────
    #
    # ``list_views = ["table", "kanban"]`` picks which views the switcher offers, in
    # order; the first is the default. ``["table"]`` switches the others off. Each
    # field below is inferred independently, see :mod:`vantage.listviews.inference`.
    list_views: list[str] | None = None
    kanban_field: str | None = None
    calendar_field: str | None = None
    #: What the board's "Group by" and the calendar's "Dates from" pickers offer.
    #: Inferred as every eligible field; ``[]`` hides the picker, leaving only the
    #: default above. The default is always offered, first.
    kanban_fields: list[str] | None = None
    calendar_fields: list[str] | None = None
    gallery_field: str | None = None
    #: A ``PointField`` name, or a ``("latitude", "longitude")`` pair.
    map_fields: str | tuple[str, str] | None = None
    card_fields: list[str] | None = None

    # ── detail page ─────────────────────────────────────────────────────────
    fields: list[str] | None = None
    exclude: list[str] | None = None
    layout: Any | None = None
    readonly_fields: list[str] | None = None
    inlines: list[Any] | None = None
    related_tabs: bool | None = None

    # ── sensitive values ────────────────────────────────────────────────────
    #
    # ``None`` infers them (see vantage.sensitive); a list names them outright and
    # replaces inference; ``[]`` switches masking off for this model. A user model's
    # password hash is masked whatever this says.
    sensitive_fields: list[str] | None = None

    # ── field-level permissions ─────────────────────────────────────────────
    #
    # Per role, which fields are ``"hidden"`` or ``"readonly"``:
    # ``{"Support": {"price": "readonly", "cost": "hidden"}}``. Added to whatever the
    # roles matrix stores, the stricter of the two winning; superusers are never
    # restricted. See vantage.fieldperms.
    field_permissions: dict[str, dict[str, str]] | None = None

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
    #: Whether deletes and edits on this model offer "Undo" in their toast. ``None``
    #: follows ``VANTAGE["UNDO_WINDOW"]``; ``False`` switches it off for this model —
    #: for one whose rows must never come back once deleted, say.
    undo: bool | None = None
    #: The record's history page: its audit timeline, the record as of any point in
    #: it, and restoring fields from there. ``None`` follows ``VANTAGE["AUDIT_LOG"]``;
    #: ``"view"`` keeps the page but offers no restore; ``False`` removes it.
    history: bool | str | None = None

    #: Fields whose changes wait for someone with ``<app>.approve_<model>`` before they
    #: are saved. Anyone without it can still edit the record: the other fields save as
    #: usual and these become a pending request. See :mod:`vantage.approvals`.
    requires_approval: list[str] | None = None

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
        columns = self._declared_list_display(request)
        if request is None:
            return columns
        # A column whose field is hidden from this person is not a masked column, it
        # is no column: the header alone would say the field exists.
        return self._without_hidden(request, columns) or [self.opts.pk.name]

    def _declared_list_display(self, request=None) -> list[str]:
        stored = listprefs.override(self, request, "list_display")
        if stored is not None:
            return list(stored)
        if self.list_display is not None:
            return list(self.list_display)
        inferred = inference.infer_list_display(self.model)
        # Inference already skips names that *look* sensitive; this also drops a field
        # the panel declared sensitive under an innocent name. An explicit list_display
        # is left alone — the column renders masked.
        if self.sensitive_fields:
            hidden = self.get_sensitive_fields()
            inferred = [name for name in inferred if name not in hidden] or [self.opts.pk.name]
        return inferred

    def get_list_link(self, request=None) -> str:
        link = self._declared_list_link(request)
        if request is not None and link not in self.get_list_display(request):
            # A hidden field is no column, so it cannot carry the row's link either.
            from vantage.fieldperms import is_hidden_path

            if is_hidden_path(request, self, link):
                return self.get_list_display(request)[0]
        return link

    def _declared_list_link(self, request=None) -> str:
        if self.list_link is not None:
            return self.list_link
        # An explicit list_display — or a UI override of one — without an explicit
        # list_link should link its first column, not a column inference would have
        # chosen but the user then hid.
        inferred = inference.infer_list_link(self.model)
        if self.sensitive_fields and inferred in self.get_sensitive_fields():
            # A link whose text is a mask opens the record fine and reads as nothing.
            return self.get_list_display(request)[0]
        if self.list_display or listprefs.override(self, request, "list_display"):
            columns = self.get_list_display(request)
            return inferred if inferred in columns else columns[0]
        return inferred

    def get_list_filter(self, request=None) -> list[str]:
        # Never a sensitive path, whoever declared it: a filter is an oracle — ticking
        # "pin = 1234" and watching the row count answers the question the mask exists
        # to refuse. ``vantage.W090`` tells the author their declaration was dropped.
        # A field hidden from this person is refused for the same reason.
        paths = self._without_sensitive(self._declared_list_filter(request))
        return self._without_hidden(request, paths)

    def _declared_list_filter(self, request=None) -> list[str]:
        stored = listprefs.override(self, request, "list_filter")
        if stored is not None:
            return list(stored)
        if self.list_filter is not None:
            return list(self.list_filter)
        return inference.infer_list_filter(self.model)

    def get_list_search(self, request=None) -> list[str]:
        # Same reason as get_list_filter: a substring search is a guessing oracle.
        paths = self._without_sensitive(self._declared_list_search(request))
        return self._without_hidden(request, paths)

    def _declared_list_search(self, request=None) -> list[str]:
        stored = listprefs.override(self, request, "list_search")
        if stored is not None:
            return list(stored)
        if self.list_search is not None:
            return list(self.list_search)
        return inference.infer_list_search(self.model)

    def _without_sensitive(self, paths: list[str]) -> list[str]:
        from vantage.sensitive import strip_sensitive

        return strip_sensitive(self, paths)

    def _without_hidden(self, request, paths: list[str]) -> list[str]:
        if request is None:
            return paths
        from vantage.fieldperms import strip_hidden

        return strip_hidden(request, self, paths)

    # ── sensitive values ────────────────────────────────────────────────────

    def get_sensitive_fields(self) -> frozenset[str]:
        """The field names masked on every surface. See :mod:`vantage.sensitive`.

        Memoised per vocabulary: this is asked once per cell on a list page, and the
        answer only changes when ``sensitive_fields`` or the settings do.
        """
        from vantage.sensitive import panel_sensitive_fields, patterns

        declared = self.sensitive_fields
        key = (patterns(), None if declared is None else tuple(declared))
        cached = getattr(self, "_sensitive_memo", None)
        if cached is None or cached[0] != key:
            cached = (key, panel_sensitive_fields(self), {})
            self._sensitive_memo = cached
        return cached[1]

    def get_requires_approval(self) -> tuple[str, ...]:
        """The fields a change to which needs approval, in declared order.

        Only names a held-back change can actually be made to: an unknown name, a
        many-to-many or a file field is reported by ``manage.py check``
        (``vantage.E106`` to ``E108``) and skipped here, so one bad entry cannot make the
        edit page fail for a field nobody meant.
        """
        from vantage.approvals import approvable_fields

        return approvable_fields(self)

    def can_reveal(self, request, obj, field_name: str) -> bool:
        """Whether ``request`` may reveal ``field_name`` on ``obj``. Override to narrow or widen.

        Superusers, and anyone holding ``vantage.reveal_sensitive``. Asked only after the
        user has passed the view check for ``obj`` and only for a field that is not a
        hash — a hashed field cannot be made revealable from here.
        """
        from vantage.sensitive import REVEAL_PERMISSION

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return bool(user.is_superuser) or user.has_perm(REVEAL_PERMISSION)

    def get_ordering(self, request=None) -> list[str]:
        ordering = self._declared_ordering()
        if request is None or not ordering:
            return ordering
        # Ranking rows by a field someone cannot see tells them its order, which is a
        # bound on every value of it. Falls back to newest-first rather than to the
        # model's Meta.ordering, which may be the very field being hidden.
        return self._without_hidden(request, ordering) or ["-pk"]

    def _declared_ordering(self) -> list[str]:
        if self.ordering is not None:
            # A fixed order by a secret still ranks every row by it. Dropped, with
            # vantage.W090 saying so; inference never chooses one.
            kept = self._without_sensitive(list(self.ordering))
            if kept or not self.ordering:
                return kept
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

    # ── alternative list views ──────────────────────────────────────────────

    def get_list_views(self, request=None) -> list[str]:
        """The views the switcher offers, first one the default.

        A declared list is filtered down to the views this model can actually draw,
        so a panel naming ``"map"`` on a model with no coordinates loses the button
        rather than rendering an empty plot — ``vantage.E071`` reports the mistake.
        """
        from vantage.listviews import supported

        if self.list_views is None:
            from vantage.listviews.inference import infer_list_views

            declared = infer_list_views(self.model)
        else:
            declared = list(self.list_views)
        views = [name for name in declared if supported(self, name)]
        if request is not None:
            # A board grouped by a field this person cannot see would print its values
            # as column headings; a map of hidden coordinates is the coordinates.
            views = [name for name in views if self._view_visible(name, request)]
        return views or ["table"]

    def _view_visible(self, name: str, request) -> bool:
        from vantage.fieldperms import hidden_names

        hidden = hidden_names(request, self)
        if not hidden:
            return True
        if name == "kanban":
            return bool(self.get_kanban_fields(request))
        if name == "calendar":
            return bool(self.get_calendar_fields(request))
        if name == "gallery":
            return self.get_gallery_field(request) not in hidden
        if name == "map":
            fields = self.get_map_fields(request)
            names = (fields,) if isinstance(fields, str) else tuple(fields or ())
            return not any(field in hidden for field in names)
        return True

    def get_kanban_field(self, request=None) -> str | None:
        if self.kanban_field is not None:
            return self.kanban_field
        from vantage.listviews.inference import infer_kanban_field

        return infer_kanban_field(self.model)

    def get_calendar_field(self, request=None) -> str | None:
        if self.calendar_field is not None:
            return self.calendar_field
        from vantage.listviews.inference import infer_calendar_field

        return infer_calendar_field(self.model)

    def get_kanban_fields(self, request=None) -> list[str]:
        """The fields a person may group the board by, the default first."""
        from vantage.listviews.inference import kanban_candidates

        return self._view_field_choices(
            self.get_kanban_field(request), self.kanban_fields, kanban_candidates, request
        )

    def get_calendar_fields(self, request=None) -> list[str]:
        """The dates a person may place the calendar by, the default first."""
        from vantage.listviews.inference import calendar_candidates

        return self._view_field_choices(
            self.get_calendar_field(request), self.calendar_fields, calendar_candidates, request
        )

    def _view_field_choices(self, default, declared, infer, request=None) -> list[str]:
        """``default`` first, then the declared or inferred rest, never a sensitive one.

        A sensitive field is refused even when declared: grouping by a secret prints
        one column per distinct value, which is the value. So is a field hidden from
        this person, for the same reason.
        """
        sensitive = self.get_sensitive_fields()
        rest = infer(self.model) if declared is None else list(declared)
        chosen = [default] if default else []
        chosen.extend(name for name in rest if name not in chosen)
        return self._without_hidden(request, [name for name in chosen if name not in sensitive])

    def get_gallery_field(self, request=None) -> str | None:
        if self.gallery_field is not None:
            return self.gallery_field
        from vantage.listviews.inference import infer_gallery_field

        return infer_gallery_field(self.model)

    def get_map_fields(self, request=None) -> str | tuple[str, str] | None:
        if self.map_fields is not None:
            fields = self.map_fields
            return fields if isinstance(fields, str) else tuple(fields)
        from vantage.listviews.inference import infer_map_fields

        return infer_map_fields(self.model)

    def get_card_fields(self, request=None, exclude: tuple[str, ...] = ()) -> list[str]:
        """What a card shows under its title. ``exclude`` is the view's own field."""
        if self.card_fields is not None:
            return self._without_hidden(request, list(self.card_fields))
        from vantage.listviews.inference import infer_card_fields

        return infer_card_fields(
            self.model, self.get_list_display(request), exclude, self.get_list_link(request)
        )

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
