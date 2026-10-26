"""Two hand-written panels, and five models left entirely alone.

The comparison is the point of this file. ``Order`` and ``Product`` below say exactly
what they want; ``Customer``, ``Category``, ``Tag``, ``OrderItem`` and ``Review`` say
nothing at all and are auto-registered with inferred columns, filters and search. Open
both kinds in the sidebar and judge whether the configured ones were worth the typing.

Note that ``ProductPanel`` sets ``list_display`` and nothing else: its filters and
search are still inferred. Overriding one attribute never costs you the others.

The list views follow the same rule. Every model here gets a view switcher inferred from
its fields — orders become a Kanban board by ``status`` and a calendar by when they
were placed, products a gallery from their ``image``, customers a map from their
latitude/longitude pair — and each panel below changes one thing about that, in one line.
"""

from django.db.models import Avg, Count, Q, Sum

from shop.models import Order, Product, Review
from vantage import Chart, ModelPanel, Stat, action, register


@register(Order)
class OrderPanel(ModelPanel):
    label = "Order"
    label_plural = "Orders"
    icon = "shopping-cart"
    group = "Commerce"
    order = -10
    description = "Every order placed in the last six months."

    list_display = ["number", "customer", "status", "total", "shipping_city", "created_at"]
    list_filter = ["status", "created_at", "customer"]
    list_search = ["number", "customer__name", "customer__email"]
    list_per_page = 25

    #: Declared in Python and computed against whatever the page is currently
    #: filtered to. Narrow the list with the status filter or the search box and
    #: every number below follows.
    list_stats = [
        Stat("Revenue", Sum("total"), format="currency", trend="created_at"),
        Stat("Orders", Count("id")),
        Stat("Average order", Avg("total"), format="currency"),
        Stat("Refunded", Count("id", filter=Q(status="refunded")), tone="destructive"),
    ]

    list_charts = [
        Chart.timeseries("Revenue over time", date="created_at", value=Sum("total")),
        Chart.breakdown("By status", group_by="status", value=Count("id"), kind="donut"),
    ]

    #: The board and the calendar are inferred; this only chooses what a card shows.
    card_fields = ["customer", "total", "shipping_city"]


@register(Product)
class ProductPanel(ModelPanel):
    label_plural = "Catalogue"
    icon = "box"
    group = "Commerce"

    #: Deliberately drops ``cost`` and reorders the rest — margin is a back-office
    #: number, not something to scan a catalogue by.
    list_display = ["name", "sku", "category", "status", "price", "stock"]

    #: A catalogue is browsed by eye, so it opens as the gallery. The table is still
    #: one click away, and a person's own choice is remembered per model.
    list_views = ["gallery", "table", "kanban"]


@register(Review)
class ReviewPanel(ModelPanel):
    """Shows the action API: three bulk operations declared as ordinary methods."""

    icon = "message-square"
    group = "People"
    label_plural = "Feedback"

    list_display = ["headline", "product", "customer", "rating", "is_published", "created_at"]
    list_filter = ["is_published", "rating", "created_at"]

    @action("Publish", icon="check", permission="change")
    def publish(self, request, queryset):
        return f"Published {queryset.update(is_published=True)} reviews."

    @action("Unpublish", icon="x", permission="change")
    def unpublish(self, request, queryset):
        return f"Unpublished {queryset.update(is_published=False)} reviews."

    @action(
        "Delete",
        icon="trash",
        permission="delete",
        destructive=True,
        max_rows=500,
        confirm="Permanently delete {count} reviews? This cannot be undone.",
    )
    def delete_selected(self, request, queryset):
        deleted, _ = queryset.delete()
        return f"Deleted {deleted} records."
