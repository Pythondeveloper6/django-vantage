"""The demo's dashboard, declared in Python and pointed at from ``VANTAGE["DASHBOARD"]``.

Every widget type appears exactly once, so the file doubles as the reference for the
API. The comparison worth making is with the default: delete the ``DASHBOARD`` line in
settings and the panel still opens on a real dashboard, generated from the registry.
"""

from django.db.models import Avg, Count, Q, Sum

from shop.models import Customer, Order, Product, Review
from vantage import Chart
from vantage.dashboards import (
    ChartCard,
    Dashboard,
    ListCard,
    StatCard,
    TableCard,
    TemplateCard,
)


class ShopDashboard(Dashboard):
    title = "Overview"
    description = "Six months of trading, computed on request."

    def get_widgets(self, request):
        return [
            # A queryset built here rather than at module scope is not a requirement —
            # widgets never evaluate what they hold until they are rendered — but it is
            # what lets a card depend on the request.
            StatCard(
                "Revenue",
                Order.objects.exclude(status=Order.Status.REFUNDED),
                aggregate=Sum("total"),
                format="currency",
                trend="created_at",
            ),
            StatCard("Orders", Order.objects.all(), trend="created_at"),
            StatCard(
                "Average order",
                Order.objects.all(),
                aggregate=Avg("total"),
                format="currency",
            ),
            StatCard(
                "Refunded",
                Order.objects.filter(status=Order.Status.REFUNDED),
                tone="destructive",
            ),
            ChartCard(
                "Revenue over time",
                Order.objects.all(),
                chart=Chart.timeseries("Revenue", date="created_at", value=Sum("total")),
            ),
            ChartCard(
                "Orders by status",
                Order.objects.all(),
                chart=Chart.breakdown(
                    "Orders", group_by="status", value=Count("id"), kind="donut"
                ),
            ),
            TableCard("Latest orders", Order.objects.all(), limit=5),
            TableCard(
                "Newest reviews",
                Review.objects.select_related("product"),
                columns=["headline", "product", "rating"],
                limit=5,
            ),
            # Anything the ORM cannot say in one expression, or three numbers that do
            # not each deserve a card of their own.
            ListCard(
                "Catalogue",
                lambda request: [
                    ("Products", Product.objects.count()),
                    {
                        "label": "Out of stock",
                        "value": Product.objects.filter(stock=0).count(),
                        "tone": "warning",
                    },
                    {
                        "label": "Customers",
                        "value": Customer.objects.filter(is_active=True).count(),
                        "url": "/admin/shop/customer/",
                    },
                ],
                width=2,
            ),
            # The escape hatch: your template, your markup. It borrows a Vantage partial
            # here only because this demo project ships no templates of its own.
            TemplateCard(
                "Five-star reviews",
                "vantage/partials/stat_card.html",
                context=lambda request: {
                    "stat": {
                        "label": "Five-star reviews",
                        "display": str(
                            Review.objects.filter(
                                Q(rating=5) & Q(is_published=True)
                            ).count()
                        ),
                        "tone": "success",
                        "description": "Published, rated five.",
                    }
                },
                width=2,
            ),
        ]
