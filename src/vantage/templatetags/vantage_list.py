"""Template helpers the list page needs and the query string cannot express.

Two jobs live here, and both are here for the same reason: the Django template
language can express neither arithmetic nor removal of *one* value from a repeated
query parameter, and both are load-bearing on this page.

*Chart geometry.* Charts are inline SVG computed on the server, not a JavaScript
charting library. A list page that already knows its numbers should not ship 60 kB of
runtime to draw four of them, and an admin panel behind a strict CSP frequently cannot
run one anyway. The tag computes coordinates only; every stroke, fill and colour is
declared in ``partials/chart_svg.html`` so the markup stays with the other markup.

*Filter URLs.* ``?status=draft&status=review`` means OR, so removing "draft" is not
"replace the parameter" — it is "drop one of its values", which
:func:`~vantage.templatetags.vantage.querystring_replace` deliberately cannot do.
Both helpers also drop ``page``: changing what is being filtered invalidates which
page you were on, and landing on an out-of-range page 7 is the classic version of that
bug.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from django import template
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

register = template.Library()

#: Viewport of a line or bar chart, in user units. The SVG is scaled by ``viewBox`` to
#: whatever width the card gives it, so these are proportions, not pixels.
CHART_WIDTH = 320
CHART_HEIGHT = 96

#: Breathing room above the peak and below the floor, so a maximum point is not
#: clipped by the edge of the viewport and a stroke is never half-drawn.
CHART_PAD = 6

#: A donut drawn as one dashed circle rather than as arc paths. Choosing a radius whose
#: circumference is exactly 100 makes every dash length a percentage, which is both
#: exact and readable in the rendered markup.
DONUT_BOX = 42
DONUT_RADIUS = 100 / (2 * math.pi)
DONUT_STROKE = 5

#: Segments are separated by lightness rather than by hue: a sequential ramp of the one
#: brand colour survives both themes and a recolour, where a hard-coded categorical
#: palette survives neither.
SEGMENT_MIN_OPACITY = 0.3
SEGMENT_STEP = 0.11

#: How many segments an ``aria-label`` names before it summarises the rest. A screen
#: reader announcing forty segments is not more accessible than one announcing five.
SPOKEN_SEGMENTS = 5

#: The kind of chart, as a whole phrase per kind, lazily translated because this is
#: evaluated at import. Never ``str.lower()`` of a translated word: lowercasing is not
#: a safe transform — a German noun loses its capital and a Turkish "I" loses its dot —
#: and the phrase a translator writes for the middle of a sentence is frequently not
#: the one they would write at the start of it.
CHART_SHAPES: dict[str, Any] = {
    "bar": _("bar chart"),
    "donut": _("donut chart"),
    "line": _("line chart"),
}


# ── charts ──────────────────────────────────────────────────────────────────


def _fmt(value: float) -> str:
    """A coordinate short enough to read, without a trailing ``.0`` on every one."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _values(chart: Any) -> list[float]:
    """The first series as plain floats, with anything non-numeric read as zero."""
    series = _get(chart, "series") or []
    if not series:
        return []
    data = _get(series[0], "data") or []
    numbers = []
    for value in data:
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            numbers.append(0.0)
        else:
            numbers.append(float(value))
    return numbers


def _get(source: Any, name: str, default: Any = None) -> Any:
    """Read a field from either the dataclass or the ``as_dict()`` form of a result."""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _line_geometry(values: list[float]) -> dict[str, Any]:
    """A polyline and the area beneath it, scaled to the extent of the data.

    The floor is pinned at zero whenever the data is non-negative, which is the usual
    case for a count or a sum: a chart auto-scaled to its own minimum turns a rise from
    1000 to 1010 into a cliff.
    """
    count = len(values)
    high = max([*values, 0.0])
    low = min([*values, 0.0])
    if high == low:
        high = low + 1
    span = high - low
    inner = CHART_HEIGHT - 2 * CHART_PAD

    def y_of(value: float) -> float:
        return CHART_PAD + (high - value) / span * inner

    if count == 1:
        points = [(0.0, y_of(values[0])), (float(CHART_WIDTH), y_of(values[0]))]
    else:
        step = CHART_WIDTH / (count - 1)
        points = [(index * step, y_of(value)) for index, value in enumerate(values)]

    polyline = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in points)
    edge = " L ".join(f"{_fmt(x)} {_fmt(y)}" for x, y in points)
    area = (
        f"M {_fmt(points[0][0])} {CHART_HEIGHT} L {edge} "
        f"L {_fmt(points[-1][0])} {CHART_HEIGHT} Z"
    )
    return {
        "polyline": polyline,
        "area": area,
        "last": {"x": _fmt(points[-1][0]), "y": _fmt(points[-1][1])},
        "high": high,
        "low": low,
    }


def _bar_geometry(values: list[float], labels: list[str]) -> list[dict[str, str]]:
    """One rectangle per value, sharing an equal slot so the axis stays honest."""
    count = len(values)
    if not count:
        return []
    high = max([*values, 0.0]) or 1.0
    inner = CHART_HEIGHT - 2 * CHART_PAD
    slot = CHART_WIDTH / count
    width = slot * 0.62

    bars = []
    for index, value in enumerate(values):
        height = max(value, 0.0) / high * inner
        # A zero bar still gets a hairline: an empty slot reads as missing data, which
        # is a different statement from "this category is zero".
        height = max(height, 1.0)
        bars.append(
            {
                "x": _fmt(index * slot + (slot - width) / 2),
                "y": _fmt(CHART_HEIGHT - height),
                "width": _fmt(width),
                "height": _fmt(height),
                "label": labels[index] if index < len(labels) else "",
                "value": value,
            }
        )
    return bars


def _donut_geometry(values: list[float], labels: list[str]) -> dict[str, Any]:
    """Segments as dash lengths on one circle, in percent of the whole.

    Negative values are dropped rather than drawn: a part cannot be a negative share of
    a whole, and rendering one would silently overlap its neighbours.
    """
    positive = [max(value, 0.0) for value in values]
    total = sum(positive)
    if total <= 0:
        return {"segments": [], "total": 0.0}

    segments = []
    cumulative = 0.0
    for index, value in enumerate(positive):
        percent = value / total * 100
        segments.append(
            {
                "dash": _fmt(percent),
                "gap": _fmt(100 - percent),
                # 25 places the first segment at twelve o'clock rather than at three.
                "offset": _fmt(25 - cumulative),
                "opacity": _fmt(max(1 - SEGMENT_STEP * index, SEGMENT_MIN_OPACITY)),
                "label": labels[index] if index < len(labels) else "",
                "value": values[index],
                "percent": round(percent, 1),
            }
        )
        cumulative += percent
    return {"segments": segments, "total": total}


def _number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _spoken_period(bucket: str, count: int) -> str:
    """``"3 days"`` — a count and its unit, as one translated phrase.

    One message per bucket, pluralised on the count, because "day" and "days" differ by
    more than a letter in most languages and by more than two forms in several.
    """
    if bucket == "week":
        return ngettext("%(count)s week", "%(count)s weeks", count) % {"count": count}
    if bucket == "month":
        return ngettext("%(count)s month", "%(count)s months", count) % {"count": count}
    if bucket == "day":
        return ngettext("%(count)s day", "%(count)s days", count) % {"count": count}
    # Unreachable through ``Chart``, which validates the bucket, but a chart arriving as
    # a plain dict can say anything and a screen reader must still get a whole word.
    return ngettext("%(count)s point", "%(count)s points", count) % {"count": count}


def _aria_label(chart: Any, values: list[float], labels: list[str], kind: str) -> str:
    """A sentence that answers the same question the picture does.

    ``role="img"`` replaces the graphic entirely for a screen reader, so this string is
    the chart for that user. It names the shape, the range and the extremes rather than
    reciting every point.

    Every branch returns one whole sentence with named placeholders. Assembling this
    text from clauses would be worse here than anywhere else in the package: the reader
    has no picture to fall back on when the word order comes out wrong, and the
    placeholders arrive in an order no translator can change from Python.
    """
    label = _get(chart, "label", "") or _("Chart")
    if not values:
        return _("%(label)s: no data.") % {"label": label}

    shape = CHART_SHAPES.get(kind, CHART_SHAPES["line"])
    bucket = _get(chart, "bucket")
    if bucket:
        parts = {
            "label": label,
            "shape": shape,
            "period": _spoken_period(str(bucket), len(values)),
            "low": _number(min(values)),
            "high": _number(max(values)),
        }
        if labels:
            return _(
                "%(label)s: %(shape)s of %(period)s, %(first)s to %(last)s, "
                "from %(low)s to %(high)s."
            ) % {**parts, "first": labels[0], "last": labels[-1]}
        return _("%(label)s: %(shape)s of %(period)s, from %(low)s to %(high)s.") % parts

    named = []
    for index, value in enumerate(values[:SPOKEN_SEGMENTS]):
        # A label and a number side by side: data, not prose, so nothing to translate
        # beyond the stand-in used when a series arrived without labels.
        name = labels[index] if index < len(labels) else _("item %(number)s") % {
            "number": index + 1
        }
        named.append(f"{name} {_number(value)}")

    parts = {"label": label, "shape": shape, "segments": "; ".join(named)}
    rest = len(values) - len(named)
    if rest > 0:
        # English needs no plural here; Polish and Russian need three forms of it.
        return ngettext(
            "%(label)s: %(shape)s, %(segments)s, and %(count)s more.",
            "%(label)s: %(shape)s, %(segments)s, and %(count)s more.",
            rest,
        ) % {**parts, "count": rest}
    return _("%(label)s: %(shape)s, %(segments)s.") % parts


@register.inclusion_tag("vantage/partials/chart_svg.html")
def chart_svg(chart: Any) -> dict[str, Any]:
    """``{% chart_svg chart %}`` — one ``ChartResult`` as an inline, themed SVG."""
    kind = _get(chart, "kind", "line") or "line"
    labels = list(_get(chart, "labels") or [])
    values = _values(chart)

    context: dict[str, Any] = {
        "kind": kind,
        "key": _get(chart, "key", ""),
        "label": _get(chart, "label", ""),
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "aria_label": _aria_label(chart, values, labels, kind),
        "line": None,
        "bars": [],
        "donut": None,
    }
    if not values:
        return context

    if kind == "donut":
        context["donut"] = _donut_geometry(values, labels)
        context["donut_box"] = DONUT_BOX
        context["donut_radius"] = _fmt(DONUT_RADIUS)
        context["donut_centre"] = _fmt(DONUT_BOX / 2)
        context["donut_stroke"] = DONUT_STROKE
        context["donut_total"] = _number(context["donut"]["total"])
    elif kind == "bar":
        context["bars"] = _bar_geometry(values, labels)
    else:
        context["line"] = _line_geometry(values)
    return context


# ── filter URLs ─────────────────────────────────────────────────────────────


def _params(context: Any):
    request = context.get("request")
    return request.GET.copy() if request is not None else None


@register.simple_tag(takes_context=True, name="filter_remove_url")
def filter_remove_url(context, filt: Any, value: Any = None) -> str:
    """The query string with one filter value — or the whole filter — removed.

    Passing no ``value`` clears every parameter the filter owns, which is what a
    two-parameter filter such as a numeric range needs; passing one drops that single
    value and leaves the filter's other selections alone.
    """
    params = _params(context)
    if params is None:
        return ""
    if value is None:
        for name in filt.expected_parameters():
            params.pop(name, None)
    else:
        name = filt.parameter_name
        kept = [item for item in params.getlist(name) if item != str(value)]
        params.setlist(name, kept)
    params.pop("page", None)
    return params.urlencode()


@register.simple_tag(takes_context=True, name="facets_on")
def facets_on(context) -> bool:
    """Whether this request asked for counts.

    A template cannot read ``request.GET._facets`` — the engine refuses variables that
    start with an underscore — so the check the view already makes is exposed as a tag
    rather than duplicated as a string comparison.
    """
    from vantage.filters import facets_requested

    return facets_requested(context.get("request"))


@register.simple_tag(takes_context=True, name="filters_clear_url")
def filters_clear_url(context, filters: Any) -> str:
    """The query string with every filter cleared, keeping search and sort intact."""
    params = _params(context)
    if params is None:
        return ""
    for filt in filters:
        for name in filt.expected_parameters():
            params.pop(name, None)
    params.pop("page", None)
    return params.urlencode()
