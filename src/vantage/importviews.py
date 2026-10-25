"""The import wizard: upload, map, preview, commit.

Four screens, and the order is the whole design. Nothing is written until the user has
seen, in the same units they will be asked to approve, exactly what would change — a
count per outcome and a before/after diff. An importer that goes straight from "choose
file" to "done" is a bulk edit performed blind against production.

The wizard keeps no server-side state beyond the uploaded bytes. The mapping travels in
the form and is re-derived from the model on every single request through
:func:`~vantage.importer.resolve_mapping`, so what the preview validated and what the
commit writes are produced by the same check rather than by a trusted hand-off. The
file itself lives in a private temp directory under a random token bound to its
uploader — see the stash section of :mod:`vantage.importer` for why not the session and
why not storage.

Permissions are layered rather than checked once. ``required_action = "add"`` gates
every screen, because the ability to bulk-create is the floor for being here at all;
then :meth:`ImportView.check_mode` re-checks the verbs the chosen mode actually needs,
so a user with ``add`` but not ``change`` can create records and cannot upsert. Both
checks run again on the commit request, since a mode posted to it is as untrusted as
one posted to the preview. Read-only mode needs no handling here: it is enforced in the
permission layer, so ``add`` is already refused and the wizard is unreachable.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from vantage import importer
from vantage.views import PanelView

__all__ = [
    "ImportCommitView",
    "ImportErrorsView",
    "ImportMapView",
    "ImportPreviewView",
    "ImportUploadView",
    "build_import_urls",
]

#: Errors listed on the page before the reader is pointed at the CSV instead. A wall of
#: three thousand messages is not a report, and the download is the right shape for it.
ERROR_PREVIEW = 50

#: One whole sentence per permission verb, rather than one sentence with the verb
#: interpolated. ``add`` and ``change`` are the only verbs :data:`~vantage.importer.MODES`
#: names, and a bare English verb dropped into ``"permission to %(verb)s"`` gives the
#: translator no case, aspect or word order to work with — several languages need a
#: different construction for each. Two messages is cheaper than an unusable one.
PERMISSION_DENIED: dict[str, str] = {
    "add": _("You do not have permission to add %(objects)s."),
    "change": _("You do not have permission to change %(objects)s."),
}

#: Reached only if a mode is added with a verb the dict above does not name.
PERMISSION_DENIED_FALLBACK = _("You do not have permission to import %(objects)s.")


class ImportView(PanelView):
    """Shared ground: the stash, the mapping, and the permission the mode needs."""

    #: Being here at all means being able to create records in bulk. Update-only and
    #: upsert additionally require "change", checked per request in :meth:`check_mode`.
    required_action = "add"

    @property
    def model_label(self) -> str:
        opts = self.panel.opts
        return f"{opts.app_label}.{opts.model_name}"

    @property
    def token(self) -> str:
        return self.kwargs.get("token", "")

    # ── urls ────────────────────────────────────────────────────────────────

    def url(self, suffix: str, *args) -> str:
        return reverse(f"vantage:{self.panel.slug}-{suffix}", args=args)

    def upload_url(self) -> str:
        return self.url("import")

    def list_url(self) -> str:
        return reverse(self.panel.list_url_name)

    # ── the file ────────────────────────────────────────────────────────────

    @property
    def max_bytes(self) -> int:
        return importer.limit("IMPORT_MAX_BYTES", importer.MAX_BYTES)

    @property
    def max_rows(self) -> int:
        return importer.limit("IMPORT_MAX_ROWS", importer.MAX_ROWS)

    def load(self):
        """The stashed upload and its parsed form, or :class:`StashExpired`.

        Re-parsed on every step rather than carried between them. Parsing is cheap next
        to the row loop, and a re-parse cannot go stale against a file that a later
        request believes has different columns.
        """
        stashed = importer.read_stash(
            user=self.request.user, model_label=self.model_label, token=self.token
        )
        parsed = importer.read_csv(stashed.raw, max_bytes=self.max_bytes, max_rows=self.max_rows)
        return stashed, parsed

    def expired(self, exc) -> HttpResponseRedirect:
        messages.warning(self.request, str(exc))
        return HttpResponseRedirect(self.upload_url())

    # ── the mapping ─────────────────────────────────────────────────────────

    def options(self):
        return importer.importable_fields(self.panel, self.request, self.permissions)

    def chosen(self, parsed) -> dict[str, str]:
        """Read the submitted mapping.

        Inputs are named by column *index*, not by header: a header is arbitrary text
        from a file — it can contain dots, brackets or a newline — and using it as a
        form field name invites a parsing bug at exactly the point where the value
        decides which model field gets written.
        """
        post = self.request.POST
        return {
            header: post.get(f"map-{index}", "").strip()
            for index, header in enumerate(parsed.headers)
        }

    def mapping(self, parsed):
        return importer.resolve_mapping(
            self.panel,
            parsed,
            self.chosen(parsed),
            key_field=self.request.POST.get("key_field", ""),
            mode=self.request.POST.get("mode", ""),
            request=self.request,
            permissions=self.permissions,
        )

    def check_mode(self, mode: str) -> None:
        """Re-check the verbs this mode needs, on this request.

        Not folded into ``required_action``: which permissions an import needs is
        decided by a radio button, and a radio button is user input.
        """
        needed = importer.MODES.get(mode)
        if needed is None:  # pragma: no cover - resolve_mapping refuses first
            raise PermissionDenied(_("Unknown import mode."))
        allowed = self.permissions.allowed_actions(self.request, self.panel.model)
        missing = [verb for verb in needed if verb not in allowed]
        if missing:
            sentence = PERMISSION_DENIED.get(missing[0], PERMISSION_DENIED_FALLBACK)
            raise PermissionDenied(sentence % {"objects": self.panel.get_label_plural().lower()})

    def allowed_modes(self) -> list[dict]:
        allowed = self.permissions.allowed_actions(self.request, self.panel.model)
        return [
            {"value": mode, "label": importer.MODE_LABELS[mode]}
            for mode, needed in importer.MODES.items()
            if set(needed) <= allowed
        ]

    def plan(self, parsed, chosen=None, key_field=None, mode=None) -> dict:
        """Everything the mapping form needs: inferred, or echoed back after a refusal.

        Lives on the base view because the preview has to be able to render step 2
        again when a submitted mapping turns out to be unusable, and the two screens
        must not disagree about what the options are.
        """
        options = self.options()
        inferred = chosen if chosen is not None else importer.infer_mapping(parsed.headers, options)
        samples = parsed.sample(3)
        pk_name = self.panel.model._meta.pk.name

        columns = [
            {
                "index": index,
                "header": header,
                "target": inferred.get(header, ""),
                "samples": [row[index] for row in samples],
            }
            for index, header in enumerate(parsed.headers)
        ]

        # Every unique field is offered, not only the ones the inferred mapping happens
        # to feed: the user may be about to map one in the select beside it, and a list
        # computed from the previous guess would make that a dead end. Choosing one that
        # no column feeds is refused by ``resolve_mapping`` with a sentence saying so.
        keys = importer.key_options(options)
        mapped = {value for value in inferred.values() if value}
        default_key = (
            key_field
            if key_field is not None
            else _default_key([key for key in keys if key.name in mapped], pk_name)
        )

        modes = self.allowed_modes()
        names = [entry["value"] for entry in modes]
        # A file carrying primary keys came out of an export of this same table, so
        # upsert is what its owner means. Anything else defaults to the safe reading:
        # create, which can only add rows.
        preferred = "upsert" if default_key == pk_name else "create"
        if preferred not in names:
            preferred = names[0] if names else ""

        return {
            "options": options,
            "columns": columns,
            "keys": keys,
            "default_key": default_key,
            "modes": modes,
            "default_mode": mode or preferred,
            "row_count": parsed.row_count,
            "delimiter": parsed.delimiter,
            "encoding": parsed.encoding,
            "preview_url": self.url("import-preview", self.token),
        }

    def render_map(self, parsed, error: str) -> HttpResponse:
        """Step 2 again, carrying the reason the submitted mapping was refused."""
        self.template_name = ImportMapView.template_name
        context = self.get_context_data(
            parsed=parsed,
            error=error,
            **self.plan(
                parsed,
                chosen=self.chosen(parsed),
                key_field=self.request.POST.get("key_field", ""),
                mode=self.request.POST.get("mode", ""),
            ),
        )
        return self.render_to_response(context, status=400)

    # ── presentation ────────────────────────────────────────────────────────

    @property
    def page_title(self) -> str:
        return _("Import %(objects)s") % {"objects": self.panel.get_label_plural().lower()}

    def get_breadcrumbs(self):
        # A lone "Import" is a verb and a noun in English and only one of the two in
        # most other languages, and the crumb is the noun (the page you are on), not
        # the button. Nothing else in the wizard is ambiguous enough to need context.
        return [
            (self.panel.get_label_plural(), self.list_url()),
            (pgettext_lazy("breadcrumb", "Import"), None),
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            panel=self.panel,
            token=self.token,
            upload_url=self.upload_url(),
            list_url=self.list_url(),
        )
        return context


def _default_key(keys, pk_name: str) -> str:
    """Prefer the primary key, then the first unique column that is actually present."""
    for option in keys:
        if option.name == pk_name:
            return option.name
    return keys[0].name if keys else ""


class ImportUploadView(ImportView):
    """Step 1 — choose a file, and be told what a good one looks like."""

    template_name = "vantage/import_upload.html"

    def post(self, request, *args, **kwargs):
        discard = request.POST.get("discard")
        if discard:
            importer.discard_stash(discard)
            messages.info(request, _("That upload was discarded."))
            return HttpResponseRedirect(self.upload_url())

        upload = request.FILES.get("file")
        if upload is None:
            return self.reject(_("Choose a CSV file to import."))
        # Checked before reading: the point of a size cap is not to read the file.
        if upload.size > self.max_bytes:
            return self.reject(
                _("That file is %(size)s MB; the limit is %(limit)s MB.")
                % {
                    "size": f"{upload.size / (1024 * 1024):.1f}",
                    "limit": f"{self.max_bytes / (1024 * 1024):.0f}",
                }
            )

        raw = upload.read()
        try:
            importer.read_csv(raw, max_bytes=self.max_bytes, max_rows=self.max_rows)
        except importer.ImportRejected as exc:
            return self.reject(str(exc))

        token = importer.stash_upload(
            user=request.user,
            model_label=self.model_label,
            filename=upload.name or "import.csv",
            raw=raw,
        )
        return HttpResponseRedirect(self.url("import-map", token))

    def reject(self, message: str) -> HttpResponse:
        """Re-render with the reason, at 400.

        The status matters as much as the message: a scripted caller posting a bad file
        should be able to tell refusal from success without parsing HTML.
        """
        return self.render_to_response(self.get_context_data(error=message), status=400)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        options = self.options()
        context.update(
            options=options,
            max_rows=self.max_rows,
            max_mb=int(self.max_bytes / (1024 * 1024)),
        )
        return context


class ImportMapView(ImportView):
    """Step 2 — point each column at a field, choose a key, choose a mode."""

    template_name = "vantage/import_map.html"

    def get(self, request, *args, **kwargs):
        try:
            stashed, parsed = self.load()
        except (importer.StashExpired, importer.ImportRejected) as exc:
            return self.expired(exc)
        return self.render_to_response(
            self.get_context_data(stashed=stashed, parsed=parsed, **self.plan(parsed))
        )


class ImportPreviewView(ImportView):
    """Step 3 — the dry run, and the only thing standing between a file and the table."""

    template_name = "vantage/import_preview.html"
    done = False

    def post(self, request, *args, **kwargs):
        try:
            stashed, parsed = self.load()
        except (importer.StashExpired, importer.ImportRejected) as exc:
            return self.expired(exc)

        try:
            mapping = self.mapping(parsed)
        except importer.ImportRejected as exc:
            return self.render_map(parsed, str(exc))
        self.check_mode(mapping.mode)

        return self.run(request, stashed, parsed, mapping)

    def run(self, request, stashed, parsed, mapping) -> HttpResponse:
        result = importer.dry_run(self.panel, parsed, mapping, request, self.permissions)
        if result.errors:
            importer.stash_errors(token=self.token, result=result)
        return self.render_to_response(
            self.get_context_data(stashed=stashed, parsed=parsed, mapping=mapping, result=result)
        )

    def hidden(self, mapping, parsed) -> list[dict]:
        """The mapping, re-serialised for the next form.

        Echoed from the *resolved* mapping rather than from the POST, so a column that
        was dropped as unwritable is not quietly carried forward to a request that
        might resolve it differently.
        """
        fields = [
            {"name": f"map-{index}", "value": mapping.columns.get(header, "")}
            for index, header in enumerate(parsed.headers)
        ]
        if mapping.key_column is not None:
            index = parsed.column(mapping.key_column)
            fields[index] = {"name": f"map-{index}", "value": mapping.key_field}
        fields.append({"name": "key_field", "value": mapping.key_field or ""})
        fields.append({"name": "mode", "value": mapping.mode})
        return fields

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        result = context.get("result")
        mapping = context.get("mapping")
        parsed = context.get("parsed")
        if result is None:  # pragma: no cover - both are always supplied together
            return context
        context.update(
            errors_shown=result.errors[:ERROR_PREVIEW],
            errors_hidden=max(0, len(result.errors) - ERROR_PREVIEW),
            commit_url=self.url("import-commit", self.token),
            errors_url=self.url("import-errors", self.token),
            map_url=self.url("import-map", self.token),
            hidden=self.hidden(mapping, parsed),
            done=self.done,
        )
        return context


class ImportCommitView(ImportPreviewView):
    """Step 4 — write it, chunked, and report what happened row for row."""

    done = True

    def run(self, request, stashed, parsed, mapping) -> HttpResponse:
        result = importer.commit(self.panel, parsed, mapping, request, self.permissions)

        if result.errors:
            # The report outlives the upload: they have not downloaded it yet, and the
            # file itself is production data with no reason to stay on disk.
            importer.stash_errors(token=self.token, result=result)
            importer.keep_report_only(self.token)
        else:
            importer.discard_stash(self.token)

        level = messages.success if not result.failed else messages.warning
        level(request, _("Import finished: %(summary)s.") % {"summary": result.summary()})

        if not result.failed:
            return HttpResponseRedirect(self.list_url())

        # Failures stay on the page, where the error CSV is one click away. A redirect
        # would reduce "3 rows failed and here is why" to a toast.
        return self.render_to_response(
            self.get_context_data(stashed=stashed, parsed=parsed, mapping=mapping, result=result)
        )


class ImportErrorsView(ImportView):
    """The error report as CSV: row number, column, message."""

    def get(self, request, *args, **kwargs):
        try:
            body = importer.read_error_csv(
                user=request.user, model_label=self.model_label, token=self.token
            )
        except importer.StashExpired as exc:
            return self.expired(exc)

        response = HttpResponse(body, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="import-errors.csv"'
        # Same reasoning as the export: a per-user extract must not sit in a cache.
        response["Cache-Control"] = "no-store"
        return response


def build_import_urls(site) -> list:
    """The wizard's routes for every panel on ``site``.

    Must be added to the urlconf **before** the per-panel detail route: that route's
    ``<path:pk>`` converter is greedy and would otherwise swallow ``import/`` as a
    primary key, 404ing the whole feature in a way that looks like a missing view.
    """
    urlpatterns = []
    for panel in site.panels():
        opts = panel.opts
        prefix = f"{opts.app_label}/{opts.model_name}/import/"
        bound = {"site": site, "panel": panel}
        urlpatterns.extend(
            [
                path(prefix, ImportUploadView.as_view(**bound), name=f"{panel.slug}-import"),
                path(
                    f"{prefix}<str:token>/",
                    ImportMapView.as_view(**bound),
                    name=f"{panel.slug}-import-map",
                ),
                path(
                    f"{prefix}<str:token>/preview/",
                    ImportPreviewView.as_view(**bound),
                    name=f"{panel.slug}-import-preview",
                ),
                path(
                    f"{prefix}<str:token>/commit/",
                    ImportCommitView.as_view(**bound),
                    name=f"{panel.slug}-import-commit",
                ),
                path(
                    f"{prefix}<str:token>/errors.csv",
                    ImportErrorsView.as_view(**bound),
                    name=f"{panel.slug}-import-errors",
                ),
            ]
        )
    return urlpatterns
