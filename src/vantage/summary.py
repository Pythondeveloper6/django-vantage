"""The bottom summary bar: one aggregate per numeric column.

Of the three places a product can put list-page analytics, this is the cheapest and
the highest value per pixel. Filament puts stat cards above the table and spends
100-140px of vertical space on every page load, before the user reaches the thing they
came for. Airtable puts a single 32px strip at the bottom with a per-column aggregate,
and it costs almost nothing. This is the Airtable one, and unlike ``list_stats`` it is
on by default, because it requires no configuration to be useful: a column of money
wants summing whether or not anyone said so.

Everything folds into one ``aggregate()`` call over the filtered queryset, so the whole
bar costs a single query however many columns it covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import Avg, Count, DecimalField, FloatField, IntegerField, Max, Min, Sum

from vantage.introspection import resolve_path

__all__ = ["SUMMARY_FUNCTIONS", "SummaryCell", "compute_summary", "infer_summaries"]

#: What a user may ask for from the column menu.
SUMMARY_FUNCTIONS: dict[str, Any] = {
    "sum": Sum,
    "avg": Avg,
    "min": Min,
    "max": Max,
    "count": Count,
}

NUMERIC = (IntegerField, DecimalField, FloatField)

#: Field names that are identifiers stored as numbers. Summing an order number
#: produces a large meaningless integer, which erodes trust in the whole row.
NOT_SUMMABLE = ("id", "pk", "number", "code", "year", "zip", "postcode", "phone")

#: Names that read as a rate or a score, where an average is the only sensible answer.
AVERAGED = ("rating", "score", "percent", "percentage", "rate", "ratio", "average", "avg")


@dataclass(frozen=True)
class SummaryCell:
    """One column's footer value."""

    column: str
    function: str
    value: Any
    display: str


def _default_function(field, name: str) -> str | None:
    """Which aggregate a column gets when nobody said.

    Conservative on purpose. A wrong number in a footer is worse than a blank one,
    because a reader has no way to tell it is wrong.
    """
    lowered = name.lower()
    if any(part == lowered or lowered.endswith(f"_{part}") for part in NOT_SUMMABLE):
        return None
    if any(part in lowered for part in AVERAGED):
        return "avg"
    if isinstance(field, NUMERIC):
        return "sum"
    return None


def infer_summaries(panel, columns: list[str]) -> dict[str, str]:
    """Choose a function per column, honouring ``list_aggregates`` where given.

    ``list_aggregates`` may name a function (``{"total": "avg"}``) or pass the
    aggregate class itself, so ``{"total": Sum}`` reads naturally next to the
    ``list_stats`` declarations right above it.
    """
    from vantage.sensitive import is_sensitive_path

    declared = panel.list_aggregates or {}
    chosen: dict[str, str] = {}

    for name in columns:
        if is_sensitive_path(panel, name):
            # A min, a max or an average of a masked column is a value from it, or a
            # bound on one. Not even ``list_aggregates`` can ask for that.
            continue
        if name in declared:
            wanted = declared[name]
            if wanted is None:
                continue
            key = wanted if isinstance(wanted, str) else getattr(wanted, "name", "").lower()
            if key in SUMMARY_FUNCTIONS:
                chosen[name] = key
            continue

        hops = resolve_path(panel.model, name)
        # Only a local concrete column. Aggregating across a join silently multiplies
        # values through the fan-out, and a footer is the last place to discover that.
        if len(hops) != 1 or hops[0].is_relation:
            continue
        function = _default_function(hops[0].field, name)
        if function:
            chosen[name] = function

    return chosen


def _format(value: Any, function: str) -> str:
    if value is None:
        return "—"
    if function == "count":
        return f"{int(value):,}"
    number = float(value)
    if number == int(number) and abs(number) < 1e15:
        return f"{int(number):,}"
    return f"{number:,.2f}"


def compute_summary(queryset, summaries: dict[str, str]) -> list[SummaryCell]:
    """One query for the whole bar, whatever the column count."""
    if not summaries:
        return []

    # Positional aliases: a column name could otherwise collide with a model field
    # and Django would reject the annotation.
    expressions = {
        f"s{index}": SUMMARY_FUNCTIONS[function](column)
        for index, (column, function) in enumerate(summaries.items())
    }
    values = queryset.aggregate(**expressions)

    return [
        SummaryCell(
            column=column,
            function=function,
            value=values[f"s{index}"],
            display=_format(values[f"s{index}"], function),
        )
        for index, (column, function) in enumerate(summaries.items())
    ]
