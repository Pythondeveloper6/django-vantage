from django.db.models import Avg, Count, Sum

from shop.models import Order, Product
from vantage import Chart
from vantage.dashboards import ChartCard, Dashboard, ListCard, StatCard, TableCard


class ShopDashboard(Dashboard):
    title = "Overview"
    description = "The last four months of trading."

    def get_widgets(self, request):
        return [
            StatCard(
                "Revenue",
                Order.objects.exclude(status="refunded"),
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
            StatCard("Refunded", Order.objects.filter(status="refunded"), tone="destructive"),
            ChartCard(
                "Revenue over time",
                Order.objects.all(),
                chart=Chart.timeseries("Revenue", date="created_at", value=Sum("total")),
            ),
            ChartCard(
                "Orders by status",
                Order.objects.all(),
                chart=Chart.breakdown("Orders", group_by="status", value=Count("id"), kind="donut"),
            ),
            TableCard("Latest orders", Order.objects.all(), limit=5),
            ListCard(
                "Stock watch",
                lambda request: [
                    ("Products", Product.objects.count()),
                    {
                        "label": "Out of stock",
                        "value": Product.objects.filter(stock=0).count(),
                        "tone": "warning",
                    },
                ],
            ),
        ]
