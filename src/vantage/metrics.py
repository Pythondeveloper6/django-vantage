"""Operational metrics in the Prometheus text format, and slow-request logging.

Two halves, for the two questions an operator asks:

* **Is the back office's machinery healthy?** :func:`collect` reads gauges from the
  database at scrape time — background jobs, webhook deliveries, scheduled report runs
  and approval requests by status, plus undo entries held. These are true across every
  worker because they come from shared tables, and a scrape costs a handful of
  ``GROUP BY`` queries on small tables.
* **Is the panel itself slow?** :class:`SlowRequestMiddleware` times every request into
  the panel, counts them per process, and logs the slow ones to the ``vantage.requests``
  logger with structured ``extra`` fields (``vantage_path``, ``vantage_view``,
  ``vantage_ms``, ``vantage_status``), which a JSON log formatter turns into searchable
  fields. The per-process counters are exported too
  and labelled as such; aggregate them with ``sum()`` in PromQL.

The endpoint is off until ``VANTAGE["METRICS"]`` is ``True``. A scraper authenticates
with ``Authorization: Bearer <VANTAGE["METRICS_TOKEN"]>``; a signed-in superuser may
open it in a browser. Nothing in it names a record, a user or a value.

No dependency on ``prometheus_client``: the format is a few lines of text, and the
package keeps Django as its only runtime requirement.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections.abc import Iterable

from django.http import Http404, HttpResponse
from django.views import View

__all__ = ["MetricsView", "SlowRequestMiddleware", "collect", "render"]

logger = logging.getLogger("vantage.requests")

#: (name, help, type, [(labels, value), ...])
Sample = tuple[str, str, str, list[tuple[dict[str, str], float]]]

_lock = threading.Lock()
_requests: dict[tuple[str, str], int] = {}
_slow: dict[str, int] = {}
_seconds: dict[str, float] = {}


def _status_counts(model, field: str = "status") -> list[tuple[dict[str, str], float]]:
    from django.db.models import Count

    rows = model.objects.values(field).annotate(n=Count("pk")).order_by()
    found = {row[field]: row["n"] for row in rows}
    statuses = [value for value, _label in model.Status.choices]
    return [({"status": status}, float(found.get(status, 0))) for status in statuses]


def collect() -> list[Sample]:
    """Every metric, gauges from the database first, then this process's counters."""
    from vantage import __version__
    from vantage.models import (
        ChangeRequest,
        ReportRun,
        UndoEntry,
        VantageJob,
        WebhookDelivery,
    )

    samples: list[Sample] = [
        (
            "vantage_info",
            "Installed django-vantage version.",
            "gauge",
            [({"version": __version__}, 1.0)],
        ),
    ]
    sources = [
        ("vantage_jobs", "Background jobs by status.", VantageJob),
        ("vantage_webhook_deliveries", "Webhook deliveries by status.", WebhookDelivery),
        ("vantage_report_runs", "Scheduled report runs by status.", ReportRun),
        ("vantage_change_requests", "Approval requests by status.", ChangeRequest),
    ]
    for name, help_text, model in sources:
        try:
            samples.append((name, help_text, "gauge", _status_counts(model)))
        except Exception:  # A missing table must not take the scrape down.
            logger.exception("Could not collect %s.", name)
    try:
        samples.append(
            (
                "vantage_undo_entries",
                "Undo entries held.",
                "gauge",
                [({}, float(UndoEntry.objects.count()))],
            )
        )
    except Exception:
        logger.exception("Could not collect vantage_undo_entries.")

    with _lock:
        requests = dict(_requests)
        slow = dict(_slow)
        seconds = dict(_seconds)
    samples.extend(
        [
            (
                "vantage_process_requests_total",
                "Panel requests served by this process, by view and status class.",
                "counter",
                [
                    ({"view": view, "status": status}, float(n))
                    for (view, status), n in sorted(requests.items())
                ],
            ),
            (
                "vantage_process_request_seconds_total",
                "Time this process spent serving panel requests, by view.",
                "counter",
                [({"view": view}, total) for view, total in sorted(seconds.items())],
            ),
            (
                "vantage_process_slow_requests_total",
                "Panel requests over VANTAGE['SLOW_REQUEST_MS'] served by this process.",
                "counter",
                [({"view": view}, float(n)) for view, n in sorted(slow.items())],
            ),
        ]
    )
    return samples


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render(samples: Iterable[Sample]) -> str:
    """The Prometheus text exposition format (version 0.0.4)."""
    lines: list[str] = []
    for name, help_text, kind, values in samples:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in values:
            label_text = ",".join(f'{key}="{_escape(str(val))}"' for key, val in labels.items())
            suffix = f"{{{label_text}}}" if label_text else ""
            lines.append(f"{name}{suffix} {value:g}")
    return "\n".join(lines) + "\n"


def _authorised(request) -> bool:
    from vantage.conf import vantage_settings

    token = vantage_settings.METRICS_TOKEN
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if token and header.startswith("Bearer "):
        # Constant time: the token is the only thing between the internet and this.
        return hmac.compare_digest(header[len("Bearer ") :].encode(), str(token).encode())
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_superuser)


class MetricsView(View):
    """``GET _/metrics/``. 404 while ``METRICS`` is off, so its existence is not news."""

    site = None
    http_method_names = ["get", "head"]

    def get(self, request, *args, **kwargs):
        from vantage.conf import vantage_settings

        if not vantage_settings.METRICS:
            raise Http404
        if not _authorised(request):
            response = HttpResponse("Unauthorized\n", status=401, content_type="text/plain")
            response["WWW-Authenticate"] = 'Bearer realm="vantage-metrics"'
            return response
        response = HttpResponse(
            render(collect()), content_type="text/plain; version=0.0.4; charset=utf-8"
        )
        response["Cache-Control"] = "no-store"
        return response


class SlowRequestMiddleware:
    """Time requests into the panel; count them, and log the slow ones.

    Only requests resolved into the ``vantage`` URL namespace are touched, so the rest
    of the project is neither timed nor logged. Add it anywhere in ``MIDDLEWARE``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        if match is None or "vantage" not in (match.namespaces or ()):
            return response
        elapsed = time.perf_counter() - started
        view = match.url_name or "unknown"
        # A list page's url_name carries the model ("shop-order-list"); collapsing it to
        # the suffix keeps the label set bounded however many models are registered.
        view = view.rsplit("-", 1)[-1] if "-" in view else view
        status_class = f"{response.status_code // 100}xx"
        from vantage.conf import vantage_settings

        threshold = vantage_settings.SLOW_REQUEST_MS
        is_slow = bool(threshold) and elapsed * 1000 >= threshold
        with _lock:
            _requests[(view, status_class)] = _requests.get((view, status_class), 0) + 1
            _seconds[view] = _seconds.get(view, 0.0) + elapsed
            if is_slow:
                _slow[view] = _slow.get(view, 0) + 1
        if is_slow:
            logger.warning(
                "Slow panel request: %s %s took %.0f ms",
                request.method,
                request.path,
                elapsed * 1000,
                extra={
                    "vantage_path": request.path,
                    "vantage_view": match.url_name,
                    "vantage_ms": round(elapsed * 1000, 1),
                    "vantage_status": response.status_code,
                },
            )
        return response


def _reset_counters() -> None:
    """For tests."""
    with _lock:
        _requests.clear()
        _slow.clear()
        _seconds.clear()
