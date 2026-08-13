"""Two hand-written panels, and five models left entirely alone.

The comparison is the point of this file. ``Order`` and ``Product`` below say exactly
what they want; ``Customer``, ``Category``, ``Tag``, ``OrderItem`` and ``Review`` say
nothing at all and are auto-registered with inferred columns, filters and search. Open
both kinds in the sidebar and judge whether the configured ones were worth the typing.

Note that ``ProductPanel`` sets ``list_display`` and nothing else: its filters and
search are still inferred. Overriding one attribute never costs you the others.
"""

from shop.models import Order, Product
from vantage import ModelPanel, register


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


@register(Product)
class ProductPanel(ModelPanel):
    label_plural = "Catalogue"
    icon = "box"
    group = "Commerce"

    #: Deliberately drops ``cost`` and reorders the rest — margin is a back-office
    #: number, not something to scan a catalogue by.
    list_display = ["name", "sku", "category", "status", "price", "stock"]
