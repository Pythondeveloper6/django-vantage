"""CSV import and filtered export over the JSON API.

Import reuses the wizard's pieces end to end: :func:`vantage.importer.read_csv` (size,
row, header and encoding limits, and the refusal of binary files), the same
``resolve_mapping`` that drops unwritable columns, the same per-mode permission check, and
the same ``dry_run`` / ``commit`` row loops. Only CSV is supported, as in the wizard.
The upload is held in memory for the one request; nothing is stashed.

A request is a dry run **unless it says otherwise** (``dry_run=false``): a script that
forgets the flag previews instead of writing.

Export builds its rows through :meth:`vantage.views.ListView.export_parts`, the method
the HTML download button and scheduled reports use, so the columns, the permission
stripping and the masking of sensitive columns are the HTML export's, not a copy.
"""

from __future__ import annotations

import json
from typing import Any

from vantage import importer, listprefs
from vantage.api import ApiError, BadRequest, ModelApiView, json_response
from vantage.export import FORMATS, stream_export
from vantage.importviews import PERMISSION_DENIED, PERMISSION_DENIED_FALLBACK

__all__ = ["ExportView", "ImportView"]

#: Row errors returned inline before the response says it was cut short. The counts are
#: always exact; only the per-row messages are capped.
MAX_RETURNED_ERRORS = 1000


class ImportView(ModelApiView):
    """``POST`` a CSV as ``multipart/form-data``."""

    def post(self, request, *args, **kwargs):
        upload = request.FILES.get("file")
        if upload is None:
            raise BadRequest("Send the CSV as a multipart part named 'file'.")
        max_bytes = importer.limit("IMPORT_MAX_BYTES", importer.MAX_BYTES)
        max_rows = importer.limit("IMPORT_MAX_ROWS", importer.MAX_ROWS)
        # Before reading: the point of a size cap is not to read the file.
        if upload.size > max_bytes:
            raise ApiError(413, f"That file is larger than the {max_bytes} byte limit.")
        try:
            parsed = importer.read_csv(upload.read(), max_bytes=max_bytes, max_rows=max_rows)
        except importer.ImportRejected as exc:
            raise ApiError(422, str(exc)) from exc

        post = request.POST
        mode = post.get("mode", "create")
        options = importer.importable_fields(self.panel, request, self.permissions)
        chosen = self._mapping(post.get("mapping"), parsed, options)
        try:
            mapping = importer.resolve_mapping(
                self.panel,
                parsed,
                chosen,
                key_field=post.get("key_field", ""),
                mode=mode,
                request=request,
                permissions=self.permissions,
            )
        except importer.ImportRejected as exc:
            raise ApiError(422, str(exc)) from exc
        self._check_mode(request, mapping.mode)

        dry = (post.get("dry_run") or "true").strip().lower() not in ("false", "0", "no", "off")
        runner = importer.dry_run if dry else importer.commit
        result = runner(self.panel, parsed, mapping, request, self.permissions)
        return json_response(self._payload(result, mapping, parsed))

    @staticmethod
    def _mapping(raw: str | None, parsed, options) -> dict[str, str]:
        """Header to field name: as sent, or inferred the way the wizard infers it."""
        if not raw:
            return importer.infer_mapping(parsed.headers, options)
        try:
            chosen = json.loads(raw)
        except ValueError as exc:
            raise BadRequest("'mapping' must be a JSON object of header to field.") from exc
        if not isinstance(chosen, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in chosen.items()
        ):
            raise BadRequest("'mapping' must be a JSON object of header to field.")
        return chosen

    def _check_mode(self, request, mode: str) -> None:
        """Re-check the verbs this mode needs, as the wizard's ``check_mode`` does."""
        allowed = self.permissions.allowed_actions(request, self.panel.model)
        missing = [verb for verb in importer.MODES[mode] if verb not in allowed]
        if missing:
            sentence = PERMISSION_DENIED.get(missing[0], PERMISSION_DENIED_FALLBACK)
            raise ApiError(403, str(sentence) % {"objects": self.panel.get_label_plural().lower()})

    @staticmethod
    def _payload(result, mapping, parsed) -> dict[str, Any]:
        errors = result.errors[:MAX_RETURNED_ERRORS]
        return {
            "dry_run": result.dry_run,
            "summary": str(result.summary()),
            "rows": parsed.row_count,
            "created": result.created,
            "updated": result.updated,
            "unchanged": result.unchanged,
            "failed": result.failed,
            "queued_for_approval": result.queued,
            "errors": [{"row": e.row, "column": e.column, "message": e.message} for e in errors],
            "errors_truncated": len(result.errors) > len(errors),
            "diffs": [
                {
                    "row": d.row,
                    "pk": str(d.pk) if d.pk is not None else None,
                    "label": d.label,
                    "changes": [
                        {"field": c.label, "before": c.before, "after": c.after} for c in d.changes
                    ],
                }
                for d in result.diffs
            ],
            "mapping": {
                "mode": mapping.mode,
                "key_field": mapping.key_field,
                "columns": dict(mapping.columns),
                "ignored": [{"column": h, "reason": str(r)} for h, r in mapping.ignored],
                "missing_required": [
                    {"field": n, "label": str(label)} for n, label in mapping.missing_required
                ],
            },
        }


class ExportView(ModelApiView):
    """``GET`` the filtered list as ``csv``, ``tsv`` or ``json`` (newline-delimited)."""

    def get(self, request, *args, **kwargs):
        fmt = request.GET.get("format", "csv")
        if fmt not in FORMATS:
            raise BadRequest(f"'format' must be one of: {', '.join(FORMATS)}.")
        from vantage.views import ListView

        listprefs.resolve(self.panel, request)
        view = ListView(site=self.site, panel=self.panel)
        view.request = request
        queryset, columns, headers, masked = view.export_parts(request)
        return stream_export(
            fmt,
            queryset,
            columns,
            headers,
            self.panel.get_label_plural(),
            masked=masked,
        )
