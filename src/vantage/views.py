"""Panel views.

Every view is bound to a :class:`~vantage.sites.VantageSite` at URL-build time via
``as_view(site=...)``, so two mounted panels never share view state.
"""

from __future__ import annotations

from functools import cached_property

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.utils.translation import get_language, get_language_bidi
from django.views.generic import TemplateView

from vantage.actions import Selection, resolve_actions
from vantage.conf import vantage_settings
from vantage.export import FORMATS, stream_export
from vantage.filters import apply_filters, build_filters, facets_requested
from vantage.nav import build_nav, resolve_theme
from vantage.permissions import get_permissions
from vantage.safety import (
    ImpersonationError,
    impersonating,
    read_only_reason,
    real_user,
    start_impersonation,
    stop_impersonation,
)
from vantage.theme import NO_FLASH_SCRIPT, render_theme_css


class SiteView(TemplateView):
    """Base for every view belonging to a panel."""

    site = None
    page_title = ""
    page_description = ""

    @cached_property
    def permissions(self):
        """Resolved lazily so a directly-constructed view still works in tests."""
        return get_permissions()

    def dispatch(self, request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_staff:
            return redirect_to_login(request.get_full_path())
        denied = self.check_permission(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

    def check_permission(self, request):
        """Return a response to short-circuit with, or None to allow."""
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            site=self.site,
            settings=vantage_settings,
            nav=build_nav(self.site, self.request),
            permissions=self.permissions,
            path=self.request.path,
            theme=resolve_theme(self.request),
            read_only=read_only_reason(),
            impersonating=impersonating(self.request),
            real_user=real_user(self.request),
            language_code=get_language() or "en",
            is_rtl=get_language_bidi(),
            theme_css=render_theme_css(),
            no_flash_script=NO_FLASH_SCRIPT,
            page_title=self.page_title,
            page_description=self.page_description,
            breadcrumbs=self.get_breadcrumbs(),
        )
        return context

    def get_breadcrumbs(self) -> list[tuple[str, str | None]]:
        return []


class DashboardView(SiteView):
    template_name = "vantage/dashboard.html"
    page_title = "Dashboard"

    def get_context_data(self, **kwargs):
        from vantage.dashboards import dashboard_context

        context = super().get_context_data(**kwargs)
        # Passing the view's resolver reuses its cached backend-capability probe
        # rather than making the dashboard work it out again.
        context.update(dashboard_context(self.site, self.request, self.permissions))
        return context


class PanelView(SiteView):
    """Base for views scoped to one registered model."""

    panel = None

    #: The action a subclass needs permission for.
    required_action = "view"

    def check_permission(self, request):
        if self.permissions.has_model_perm(request, self.panel.model, self.required_action):
            return None
        raise PermissionDenied(
            f"You do not have permission to {self.required_action} "
            f"{self.panel.get_label_plural().lower()}."
        )

    def get_breadcrumbs(self):
        return [(self.panel.get_label_plural(), None)]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Rendering a button the user cannot use is worse than not offering it.
        context["allowed"] = self.permissions.allowed_actions(self.request, self.panel.model)
        return context


class ListView(PanelView):
    template_name = "vantage/list.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") in FORMATS:
            return self.export(request)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        """Run a bulk action, or render its confirmation step."""
        from django.contrib import messages
        from django.shortcuts import redirect

        available = {a.name: a for a in resolve_actions(self.panel, request)}
        chosen = available.get(request.POST.get("action", ""))
        if chosen is None:
            # Either a stale form or an action the user may not run. Both deserve the
            # same answer: nothing happens, and we do not say which it was.
            raise PermissionDenied("That action is not available.")

        selection = Selection.from_request(request)
        if selection.is_empty:
            messages.warning(request, "Nothing was selected.")
            return redirect(request.get_full_path())

        queryset, _, _, _, _ = self.build_queryset(request)
        queryset = selection.apply(queryset)
        count = queryset.count()

        if count == 0:
            messages.warning(request, "Those records no longer match.")
            return redirect(request.get_full_path())

        if chosen.max_rows is not None and count > chosen.max_rows:
            messages.error(
                request,
                f"{chosen.label} is limited to {chosen.max_rows} records at a time; "
                f"{count} were selected.",
            )
            return redirect(request.get_full_path())

        if chosen.needs_confirmation() and request.POST.get("confirmed") != "1":
            return self.render_to_response(
                self.get_context_data(
                    confirming=chosen,
                    confirm_count=count,
                    confirm_scope=selection.describe(count),
                    confirm_text=chosen.confirmation_text(count),
                    confirm_selection=selection,
                )
            )

        from vantage import audit

        result = chosen.run(self.panel, request, queryset)
        audit.log_action(request, self.panel.model, chosen, count)
        # Report the scope that was actually resolved. Django's own admin once let
        # select-across silently fall back to page one on a destructive action.
        getattr(messages, result.level, messages.info)(
            request, f"{result.message} ({selection.describe(count)})"
        )
        return redirect(result.redirect or request.get_full_path())

    def build_queryset(self, request):
        """The rows the page is showing: permissions, search, filters and sort.

        Returns the *unfiltered* base alongside the final queryset, because the facet
        engine needs the base to compute a disjunctive count — the count for an
        unticked option has to ignore its own filter or it always reads zero.

        Shared with the export and the analytics strip so none of the three can
        disagree about which rows the page is about.
        """
        panel = self.panel
        patterns = panel.get_patterns()

        if self.showing_trash(request):
            queryset = self.permissions.filter_queryset(
                request, panel.get_trash_queryset(request), "view"
            )
        else:
            queryset = self.permissions.filter_queryset(
                request, panel.get_list_queryset(request), "view"
            )
            if patterns.soft_delete and not patterns.soft_delete.manager_hides_trashed:
                # A model whose manager does not already exclude them: the list means
                # live records, and the trash is a place you go to deliberately.
                queryset = queryset.filter(patterns.soft_delete.live)

        query = request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(self._search_q(panel, query, request))

        base = queryset
        filters = build_filters(panel, request)
        queryset = apply_filters(queryset, filters)

        sort = self._resolve_sort(panel, request)
        if sort:
            queryset = queryset.order_by(*sort["order_by"])
        elif self.showing_tree(request) and patterns.tree.depth_field:
            # Only the library trees have a column that sorts parent-before-child.
            # A plain self-FK is arranged in Python once the page is fetched.
            queryset = queryset.order_by(*patterns.tree.ordering)
        return queryset, base, filters, query, sort

    def export(self, request):
        panel = self.panel
        queryset, _, _, _, _ = self.build_queryset(request)
        columns = panel.get_list_display(request)
        headers = [self._column_header(panel, name, None)["label"] for name in columns]
        return stream_export(
            request.GET["export"], queryset, columns, headers, panel.get_label_plural()
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        panel = self.panel
        request = self.request

        queryset, base, filters, query, sort = self.build_queryset(request)

        if filters and facets_requested(request):
            from vantage.filters import apply_facet_counts

            apply_facet_counts(filters, base)

        columns = panel.get_list_display(request)
        link = panel.get_list_link(request)

        paginator = Paginator(queryset, panel.get_per_page(request))
        page = paginator.get_page(request.GET.get("page"))

        context.update(
            panel=panel,
            columns=[self._column_header(panel, name, sort) for name in columns],
            sort=sort,
            rows=self._rows(panel, page.object_list, columns, link, request),
            grouping=self.grouping(request),
            group_options=[("day", "Day"), ("week", "Week"), ("month", "Month"), ("year", "Year")],
            groups=self._groups(panel, page.object_list, request),
            timeline_field=self._timeline_field(panel),
            page=page,
            paginator=paginator,
            query=query,
            filters=filters,
            has_active_filters=any(f.is_active for f in filters),
            analytics=self.get_analytics(queryset),
            searchable=bool(panel.get_list_search(request)),
            patterns=panel.get_patterns(),
            showing_tree=self.showing_tree(request),
            showing_trash=self.showing_trash(request),
            trash_count=self.get_trash_count(panel, request),
            actions=resolve_actions(panel, request),
            summary=self.get_summary(panel, queryset, columns),
            page_title=panel.get_label_plural(),
        )
        from vantage import savedviews

        context.update(savedviews.list_context(request, panel))
        return context

    #: Grouping keys the list page accepts, mapped to the truncation to apply.
    GROUPINGS = {"day": "day", "week": "week", "month": "month", "year": "year"}

    @staticmethod
    def _timeline_field(panel) -> str | None:
        timeline = panel.get_patterns().timeline
        return timeline.field if timeline else None

    def grouping(self, request) -> str | None:
        """Which time bucket to group rows into, if any.

        Only offered when the model has a creation-ish date, and never while a tree
        is being drawn - two competing orderings would fight over the same rows.
        """
        wanted = request.GET.get("group")
        if wanted not in self.GROUPINGS or self.showing_tree(request):
            return None
        return wanted if self.panel.get_patterns().timeline else None

    def showing_tree(self, request) -> bool:
        """Whether to render the hierarchy rather than a flat page of rows.

        Off while searching, filtering or sorting: those all produce a subset, and a
        tree drawn from a subset shows children whose parents are missing, which
        reads as corruption rather than as a filtered view.
        """
        patterns = self.panel.get_patterns()
        if patterns.tree is None or request.GET.get("flat") == "1":
            return False
        if request.GET.get("q") or request.GET.get("sort"):
            return False
        return not any(
            key not in {"page", "trash", "flat", "_facets"} and value
            for key, value in request.GET.items()
        )

    def showing_trash(self, request) -> bool:
        """Whether this request is asking for the trash rather than the live list."""
        return (
            request.GET.get("trash") == "1"
            and self.panel.get_patterns().soft_delete is not None
        )

    def get_trash_count(self, panel, request) -> int | None:
        """How many soft-deleted rows there are, or None when the model has no trash."""
        if panel.get_patterns().soft_delete is None:
            return None
        return self.permissions.filter_queryset(
            request, panel.get_trash_queryset(request), "view"
        ).count()

    @staticmethod
    def get_summary(panel, queryset, columns):
        """The footer bar, keyed by column so the template can align it."""
        from vantage.summary import compute_summary, infer_summaries

        summaries = infer_summaries(panel, columns)
        return {cell.column: cell for cell in compute_summary(queryset, summaries)}

    def get_analytics(self, queryset) -> dict:
        """Stat cards and charts over the filtered rows.

        Computed from the same queryset the table is about, before pagination — never
        from the rendered rows, which would silently answer for the current page only.
        """
        from vantage.stats import compute_panel

        stats = self.panel.list_stats or ()
        charts = self.panel.list_charts or ()
        if not stats and not charts:
            return {"stats": [], "charts": [], "deferred": False, "reason": None}
        return compute_panel(queryset, stats=stats, charts=charts)

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _search_q(panel, query: str, request) -> Q:
        combined = Q()
        for path in panel.get_list_search(request):
            combined |= Q(**{f"{path.lstrip('^=@')}__icontains": query})
        return combined

    @staticmethod
    def _resolve_sort(panel, request) -> dict | None:
        """Validate ``?sort=`` against real, sortable field paths.

        A sort parameter goes straight into ``order_by``, so it is never trusted:
        anything that does not resolve to a concrete single-valued field is dropped.
        Sorting on a multi-valued path would silently duplicate rows.
        """
        from vantage.introspection import resolve_path

        raw = request.GET.get("sort", "").strip()
        if not raw:
            return None

        descending = raw.startswith("-")
        path = raw.lstrip("-")
        if path not in panel.get_list_display(request):
            return None

        hops = resolve_path(panel.model, path)
        if not hops or any(hop.is_multivalued for hop in hops):
            return None

        prefix = "-" if descending else ""
        # A tiebreaker on the primary key keeps pagination stable when the sort
        # column has duplicate values, which is the usual case for a status column.
        return {
            "path": path,
            "descending": descending,
            "order_by": [f"{prefix}{path}", "-pk"],
        }

    @staticmethod
    def _column_header(panel, name: str, sort: dict | None) -> dict:
        from vantage.introspection import resolve_path

        hops = resolve_path(panel.model, name)
        if hops:
            label = str(getattr(hops[-1].field, "verbose_name", name))
        else:
            label = name.replace("_", " ")

        sortable = bool(hops) and not any(hop.is_multivalued for hop in hops)
        active = bool(sort and sort["path"] == name)
        return {
            "name": name,
            "label": label.capitalize(),
            "sortable": sortable,
            "active": active,
            "descending": active and sort["descending"],
            # Clicking an active ascending column flips it; anything else starts ascending.
            "next": f"-{name}" if (active and not sort["descending"]) else name,
        }

    def _rows(self, panel, objects, columns, link, request) -> list[dict]:
        """Rows for the page, indented when rendering a hierarchy."""
        if not self.showing_tree(request):
            return [self._row(panel, obj, columns, link) for obj in objects]

        pattern = panel.get_patterns().tree
        objects = list(objects)

        # mptt and treebeard store an orderable path, so the database already returned
        # the rows in tree order. A plain self-FK has no such column - SQL cannot order
        # parent-before-child without recursion - so the page is arranged here instead.
        # That is correct within a page and honest across them: a child whose parent
        # sits on another page renders as a root rather than being hidden.
        if not pattern.depth_field:
            objects = self._arrange(pattern, objects)

        depths = self._depths(pattern, objects)
        built = [self._row(panel, obj, columns, link) for obj in objects]
        for row, obj in zip(built, objects, strict=True):
            row["depth"] = depths.get(obj.pk, 0)
        return built

    def _groups(self, panel, objects, request) -> list[dict] | None:
        """Row indices where a new time bucket starts, with its label.

        Computed from the rows already fetched rather than by a second grouped query:
        the page is at most a couple of hundred rows, and a query would have to
        re-apply every filter to stay consistent with what is on screen.
        """
        bucket = self.grouping(request)
        if not bucket:
            return None

        field = panel.get_patterns().timeline.field
        groups: list[dict] = []
        previous = object()

        for index, obj in enumerate(objects):
            value = getattr(obj, field, None)
            key = self._bucket_key(value, bucket)
            if key != previous:
                groups.append({"at": index, "label": self._bucket_label(value, bucket)})
                previous = key
        return groups

    @staticmethod
    def _bucket_key(value, bucket: str):
        if value is None:
            return None
        if bucket == "day":
            return value.date() if hasattr(value, "date") else value
        if bucket == "week":
            date = value.date() if hasattr(value, "date") else value
            return date.isocalendar()[:2]
        if bucket == "month":
            return (value.year, value.month)
        return value.year

    @staticmethod
    def _bucket_label(value, bucket: str) -> str:
        if value is None:
            return "No date"
        date = value.date() if hasattr(value, "date") else value
        if bucket == "day":
            return date.strftime("%A, %d %B %Y")
        if bucket == "week":
            year, week = date.isocalendar()[:2]
            return f"Week {week}, {year}"
        if bucket == "month":
            return date.strftime("%B %Y")
        return str(date.year)

    @staticmethod
    def _arrange(pattern, objects: list) -> list:
        """Depth-first order: each row followed by its children."""
        children: dict = {}
        present = {obj.pk for obj in objects}
        roots = []
        for obj in objects:
            parent = getattr(obj, f"{pattern.parent_field}_id", None)
            if parent in present:
                children.setdefault(parent, []).append(obj)
            else:
                roots.append(obj)

        ordered: list = []
        seen: set = set()

        def walk(nodes):
            for node in nodes:
                if node.pk in seen:  # a cycle in the data must not loop here
                    continue
                seen.add(node.pk)
                ordered.append(node)
                walk(children.get(node.pk, ()))

        walk(roots)
        # Anything left is inside a cycle; showing it beats dropping it silently.
        ordered.extend(obj for obj in objects if obj.pk not in seen)
        return ordered

    @staticmethod
    def _depths(pattern, objects) -> dict:
        """How deep each row sits, without a query per row.

        mptt and treebeard already store the depth, so use it. For a plain self-FK,
        walk the parent ids present on this page: a child whose parent is on another
        page simply starts at zero, which is the honest answer rather than a
        recursive lookup per row.
        """
        if pattern.depth_field:
            return {obj.pk: getattr(obj, pattern.depth_field, 0) or 0 for obj in objects}

        parents = {obj.pk: getattr(obj, f"{pattern.parent_field}_id", None) for obj in objects}
        depths: dict = {}
        for pk in parents:
            depth, cursor, guard = 0, parents[pk], 0
            while cursor in parents and guard < 32:
                depth += 1
                cursor = parents[cursor]
                guard += 1
            depths[pk] = depth
        return depths

    @staticmethod
    def _row(panel, obj, columns: list[str], link: str) -> dict:
        from django.urls import NoReverseMatch, reverse

        from vantage.render import render_cell

        try:
            url = reverse(f"vantage:{panel.slug}-detail", args=[obj.pk])
        except NoReverseMatch:  # pragma: no cover
            url = None

        return {
            "obj": obj,
            "pk": obj.pk,
            "url": url,
            "cells": [
                {
                    "value": render_cell(panel, obj, name),
                    "is_link": name == link,
                }
                for name in columns
            ],
        }


class DetailView(PanelView):
    """One record, read-only for now: its fields, then what points at it."""

    template_name = "vantage/detail.html"

    def get_object(self):
        from django.shortcuts import get_object_or_404

        queryset = self.permissions.filter_queryset(
            self.request, self.panel.get_queryset(self.request), "view"
        )
        return get_object_or_404(queryset, pk=self.kwargs["pk"])

    def get_breadcrumbs(self):
        from django.urls import reverse

        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (str(self.object), None),
        ]

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from vantage.introspection import displayable_fields
        from vantage.render import render_cell

        context = super().get_context_data(**kwargs)
        obj = self.object
        panel = self.panel

        fields = [
            {
                "label": str(getattr(field, "verbose_name", field.name)).capitalize(),
                "value": render_cell(panel, obj, field.name),
            }
            for field in displayable_fields(panel.model)
            if not _is_hidden(field)
        ]

        related = self._related(obj)

        context.update(
            object=obj,
            panel=panel,
            fields=fields,
            related=related,
            page_title=str(obj),
        )
        from vantage import audit

        context.update(audit.history_context(self.request, obj))
        return context


    #: How many related rows to preview before the panel becomes a link only.
    #: contrib.admin's inlines load every child row, which is what makes a record
    #: with fifty thousand children impossible to open. A preview plus a link costs
    #: two bounded queries per relation regardless of how many children exist.
    related_preview = 5

    def _related(self, obj) -> list[dict]:
        from vantage.introspection import reverse_relations

        blocks = []
        for rel in reverse_relations(obj._meta.model):
            accessor = rel.accessor_name
            if accessor is None:
                continue
            manager = getattr(obj, accessor, None)
            if manager is None or not hasattr(manager, "count"):
                continue

            child = rel.related_model
            registered = self.site.is_registered(child)
            child_panel = self.site.get_panel(child) if registered else None

            if child_panel is not None and not self.permissions.has_model_perm(
                self.request, child, "view"
            ):
                # Not merely hidden from view: a count is itself information, and
                # leaking "this customer has 47 orders" to someone who cannot see
                # orders is still a leak.
                continue

            rows, columns = [], []
            if child_panel is not None:
                columns = child_panel.get_list_display(self.request)[:4]
                queryset = self.permissions.filter_queryset(
                    self.request, manager.all(), "view"
                )
                from vantage.optimizer import plan_related

                # Only joins matter here: the preview is five rows, so a prefetch
                # would cost a query to save nothing.
                select, _ = plan_related(child, columns)
                if select:
                    queryset = queryset.select_related(*sorted(select))
                rows = [
                    self._related_row(child_panel, item, columns)
                    for item in queryset[: self.related_preview]
                ]

            blocks.append(
                {
                    "label": str(child._meta.verbose_name_plural).capitalize(),
                    "count": manager.count(),
                    "columns": [
                        ListView._column_header(child_panel, name, None)
                        for name in columns
                    ],
                    "rows": rows,
                    "more": max(0, manager.count() - self.related_preview),
                    "url": _related_url(self.site, rel, obj),
                }
            )
        return blocks

    def _related_row(self, child_panel, item, columns) -> dict:
        from django.urls import NoReverseMatch, reverse

        from vantage.render import render_cell

        try:
            url = reverse(f"vantage:{child_panel.slug}-detail", args=[item.pk])
        except NoReverseMatch:  # pragma: no cover
            url = None
        link = child_panel.get_list_link(self.request)
        return {
            "url": url,
            "cells": [
                {"value": render_cell(child_panel, item, name), "is_link": name == link}
                for name in columns
            ],
        }


def _is_hidden(field) -> bool:
    from vantage.inference import SENSITIVE

    return bool(SENSITIVE.search(field.name))


def _related_url(site, rel, obj) -> str | None:
    """A link through to the related model's list, pre-filtered to this record."""
    from django.urls import NoReverseMatch, reverse

    if not site.is_registered(rel.related_model):
        return None
    related_panel = site.get_panel(rel.related_model)
    try:
        base = reverse(related_panel.list_url_name)
    except NoReverseMatch:  # pragma: no cover
        return None
    return f"{base}?{rel.field.name}={obj.pk}"


class PageView(SiteView):
    """Renders a custom :class:`~vantage.pages.Page`."""

    page_class = None

    def get_page(self):
        return self.page_class(self.site)

    def check_permission(self, request):
        if self.get_page().has_permission(request):
            return None
        raise PermissionDenied(
            f"You do not have permission to view {self.page_class.get_title()}."
        )

    def get_template_names(self):
        return self.get_page().get_template_names()

    def get_breadcrumbs(self):
        return self.get_page().get_breadcrumbs()

    def get_context_data(self, **kwargs):
        page = self.get_page()
        context = super().get_context_data(**kwargs)
        context.update(
            page=page,
            page_title=page.get_title(),
            page_description=page.description,
        )
        # The page's own keys go on top, so a page can override anything the shell
        # supplied. Its data is the reason the page exists.
        context.update(page.get_context(self.request))
        # The generic renderer needs an ordered view of the data; a page with its own
        # template ignores this entirely.
        context.setdefault("page_data", _as_rows(page.get_context(self.request)))
        return context


def _as_rows(data: dict) -> list[dict]:
    """Shape an arbitrary context dict for the generic page renderer.

    Scalars become stat cards and everything else becomes a labelled block, which is
    enough to make a page with no template of its own worth looking at.
    """
    rows = []
    for key, value in data.items():
        label = str(key).replace("_", " ").capitalize()
        scalar = isinstance(value, (str, int, float, bool)) or value is None
        rows.append({"key": key, "label": label, "value": value, "scalar": scalar})
    return rows


class ImpersonateView(SiteView):
    """Start or stop impersonating another user. POST only, superusers only.

    Defined as a real ``post`` handler rather than a ``dispatch`` override so
    ``SiteView.dispatch`` runs the authentication check first and Django's own
    method routing produces the 405 for anything else.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        from django.contrib import messages
        from django.http import HttpResponseRedirect
        from django.shortcuts import get_object_or_404
        from django.urls import reverse

        target_pk = request.POST.get("user")
        try:
            if target_pk:
                from django.contrib.auth import get_user_model

                target = get_object_or_404(get_user_model(), pk=target_pk)
                start_impersonation(request, target)
                messages.warning(request, f"You are now acting as {target.get_username()}.")
            else:
                actor = stop_impersonation(request)
                messages.info(request, f"Returned to {actor.get_username()}.")
        except ImpersonationError as exc:
            messages.error(request, str(exc))

        return HttpResponseRedirect(request.POST.get("next") or reverse("vantage:dashboard"))


class ThemeView(SiteView):
    """Persist a theme choice. Posted to by the toggle."""

    def dispatch(self, request, *args, **kwargs):
        if request.method != "POST":
            return HttpResponse(status=405)
        theme = request.POST.get("theme")
        response = HttpResponse(status=204)
        if theme in ("light", "dark"):
            response.set_cookie(
                "vantage-theme", theme, max_age=60 * 60 * 24 * 365, samesite="Lax"
            )
        else:
            response.delete_cookie("vantage-theme")
        return response
