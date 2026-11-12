import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from shop.models import Category, Customer, Order, OrderItem, Product

CATEGORIES = ["Coffee", "Tea", "Mugs", "Grinders", "Filters"]

PRODUCTS = [
    ("House Blend 1kg", "Coffee", "18.50"),
    ("Single Origin 250g", "Coffee", "9.90"),
    ("Green Tea 100g", "Tea", "6.40"),
    ("Earl Grey 100g", "Tea", "5.80"),
    ("Ceramic Mug", "Mugs", "12.00"),
    ("Travel Mug", "Mugs", "22.50"),
    ("Hand Grinder", "Grinders", "39.00"),
    ("Burr Grinder", "Grinders", "89.00"),
    ("Paper Filters x100", "Filters", "4.20"),
    ("Steel Filter", "Filters", "14.75"),
]

FIRST = [
    "Amira",
    "Ben",
    "Chloe",
    "Dev",
    "Elena",
    "Farid",
    "Grace",
    "Hugo",
    "Ines",
    "Jonas",
    "Kira",
    "Leo",
    "Maya",
    "Noah",
    "Omar",
    "Priya",
    "Quinn",
    "Rosa",
    "Sam",
    "Tara",
]
LAST = ["Stone", "Rivera", "Okafor", "Nguyen", "Larsen", "Haddad", "Moreau", "Patel"]
CITIES = ["London", "Leeds", "Dublin", "Berlin", "Lyon", "Madrid", "Oslo", "Utrecht"]


class Command(BaseCommand):
    help = "Create repeatable demo data for the shop (safe to run twice)."

    @transaction.atomic
    def handle(self, *args, **options):
        if Order.objects.exists():
            self.stdout.write("Shop data already present, nothing to do.")
            return

        rng = random.Random(42)
        now = timezone.now()

        categories = {name: Category.objects.create(name=name) for name in CATEGORIES}
        products = [
            Product.objects.create(
                name=name,
                category=categories[cat],
                price=Decimal(price),
                stock=rng.randint(0, 120),
                is_active=rng.random() > 0.15,
            )
            for name, cat, price in PRODUCTS
        ]

        customers = [
            Customer.objects.create(
                name=f"{first} {LAST[i % len(LAST)]}",
                email=f"{first.lower()}.{i}@example.com",
                city=rng.choice(CITIES),
            )
            for i, first in enumerate(FIRST)
        ]

        statuses = ["pending", "paid", "paid", "paid", "shipped", "shipped", "shipped", "refunded"]
        for _ in range(60):
            order = Order.objects.create(
                customer=rng.choice(customers),
                status=rng.choice(statuses),
                created_at=now - timedelta(days=rng.randint(0, 120), hours=rng.randint(0, 23)),
            )
            total = Decimal("0")
            for product in rng.sample(products, rng.randint(1, 4)):
                qty = rng.randint(1, 5)
                OrderItem.objects.create(
                    order=order, product=product, quantity=qty, unit_price=product.price
                )
                total += product.price * qty
            order.total = total
            order.save(update_fields=["total"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(categories)} categories, {len(products)} products, "
                f"{len(customers)} customers, 60 orders."
            )
        )
