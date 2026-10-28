"""The request half of :mod:`vantage.history`: one record's timeline, and restoring from it.

One view, ``<record>/history/``. A GET shows every entry the audit log holds for the
record and, given a point in time — ``?entry=<id>`` for "just after this change", or
``?at=<datetime>`` — the record as it was then, field by field, next to how it is now.
A POST restores the fields that were ticked.

**Two gates for reading, because the page answers two questions.** What the record
held is governed by ``view`` on that record, fetched the way every record is (scoped
first, so an out-of-reach row is a 404). *Who* changed it is the audit log's own
question, and :func:`~vantage.audit.may_view_audit` answers it here exactly as it does
for the history panel on the detail page: someone who may not read the log does not
get a second way in through the history page.

**Restore is ``change`` on that record, rechecked on the POST.** Nothing the page
rendered is trusted on the way back: the point in time is re-read, the reconstruction
is recomputed, and the ticked names are intersected with what *it* says is restorable.
The values themselves never travel through the browser at all, so a hand-written POST
can choose which fields to restore but never what they are restored to.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import Http404, HttpResponseRedirect
from django.urls import NoReverseMatch, path, reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from vantage import history
from vantage.audit import may_view_audit
from vantage.sensitive import safe_str
from vantage.views import PanelView

__all__ = ["HistoryView", "build_urls", "history_url"]


def history_url(panel, obj) -> str | None:
    """The history page for ``obj``, or ``None`` when the panel has none."""
    if not history.enabled_for(panel):
        return None
    try:
        return reverse(f"vantage:{panel.slug}-history", args=[obj.pk])
    except NoReverseMatch:
        return None


class HistoryView(PanelView):
    """A record's audit timeline, the record as of a chosen point, and restore."""

    template_name = "vantage/history.html"
    required_action = "view"

    object = None

    def check_permission(self, request):
        if not history.enabled_for(self.panel):
            # Not mounted, as far as anyone can tell: ``history = False`` means the
            # page does not exist, not that it exists and refuses.
            raise Http404(_("This record has no history page."))
        denied = super().check_permission(request)
        if denied is not None:
            return denied
        if not may_view_audit(request):
            raise PermissionDenied(_("You do not have permission to read this record's history."))
        return None

    @property
    def page_title(self) -> str:
        if self.object is not None:
            return _("History of %(object)s") % {"object": safe_str(self.object, self.site)}
        return _("History")

    def get_object(self, action: str):
        from vantage.formviews import get_record

        return get_record(self.request, self.panel, self.permissions, self.kwargs["pk"], action)

    def detail_url(self) -> str | None:
        try:
            return reverse(f"vantage:{self.panel.slug}-detail", args=[self.object.pk])
        except NoReverseMatch:  # pragma: no cover - only when detail is not mounted
            return None

    def get_breadcrumbs(self):
        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (safe_str(self.object, self.site), self.detail_url()),
            (_("History"), None),
        ]

    def may_restore(self) -> bool:
        return history.restore_allowed(self.panel) and self.permissions.has_object_perm(
            self.request, self.panel.model, "change", self.object
        )

    def position_url(self, position) -> str:
        return f"{self.request.path}?{urlencode(position.query)}" if position else self.request.path

    # ── reading ─────────────────────────────────────────────────────────────

    def get(self, request, *args, **kwargs):
        self.object = self.get_object("view")
        entries, truncated = history.timeline(self.object)
        position = history.position_from(request.GET, entries)
        reconstruction = (
            history.reconstruct(self.panel, self.object, position, entries=entries)
            if position is not None
            else None
        )
        if reconstruction is not None:
            reconstruction.truncated = truncated
        return self.render_to_response(
            self.get_context_data(
                entries=entries,
                truncated=truncated,
                position=position,
                reconstruction=reconstruction,
                asked=bool(request.GET.get("entry") or request.GET.get("at")),
            )
        )

    def get_context_data(self, **kwargs):
        from vantage.concurrency import token_for

        context = super().get_context_data(**kwargs)
        entries = kwargs.get("entries") or []
        position = kwargs.get("position")
        reconstruction = kwargs.get("reconstruction")
        may_restore = self.may_restore()
        context.update(
            panel=self.panel,
            object=self.object,
            object_label=safe_str(self.object, self.site),
            detail_url=self.detail_url(),
            # Newest first, as the detail page's panel lists them.
            timeline=[
                {
                    "entry": entry,
                    "url": f"{self.request.path}?{urlencode({'entry': entry.pk})}",
                    "current": position is not None and position.entry_id == entry.pk,
                }
                for entry in reversed(entries)
            ],
            at_value=_local_input(position.at) if position is not None else "",
            may_restore=may_restore,
            offer_restore=bool(
                may_restore
                and reconstruction is not None
                and reconstruction.status == "ok"
                and reconstruction.restorable
            ),
            version=token_for(self.object) or "",
            fingerprint=history.fingerprint(reconstruction) if reconstruction else "",
            position_fields=position.query.items() if position is not None else (),
            replay_limit=history.REPLAY_LIMIT,
        )
        return context

    # ── restoring ───────────────────────────────────────────────────────────

    def post(self, request, *args, **kwargs):
        if not history.restore_allowed(self.panel):
            raise PermissionDenied(_("Restoring from history is switched off for this model."))
        if not self.permissions.has_model_perm(request, self.panel.model, "change"):
            raise PermissionDenied(
                _("You do not have permission to change %(records)s.")
                % {"records": self.panel.get_label_plural().lower()}
            )
        self.object = obj = self.get_object("change")
        entries, truncated = history.timeline(obj)
        position = history.position_from(request.POST, entries)
        if position is None:
            messages.error(request, _("Choose a point in this record's history to restore from."))
            return HttpResponseRedirect(request.path)
        back = self.position_url(position)

        reconstruction = history.reconstruct(self.panel, obj, position, entries=entries)
        if reconstruction.status != "ok":
            messages.error(request, _("There is nothing to restore from that point."))
            return HttpResponseRedirect(back)
        if request.POST.get("fingerprint", "") != history.fingerprint(reconstruction):
            messages.error(
                request,
                _(
                    "This record has changed since you opened its history, so nothing was "
                    "restored. Check the values again."
                ),
            )
            return HttpResponseRedirect(back)

        restorable = {state.name for state in reconstruction.restorable}
        names = [name for name in request.POST.getlist("fields") if name in restorable]
        names = list(dict.fromkeys(names))
        if not names:
            messages.warning(request, _("Choose at least one field to restore."))
            return HttpResponseRedirect(back)

        from vantage.concurrency import StaleWrite
        from vantage.formviews import _control_class, save_form
        from vantage.widgets import build_form_class

        form = build_form_class(self.panel, names)(
            data=history.restore_data(reconstruction, names), instance=obj
        )
        for form_field in form.fields.values():
            form_field.widget.attrs.setdefault("class", _control_class(form_field.widget))
        if not form.is_valid():
            # The old value is no longer acceptable — a choice removed, a unique value
            # taken since. Shown on the page, next to the values, rather than saved.
            reconstruction.truncated = truncated
            return self.render_to_response(
                self.get_context_data(
                    entries=entries,
                    truncated=truncated,
                    position=position,
                    reconstruction=reconstruction,
                    asked=True,
                    restore_errors=_errors(form, reconstruction),
                ),
                status=422,
            )

        try:
            save_form(
                request,
                self.panel,
                form,
                change=True,
                # The token the page was drawn with. A lockable record that moved since
                # conflicts here, inside the save's own transaction.
                version=request.POST.get("version", ""),
                action_name=f"{history.RESTORE_PREFIX}{position.at.isoformat()}",
            )
        except StaleWrite:
            messages.error(
                request,
                _(
                    "Someone changed this record after you opened its history, so nothing "
                    "was restored. Their change has been kept."
                ),
            )
            return HttpResponseRedirect(back)
        except IntegrityError:
            messages.error(
                request,
                _("The old values conflict with another record, so nothing was restored."),
            )
            return HttpResponseRedirect(back)

        messages.success(
            request,
            ngettext(
                "Restored %(count)d field of “%(object)s” to its value as of %(when)s.",
                "Restored %(count)d fields of “%(object)s” to their values as of %(when)s.",
                len(names),
            )
            % {
                "count": len(names),
                "object": safe_str(obj, self.site),
                "when": _local_label(position.at),
            },
        )
        return HttpResponseRedirect(self.detail_url() or back)


def _errors(form, reconstruction) -> list[str]:
    labels = {state.name: state.label for state in reconstruction.fields}
    out = []
    for name, errors in form.errors.items():
        label = labels.get(name) or ""
        for error in errors:
            out.append(f"{label}: {error}" if label else str(error))
    return out


def _local(value):
    return timezone.localtime(value) if timezone.is_aware(value) else value


def _local_input(value) -> str:
    """The value a ``datetime-local`` input wants: local time, no offset, seconds kept."""
    return _local(value).strftime("%Y-%m-%dT%H:%M:%S")


def _local_label(value) -> str:
    return _local(value).strftime("%Y-%m-%d %H:%M")


def build_urls(site) -> list:
    """One route per panel, mounted before the greedy ``<path:pk>/`` detail route."""
    urlpatterns = []
    for panel in site.panels():
        opts = panel.opts
        prefix = f"{opts.app_label}/{opts.model_name}/"
        urlpatterns.append(
            path(
                f"{prefix}<path:pk>/history/",
                HistoryView.as_view(site=site, panel=panel),
                name=f"{panel.slug}-history",
            )
        )
    return urlpatterns
