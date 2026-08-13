"""Fill the demo database with something worth clicking through.

Two properties matter here and both are load-bearing:

* **Deterministic.** The RNG is seeded with a constant, so two people running the demo
  see the same orders and can talk about row 14 and mean the same row.
* **Idempotent.** Every object is looked up by a natural key before it is created, so
  running the command twice adds nothing and running it after a code change refreshes
  the accounts without a rebuild.

No faker dependency: the word lists below are short enough to read and long enough
that the data does not look generated.
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from shop.models import Category, Customer, Order, OrderItem, Product, Review, Tag

#: Anything derived from this seed is stable across machines and runs.
SEED = 20240113

FIRST_NAMES = [
    "Amelia", "Noor", "Owen", "Priya", "Tomas", "Ines", "Kwame", "Sofia",
    "Hugo", "Mei", "Rafael", "Nadia", "Callum", "Yara", "Milos", "Freya",
    "Dario", "Lena", "Idris", "Rosa",
]
LAST_NAMES = [
    "Whitfield", "Okonkwo", "Berger", "Lindqvist", "Marchetti", "Duarte",
    "Halloran", "Nakamura", "Petrov", "Abadi", "Kowalski", "Fontaine",
    "Sorensen", "Vasquez", "Bergstrom", "Achebe", "Rinaldi", "Novak",
    "Delacroix", "Haddad",
]
PLACES = [
    ("Bristol", "GB"), ("Leeds", "GB"), ("Glasgow", "GB"), ("Cork", "IE"),
    ("Galway", "IE"), ("Lyon", "FR"), ("Nantes", "FR"), ("Leipzig", "DE"),
    ("Bremen", "DE"), ("Valencia", "ES"), ("Bilbao", "ES"), ("Utrecht", "NL"),
]

CATEGORY_TREE = {
    "Beverages": ["Coffee", "Tea"],
    "Pantry": ["Baking", "Preserves"],
    "Equipment": ["Brewing kit", "Cleaning"],
}

TAG_NAMES = [
    "Bestseller", "Organic", "Fair trade", "New in",
    "Clearance", "Bulk case", "Seasonal", "Fragile",
]

#: (name, leaf category) pairs, written out rather than generated so the catalogue
#: reads like a catalogue.
PRODUCTS = [
    ("Colombian Dark Roast 1kg", "Coffee"),
    ("Ethiopian Yirgacheffe 500g", "Coffee"),
    ("Brazilian Santos Beans 1kg", "Coffee"),
    ("Decaf House Blend 500g", "Coffee"),
    ("Espresso Bar Blend 3kg", "Coffee"),
    ("Assam Breakfast Loose Leaf", "Tea"),
    ("Sencha Green Tea 250g", "Tea"),
    ("Earl Grey Tea Bags x400", "Tea"),
    ("Peppermint Infusion x200", "Tea"),
    ("Strong Wholemeal Flour 16kg", "Baking"),
    ("Caster Sugar 10kg", "Baking"),
    ("Dried Active Yeast 500g", "Baking"),
    ("Belgian Couverture 70% 5kg", "Baking"),
    ("Vanilla Extract 1L", "Baking"),
    ("Seville Orange Marmalade x12", "Preserves"),
    ("Raspberry Conserve x12", "Preserves"),
    ("Wildflower Honey 5kg", "Preserves"),
    ("Piccalilli Jars x12", "Preserves"),
    ("Commercial Grinder EK-40", "Brewing kit"),
    ("Pour-over Server 1.2L", "Brewing kit"),
    ("Filter Papers x1000", "Brewing kit"),
    ("Milk Jug 900ml", "Brewing kit"),
    ("Group Head Brushes x6", "Cleaning"),
    ("Backflush Detergent 1kg", "Cleaning"),
    ("Descaler Sachets x50", "Cleaning"),
]

REVIEW_HEADLINES = [
    "Exactly what the café needed",
    "Good value in bulk",
    "Arrived faster than promised",
    "Consistent from batch to batch",
    "Packaging could be better",
    "Our regulars noticed the difference",
    "Solid, if a little pricey",
    "Will be ordering again",
    "Not quite as described",
    "Reliable week after week",
]
REVIEW_BODIES = [
    "We have been buying this every fortnight for six months and the quality has not "
    "wavered once. Delivery is predictable, which matters more than anything else "
    "when you are ordering for a busy site.",
    "Ordered a trial case first and went straight to a standing order. The staff "
    "picked up on the change immediately and the feedback has been good.",
    "Does the job. Nothing remarkable about it, but nothing to complain about either, "
    "and at this price per unit it is hard to argue with.",
    "Two of the jars arrived cracked, which the supplier replaced without any fuss. "
    "Marking it down for the packaging rather than the product itself.",
    "Better than the brand we were using before and slightly cheaper by weight. "
    "The only thing I would change is the lead time on larger cases.",
]
ORDER_NOTES = [
    "",
    "",
    "Leave with the neighbour if closed.",
    "Deliver before 10am, kitchen opens at 11.",
    "Split across two pallets please.",
    "Invoice to head office, not the site.",
]


class Command(BaseCommand):
    help = "Populate the demo database with users, a catalogue and six months of orders."

    def handle(self, *args, **options):
        rng = random.Random(SEED)
        with transaction.atomic():
            self._users()
            categories = self._categories()
            tags = self._tags()
            products = self._products(rng, categories, tags)
            customers = self._customers(rng)
            orders = self._orders(rng, customers, products)
            reviews = self._reviews(rng, customers, products)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(customers)} customers, {len(products)} products in "
                f"{len(categories)} categories, {len(orders)} orders and {reviews} reviews."
            )
        )
        self.stdout.write("Sign in as admin / admin12345 or editor / editor12345.")

    # ── accounts ────────────────────────────────────────────────────────────

    def _users(self):
        """Create or reset the two demo accounts.

        The password is set unconditionally so the credentials printed in the README
        stay true even on a database somebody has already poked at.
        """
        self._account("admin", "admin@example.com", "admin12345", superuser=True)
        self._account("editor", "editor@example.com", "editor12345", superuser=False)

    @staticmethod
    def _account(username, email, password, *, superuser):
        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(username=username)
        user.email = email
        user.is_staff = True  # the panel gates on is_staff, not is_superuser
        user.is_superuser = superuser
        user.set_password(password)
        user.save()
        return user

    # ── catalogue ───────────────────────────────────────────────────────────

    def _categories(self):
        categories = {}
        for parent_name, children in CATEGORY_TREE.items():
            parent, _ = Category.objects.get_or_create(
                slug=slugify(parent_name),
                defaults={"name": parent_name, "description": f"Everything under {parent_name}."},
            )
            categories[parent_name] = parent
            for child_name in children:
                child, _ = Category.objects.get_or_create(
                    slug=slugify(child_name),
                    defaults={"name": child_name, "parent": parent},
                )
                categories[child_name] = child
        return categories

    @staticmethod
    def _tags():
        tags = []
        for name in TAG_NAMES:
            tag, _ = Tag.objects.get_or_create(slug=slugify(name), defaults={"name": name})
            tags.append(tag)
        return tags

    def _products(self, rng, categories, tags):
        now = timezone.now()
        products = []
        for index, (name, category_name) in enumerate(PRODUCTS):
            cost = Decimal(str(round(rng.uniform(2.5, 240.0), 2)))
            markup = Decimal(str(round(rng.uniform(1.25, 1.9), 2)))
            price = (cost * markup).quantize(Decimal("0.01"))
            chosen_tags = rng.sample(tags, rng.randint(1, 3))
            product, _ = Product.objects.get_or_create(
                sku=f"NW-{1000 + index}",
                defaults={
                    "name": name,
                    "category": categories[category_name],
                    "status": rng.choices(
                        [Product.Status.ACTIVE, Product.Status.DRAFT, Product.Status.DISCONTINUED],
                        weights=[8, 1, 1],
                    )[0],
                    "price": price,
                    "cost": cost,
                    "stock": rng.randint(0, 900),
                    "is_featured": rng.random() < 0.25,
                    "description": (
                        f"Trade pack of {name.lower()}. Sold by the case; "
                        "price shown is per case excluding VAT."
                    ),
                    "created_at": now - timedelta(days=rng.randint(200, 900)),
                },
            )
            product.tags.set(chosen_tags)
            products.append(product)
        return products

    # ── people ──────────────────────────────────────────────────────────────

    def _customers(self, rng):
        customers = []
        seen = set()
        while len(customers) < 40:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name in seen:
                continue
            seen.add(name)
            city, country = rng.choice(PLACES)
            index = len(customers)
            # A tenth of the book is archived rather than deleted, which is what the
            # deleted_at column is there to show.
            archived = index % 10 == 3
            customer, _ = Customer.objects.get_or_create(
                email=f"{slugify(name)}@example.com",
                defaults={
                    "name": name,
                    "phone": f"+44 7{rng.randint(100, 999)} {rng.randint(100000, 999999)}",
                    "city": city,
                    "country": country,
                    "tier": rng.choices(
                        [Customer.Tier.FREE, Customer.Tier.PLUS, Customer.Tier.PRO],
                        weights=[5, 3, 2],
                    )[0],
                    "is_active": not archived and rng.random() > 0.1,
                    "api_token": f"sk_live_{rng.getrandbits(96):024x}",
                    "joined_on": timezone.localdate() - timedelta(days=rng.randint(30, 1400)),
                    "deleted_at": timezone.now() - timedelta(days=rng.randint(1, 90))
                    if archived
                    else None,
                },
            )
            customers.append(customer)
        return customers

    # ── activity ────────────────────────────────────────────────────────────

    def _orders(self, rng, customers, products):
        now = timezone.now()
        sellable = [p for p in products if p.status != Product.Status.DISCONTINUED]
        orders = []
        for index in range(200):
            placed = now - timedelta(
                days=rng.randint(0, 179), hours=rng.randint(0, 23), minutes=rng.randint(0, 59)
            )
            customer = rng.choice(customers)
            order, _ = Order.objects.get_or_create(
                number=f"ORD-{5000 + index}",
                defaults={
                    "customer": customer,
                    "status": self._status_for(rng, placed, now),
                    "shipping_city": customer.city,
                    "note": rng.choice(ORDER_NOTES),
                    "created_at": placed,
                },
            )
            total = Decimal("0.00")
            for product in rng.sample(sellable, rng.randint(1, 4)):
                quantity = rng.randint(1, 12)
                item, _ = OrderItem.objects.get_or_create(
                    order=order,
                    product=product,
                    defaults={"quantity": quantity, "unit_price": product.price},
                )
                total += item.quantity * item.unit_price
            if order.total != total:
                order.total = total
                order.save(update_fields=["total"])
            orders.append(order)
        return orders

    @staticmethod
    def _status_for(rng, placed, now):
        """Older orders have had time to ship; this week's are mostly still pending."""
        age = (now - placed).days
        if age < 3:
            return rng.choices(
                [Order.Status.PENDING, Order.Status.PAID], weights=[7, 3]
            )[0]
        return rng.choices(
            [
                Order.Status.SHIPPED,
                Order.Status.PAID,
                Order.Status.REFUNDED,
                Order.Status.CANCELLED,
            ],
            weights=[12, 4, 1, 1],
        )[0]

    def _reviews(self, rng, customers, products):
        now = timezone.now()
        for index in range(60):
            # Stepping the customer by a number coprime with 40 keeps every
            # (product, customer) pair distinct, which is what makes get_or_create
            # below an exact idempotency key.
            product = products[index % len(products)]
            customer = customers[(index * 7) % len(customers)]
            Review.objects.get_or_create(
                product=product,
                customer=customer,
                defaults={
                    "headline": rng.choice(REVIEW_HEADLINES),
                    "rating": rng.choices([5, 4, 3, 2, 1], weights=[10, 8, 4, 2, 1])[0],
                    "body": rng.choice(REVIEW_BODIES),
                    "is_published": rng.random() > 0.15,
                    "created_at": now - timedelta(days=rng.randint(0, 179)),
                },
            )
        return Review.objects.count()
