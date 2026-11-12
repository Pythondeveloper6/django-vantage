from django.db.models import Avg, Count, Q, Sum

from shop.models import Category, Customer, Order, OrderItem, Product
from vantage import Chart, ModelPanel, Stat, action, register
from vantage.inlines import Inline


@register(Category)
class CategoryPanel(ModelPanel):
    icon = "tag"
    group = "Catalogue"


@register(Product)
class ProductPanel(ModelPanel):
    icon = "box"
    group = "Catalogue"
    list_display = ["name", "category", "price", "stock", "is_active"]
    list_filter = ["category", "is_active"]
    list_search = ["name", "category__name"]
    # Lesson 6: an agent identity may write products (it still needs a role that allows it).
    agent_access = "write"


@register(Customer)
class CustomerPanel(ModelPanel):
    icon = "users"
    group = "Sales"
    list_display = ["name", "email", "city"]
    list_search = ["name", "email", "city"]
    # Lesson 6: agents never see these fields, in results, filters or writes.
    agent_hidden_fields = ["email"]


class OrderItemInline(Inline):
    model = OrderItem
    fields = ["product", "quantity", "unit_price"]
    extra = 1
    max_num = 50


@register(Order)
class OrderPanel(ModelPanel):
    icon = "shopping-cart"
    group = "Sales"
    order = -10

    list_display = ["id", "customer", "status", "total", "created_at"]
    list_filter = ["status", "created_at", "customer"]
    list_search = ["customer__name", "customer__email"]
    list_per_page = 25

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

    readonly_fields = ["created_at"]
    inlines = [OrderItemInline]
    actions = ["mark_paid", "mark_shipped"]

    @action("Mark as paid", icon="check", permission="change", undo_fields=["status"])
    def mark_paid(self, request, queryset):
        count = queryset.filter(status="pending").update(status="paid")
        return f"Marked {count} pending orders as paid."

    @action("Mark as shipped", icon="truck", permission="change", undo_fields=["status"])
    def mark_shipped(self, request, queryset):
        count = queryset.filter(status="paid").update(status="shipped")
        return f"Marked {count} paid orders as shipped."

    def has_delete_permission(self, request, obj=None):
        # Orders are financial records: nobody deletes them, superusers included.
        return False
