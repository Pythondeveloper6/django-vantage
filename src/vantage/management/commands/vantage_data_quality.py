"""``manage.py vantage_data_quality`` - the data quality report, for a terminal or CI."""

from __future__ import annotations

import json
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from vantage.dataquality import SEVERITIES, config, run_audit


class Command(BaseCommand):
    help = (
        "Print the same data quality report as the panel's Data quality page. "
        "Use --fail-on to exit non-zero when it finds something, e.g. in CI."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "models",
            nargs="*",
            help="Limit to these models, as app_label.ModelName. Default: all.",
        )
        parser.add_argument(
            "--fail-on",
            choices=["warn", "fail"],
            help="Exit with status 1 if any finding is at least this severe.",
        )
        parser.add_argument("--format", choices=["text", "json"], default="text")
        parser.add_argument(
            "--no-cache",
            action="store_true",
            help="Ignore cached results and re-run. The fresh results are cached, so the "
            "panel page shows them.",
        )
        parser.add_argument(
            "--user",
            help="Audit as this username (default: the first active superuser). The "
            "panel's permissions and tenant scoping apply to that user.",
        )

    def handle(self, models, fail_on, format, no_cache, user, **options):
        if not config()["ENABLED"]:
            raise CommandError("Data quality is disabled (VANTAGE['DATA_QUALITY']['ENABLED']).")

        from vantage.jobs import JobRequest
        from vantage.sites import site

        actor = self._actor(user)
        labels = [m.lower() for m in models] or None
        audit = run_audit(
            site, JobRequest(user=actor), labels=labels, force=no_cache, page_budget=None
        )
        if labels:
            known = {r.model for r in audit.reports}
            missing = [m for m in labels if m not in known]
            if missing:
                raise CommandError(f"Not an auditable registered model: {', '.join(missing)}.")

        if format == "json":
            self.stdout.write(json.dumps(self._as_json(audit), indent=2))
        else:
            self._print(audit)

        if fail_on:
            threshold = SEVERITIES.index(fail_on)
            if any(
                SEVERITIES.index(f.severity) >= threshold
                for report in audit.reports
                for f in report.findings
            ):
                sys.exit(1)

    def _actor(self, username):
        User = get_user_model()
        users = User.objects.filter(is_active=True)
        if username:
            found = users.filter(**{User.USERNAME_FIELD: username}).first()
            if found is None:
                raise CommandError(f"No active user {username!r}.")
            return found
        found = users.filter(is_superuser=True).order_by("pk").first()
        if found is None:
            raise CommandError("No active superuser to audit as. Pass --user.")
        return found

    def _print(self, audit):
        for report in audit.reports:
            score = "n/a" if report.score is None else str(report.score)
            note = " (skipped)" if report.skipped else " (sample)" if report.sampled else ""
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"{report.model}  rows={report.rows_display}{note}  score={score}"
                )
            )
            if report.error:
                self.stdout.write(f"  error: {report.error}")
            for f in report.findings:
                where = f" {f.field}:" if f.field else ""
                self.stdout.write(f"  [{f.severity}] {f.check}{where} {f.message}")
            for run in report.checks:
                if run.status != "ok":
                    self.stdout.write(f"  [{run.status}] {run.name}: {run.reason}")
        total = sum(len(r.issues) for r in audit.reports)
        self.stdout.write(f"{len(audit.reports)} models, {total} issues.")

    def _as_json(self, audit):
        return [
            {
                "model": r.model,
                "rows": r.rows,
                "rows_kind": r.rows_kind,
                "sampled": r.sampled,
                "skipped": r.skipped,
                "score": r.score,
                "findings": [
                    {
                        "check": f.check,
                        "severity": f.severity,
                        "field": f.field,
                        "message": f.message,
                        "count": f.count,
                        "scanned": f.scanned,
                        "url": f.url,
                    }
                    for f in r.findings
                ],
                "checks": [
                    {"name": c.name, "status": c.status, "reason": c.reason} for c in r.checks
                ],
            }
            for r in audit.reports
        ]
