"""The request half of :mod:`vantage.records`: duplicate, row action, bulk edit.

Three views, and each of them exists to reuse something rather than to build it.

:class:`DuplicateView` is :class:`~vantage.formviews.CreateView` with an ``initial``
dict. That is the whole feature: a copy is a create whose form arrives filled in, so it
gets the same template, the same widgets, the same validation and the same save path.
Anything else would be a second create page that drifts.

:class:`RowActionView` runs an ``@action`` over exactly one row. It subclasses
:class:`~vantage.views.ListView` so that the confirmation step is the confirmation step
the bulk bar already has — same card, same wording, same POST-with-``confirmed=1``
contract. A row action that deleted on a GET would be a link a crawler can fire, so the
button posts, the confirmation posts, and this view refuses GET outright by redirecting
back to the list.

:class:`BulkEditView` takes a selection and sets one or more fields across it. The
selection arrives as :class:`~vantage.actions.Selection` — a predicate plus an exclusion
set — and stays a queryset the whole way down, so "all 40,000 matching" costs the same
POST body as three ticked rows.

**Permissions are checked at the level the question has one answer.** Entry to all three
is a model-level check, once. The row action's verb is checked at model level once by
:func:`~vantage.records.resolve_row_actions`, then at object level exactly once — on the
one row it is about to touch, which is the only place the answer can legitimately differ
per row. The bulk edit does no object-level checking at all and must not: N rows means N
backend calls, which is the N+1 :mod:`vantage.permissions` exists to prevent. Row scoping
for a set is ``filter_queryset``'s job, and it has already run inside ``build_queryset``.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _

from vantage import listprefs, records
from vantage.actions import Selection
from vantage.formviews import PanelFormView, _control_class
from vantage.views import ListView, PanelView
from vantage.widgets import FieldKind, classify

__all__ = ["BulkEditView", "DuplicateView", "RowActionView", "build_urls"]


def _with_query(path: str, request) -> str:
    """``path`` carrying this request's query string, so filters survive a redirect."""
    query = request.GET.urlencode()
    return f"{path}?{query}" if query else path


# ── duplicate ───────────────────────────────────────────────────────────────


class DuplicateView(PanelFormView):
    """"Save as new": the create form, pre-filled from an existing record.

    The permission required is ``add``, not ``change`` — the outcome of this page is a
    new row, and someone who may edit orders but not create them must not be able to
    make one by copying. The *source* is fetched through the ``view`` scope for the
    reason :class:`~vantage.formviews.RecordView` gives: a row you may not see does not
    exist, and answering 403 rather than 404 would confirm that it does.
    """

    required_action = "add"
    change = False
    submit_label = _("Create copy")

    source = None

    @property
    def page_title(self) -> str:
        if self.source is not None:
            return _("Duplicate %(object)s") % {"object": self.source}
        return _("Duplicate")

    def get_source(self):
        queryset = self.permissions.filter_queryset(
            self.request, self.panel.get_queryset(self.request), "view"
        )
        obj = get_object_or_404(queryset, pk=self.kwargs["pk"])
        if not self.permissions.has_object_perm(
            self.request, self.panel.model, "view", obj
        ):
            raise PermissionDenied(
                _("You do not have permission to view this %(model)s.")
                % {"model": self.panel.get_label().lower()}
            )
        return obj

    @property
    def plan(self) -> records.DuplicatePlan:
        cached = getattr(self, "_plan", None)
        if cached is None:
            cached = records.duplicate_plan(
                self.panel, self.source, self.get_editable_field_names()
            )
            self._plan = cached
        return cached

    # ── request cycle ───────────────────────────────────────────────────────

    def get(self, request, *args, **kwargs):
        self.source = self.get_source()
        self.object = None
        return self.render_to_response(self.get_context_data(form=self.get_form()))

    def post(self, request, *args, **kwargs):
        self.source = self.get_source()
        self.object = None
        return super().post(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        # Only on GET. Seeding ``initial`` on a POST would make a field the person
        # deliberately cleared spring back to the source's value.
        if self.request.method != "POST":
            kwargs["initial"] = dict(self.plan.initial)
        return kwargs

    def chosen_children(self) -> list[records.ChildGroup]:
        """The reverse relations the person ticked. Never a default — see the plan."""
        wanted = set(self.request.POST.getlist("_copy_children"))
        return [group for group in self.plan.children if group.accessor in wanted]

    def save_form(self, form, *args, **kwargs):
        obj = super().save_form(form, *args, **kwargs)
        self._copied = records.copy_children(
            self.request, self.source, obj, self.chosen_children()
        )
        return obj

    def get_success_message(self, obj) -> str:
        base = _("Created %(model)s “%(object)s” as a copy of “%(source)s”.") % {
            "model": self.panel.get_label().lower(),
            "object": obj,
            "source": self.source,
        }
        copied = getattr(self, "_copied", None) or {}
        total = sum(copied.values())
        if not total:
            return base
        return _("%(base)s %(count)s related record(s) were copied with it.") % {
            "base": base,
            "count": total,
        }

    # ── presentation ────────────────────────────────────────────────────────

    def get_cancel_url(self) -> str:
        try:
            return reverse(f"vantage:{self.panel.slug}-detail", args=[self.kwargs["pk"]])
        except Exception:  # pragma: no cover - only when detail is not mounted
            return reverse(self.panel.list_url_name)

    def get_breadcrumbs(self):
        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (str(self.source), self.get_cancel_url()),
            (_("Duplicate"), None),
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            # Read by ``records.panel_slot`` through the ``before_form`` slot, which is
            # why the notes need no change to ``form.html``.
            duplicate_notes=self.plan.notes,
            duplicate_source=self.source,
            duplicate_children=self.plan.children,
            source=self.source,
        )
        return context


# ── one action, one row ─────────────────────────────────────────────────────


class RowActionView(ListView):
    """Run one of the panel's actions against a single record.

    Subclassing :class:`~vantage.views.ListView` is what makes the confirmation free:
    the list template already renders a confirm card from ``confirming`` and posts it
    back with ``confirmed=1``, and re-implementing that here would be a second dialogue
    with its own wording for the same decision.
    """

    def get(self, request, *args, **kwargs):
        """A row action is never a GET. Bounce back to the list rather than 405.

        Reached by the Cancel link on the confirmation card, which points at the current
        URL — so this is the ordinary way out of a confirmation, not an error path.
        """
        return HttpResponseRedirect(self.back_url())

    def back_url(self) -> str:
        return _with_query(reverse(self.panel.list_url_name), self.request)

    def post(self, request, *args, **kwargs):
        listprefs.resolve(self.panel, request)
        chosen = self.chosen_action(request)
        obj = self.get_object(request, chosen)

        if chosen.needs_confirmation() and request.POST.get("confirmed") != "1":
            return self.render_to_response(
                self.get_context_data(
                    confirming=chosen,
                    confirm_count=1,
                    confirm_scope=str(obj),
                    confirm_text=chosen.confirmation_text(1),
                    confirm_selection=None,
                    confirm_object=obj,
                )
            )

        from vantage import audit

        queryset = self.scoped_queryset(request).filter(pk=obj.pk)
        result = chosen.run(self.panel, request, queryset)
        audit.log_action(request, self.panel.model, chosen, 1)
        getattr(messages, result.level, messages.info)(
            request,
            _("%(message)s (%(object)s)") % {"message": result.message, "object": obj},
        )
        return HttpResponseRedirect(result.redirect or self.back_url())

    # ── resolution ──────────────────────────────────────────────────────────

    def chosen_action(self, request):
        """The posted action, if the panel declares it as a row action and may run it.

        Resolved through :func:`~vantage.records.resolve_row_actions` rather than
        :func:`~vantage.actions.resolve_actions`, so ``row_actions`` is a real boundary:
        an action offered in the bulk bar is not thereby reachable one row at a time.
        The model-level permission check happens inside that call, once.
        """
        name = request.POST.get("action", "")
        for spec, _permission in records.resolve_row_actions(self.panel, request):
            if not isinstance(spec, records.BuiltinRowAction) and spec.name == name:
                return spec
        # Either a stale page or an action this user may not run. Both get the same
        # answer, and it does not say which.
        raise PermissionDenied(_("That action is not available."))

    def scoped_queryset(self, request):
        """The rows this action may reach: the trash when the page was showing it."""
        panel = self.panel
        base = (
            panel.get_trash_queryset(request)
            if self.showing_trash(request)
            else panel.get_queryset(request)
        )
        return self.permissions.filter_queryset(request, base, "view")

    def get_object(self, request, chosen):
        """The one row, with the one object-level check that can differ between rows."""
        obj = get_object_or_404(self.scoped_queryset(request), pk=self.kwargs["pk"])
        verb = chosen.permission
        if verb is not None and not self.permissions.has_object_perm(
            request, self.panel.model, verb, obj
        ):
            raise PermissionDenied(
                _("You do not have permission to %(action)s this %(model)s.")
                % {"action": verb, "model": self.panel.get_label().lower()}
            )
        return obj


# ── bulk edit ───────────────────────────────────────────────────────────────


class BulkEditView(PanelView):
    """Set one or more fields across a selection, in two posts.

    The first post carries the selection and renders the picker; the second carries the
    selection *and* the values and performs the write. Both are POSTs, because the
    selection is not addressable — it is a predicate plus exclusions, and putting forty
    thousand rows' worth of intent in a URL is how it stops being one.
    """

    template_name = "vantage/bulk_edit.html"
    required_action = "change"
    page_title = _("Bulk edit")

    def get(self, request, *args, **kwargs):
        """Nothing to edit: a selection only exists inside the POST that carried it."""
        return HttpResponseRedirect(self.back_url())

    def back_url(self) -> str:
        return _with_query(reverse(self.panel.list_url_name), self.request)

    def action_url(self) -> str:
        return _with_query(
            reverse(f"vantage:{self.panel.slug}-bulk-edit"), self.request
        )

    def post(self, request, *args, **kwargs):
        listprefs.resolve(self.panel, request)
        selection = Selection.from_request(request)
        if selection.is_empty:
            messages.warning(request, _("Nothing was selected."))
            return HttpResponseRedirect(self.back_url())

        queryset = selection.apply(self.resolve_queryset(request))
        count = queryset.count()
        if not count:
            messages.warning(request, _("Those records no longer match."))
            return HttpResponseRedirect(self.back_url())

        available = records.bulk_edit_fields(self.panel)
        if not available:
            messages.error(
                request,
                _("There is nothing on %(model)s that can be set in bulk.")
                % {"model": self.panel.get_label_plural().lower()},
            )
            return HttpResponseRedirect(self.back_url())

        names = records.ticked_fields(request.POST, available)
        if request.POST.get("_bulk_apply") != "1":
            return self.render_picker(request, selection, count, available)

        if not names:
            messages.warning(request, _("Tick the field you want to change."))
            return self.render_picker(request, selection, count, available)

        form = records.bulk_edit_form_class(self.panel, names)(
            data=request.POST, prefix=records.BULK_EDIT_PREFIX
        )
        if not form.is_valid():
            return self.render_picker(request, selection, count, available, bound=True)

        outcome = records.run_bulk_edit(
            request,
            self.panel,
            queryset,
            form,
            names,
            since=parse_datetime(request.POST.get("since") or "") or None,
        )
        getattr(messages, outcome.level, messages.info)(request, outcome.message)
        for detail in outcome.errors:
            messages.error(request, detail)
        if outcome.refused:
            return self.render_picker(request, selection, count, available, bound=True)
        return HttpResponseRedirect(self.back_url())

    # ── the queryset ────────────────────────────────────────────────────────

    def resolve_queryset(self, request):
        """The rows the list page was showing, resolved by the list page's own code.

        :meth:`vantage.views.ListView.build_queryset` is permissions, search, filters,
        trash and sort in one call. Reimplementing any of it here would be a second
        answer to "which rows is this about", and the two would disagree the first time
        somebody adds a filter — which for a write is not a cosmetic disagreement.
        """
        view = ListView(site=self.site, panel=self.panel)
        view.request = request
        return view.build_queryset(request)[0]

    # ── the picker ──────────────────────────────────────────────────────────

    def render_picker(self, request, selection, count: int, available, *, bound=False):
        form_class = records.bulk_edit_form_class(self.panel, available)
        form = (
            form_class(data=request.POST, prefix=records.BULK_EDIT_PREFIX)
            if bound
            else form_class(prefix=records.BULK_EDIT_PREFIX)
        )
        # The same control classes the change form uses, read off the widget the shared
        # spec already built. A bulk screen that styled its own inputs would be a second
        # answer to "how is a decimal edited", and the two would diverge.
        for field in form.fields.values():
            field.widget.attrs.setdefault("class", _control_class(field.widget))

        ticked = set(records.ticked_fields(request.POST, available))
        if bound:
            # A field nobody ticked is not part of this edit, so its "this is required"
            # is not a complaint about anything the person did. Keeping it would put a
            # red box on every untouched control on the page.
            for name in list(form.errors):
                if name not in ticked:
                    del form.errors[name]

        entries = []
        for name in available:
            model_field = self.panel.model._meta.get_field(name)
            kind = classify(model_field)
            entries.append(
                {
                    "name": name,
                    "kind": str(kind),
                    "wide": kind in {FieldKind.LONGTEXT, FieldKind.RICHTEXT, FieldKind.JSON},
                    "readonly": False,
                    "bound": form[name],
                    "ticked": name in ticked,
                }
            )

        return self.render_to_response(
            self.get_context_data(
                form=form,
                entries=entries,
                selection=selection,
                scope=selection.describe(count),
                count=count,
                action_url=self.action_url(),
                cancel_url=self.back_url(),
                # Read back on apply and compared *inside* the UPDATE. Captured now
                # rather than at selection time because now is the last moment the
                # person could have seen the rows they are about to change.
                since=timezone.now().isoformat(),
                enable_name=records.BULK_EDIT_ENABLE,
            )
        )

    def get_breadcrumbs(self):
        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (_("Bulk edit"), None),
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("panel", self.panel)
        return context


# ── mounting ────────────────────────────────────────────────────────────────


def build_urls(site) -> list:
    """Routes for every panel on ``site``, in the order ``vantage.urls`` needs them.

    ``_bulk-edit`` is prefixed with an underscore for the reason ``_prefs`` is: the
    detail route's ``<path:pk>`` is greedy, and a model whose primary key is a slug
    would otherwise have a record called "bulk-edit" shadow the feature.
    """
    from django.urls import path

    urlpatterns = []
    for panel in site.panels():
        opts = panel.opts
        prefix = f"{opts.app_label}/{opts.model_name}/"
        urlpatterns.extend(
            [
                path(
                    f"{prefix}_bulk-edit/",
                    BulkEditView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-bulk-edit",
                ),
                path(
                    f"{prefix}<path:pk>/duplicate/",
                    DuplicateView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-duplicate",
                ),
                path(
                    f"{prefix}<path:pk>/action/",
                    RowActionView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-row-action",
                ),
            ]
        )
    return urlpatterns
