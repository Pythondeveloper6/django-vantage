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

#: City centres for the map view. Each customer is placed near their city with a small,
#: deterministic offset, so a city reads as a cluster rather than one stacked dot.
COORDINATES = {
    "Bristol": (51.4545, -2.5879), "Leeds": (53.8008, -1.5491),
    "Glasgow": (55.8642, -4.2518), "Cork": (51.8985, -8.4756),
    "Galway": (53.2707, -9.0568), "Lyon": (45.7640, 4.8357),
    "Nantes": (47.2184, -1.5536), "Leipzig": (51.3397, 12.3731),
    "Bremen": (53.0793, 8.8017), "Valencia": (39.4699, -0.3763),
    "Bilbao": (43.2630, -2.9350), "Utrecht": (52.0907, 5.1214),
}

#: One colour per top-level category for the generated product pictures.
PICTURE_COLOURS = {
    "Beverages": ((120, 72, 44), (214, 170, 120)),
    "Pantry": ((52, 110, 74), (180, 214, 160)),
    "Equipment": ((40, 78, 120), (160, 196, 230)),
}

CATEGORY_TREE = {
    "Beverages": ["Coffee", "Tea"],
    "Pantry": ["Baking", "Preserves"],
    "Equipment": ["Brewing kit", "Cleaning"],
}

TAG_NAMES = [
    "Bestseller", "Organic", "Fair trade", "New in",
    "Clearance", "Bulk case", "Seasonal", "Fragile",
]

#: (name, leaf category, trade cost) written out rather than generated. Randomised
#: prices would have put a £300 box of filter papers next to a £4 espresso grinder,
#: and the first thing a reader does with a demo is sanity-check the numbers.
PRODUCTS = [
    ("Colombian Dark Roast 1kg", "Coffee", "14.20"),
    ("Ethiopian Yirgacheffe 500g", "Coffee", "11.75"),
    ("Brazilian Santos Beans 1kg", "Coffee", "12.40"),
    ("Decaf House Blend 500g", "Coffee", "9.30"),
    ("Espresso Bar Blend 3kg", "Coffee", "38.00"),
    ("Assam Breakfast Loose Leaf", "Tea", "8.60"),
    ("Sencha Green Tea 250g", "Tea", "10.15"),
    ("Earl Grey Tea Bags x400", "Tea", "16.80"),
    ("Peppermint Infusion x200", "Tea", "7.45"),
    ("Strong Wholemeal Flour 16kg", "Baking", "13.90"),
    ("Caster Sugar 10kg", "Baking", "9.80"),
    ("Dried Active Yeast 500g", "Baking", "6.25"),
    ("Belgian Couverture 70% 5kg", "Baking", "47.50"),
    ("Vanilla Extract 1L", "Baking", "29.60"),
    ("Seville Orange Marmalade x12", "Preserves", "18.40"),
    ("Raspberry Conserve x12", "Preserves", "19.95"),
    ("Wildflower Honey 5kg", "Preserves", "42.30"),
    ("Piccalilli Jars x12", "Preserves", "17.10"),
    ("Commercial Grinder EK-40", "Brewing kit", "615.00"),
    ("Pour-over Server 1.2L", "Brewing kit", "22.75"),
    ("Filter Papers x1000", "Brewing kit", "13.50"),
    ("Milk Jug 900ml", "Brewing kit", "8.95"),
    ("Group Head Brushes x6", "Cleaning", "11.20"),
    ("Backflush Detergent 1kg", "Cleaning", "15.60"),
    ("Descaler Sachets x50", "Cleaning", "24.40"),
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
            # After everything that draws from ``rng``, and with their own generator,
            # so adding them did not reshuffle a single order in the existing demo.
            self._locations(customers)
            self._pictures(products)

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
        for index, (name, category_name, trade_cost) in enumerate(PRODUCTS):
            cost = Decimal(trade_cost)
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

    @staticmethod
    def _locations(customers):
        """Coordinates for the map view, filled in only where they are missing."""
        jitter = random.Random(SEED + 1)
        for customer in customers:
            if customer.latitude is not None or customer.city not in COORDINATES:
                continue
            lat, lng = COORDINATES[customer.city]
            customer.latitude = round(lat + jitter.uniform(-0.06, 0.06), 5)
            customer.longitude = round(lng + jitter.uniform(-0.09, 0.09), 5)
            customer.save(update_fields=["latitude", "longitude"])

    def _pictures(self, products):
        """A generated picture per product, for the gallery view. Needs Pillow.

        Drawn rather than shipped, so the repository carries no binary assets and the
        demo still runs — with file chips instead of pictures — where Pillow is absent.
        """
        try:
            from PIL import Image, ImageDraw
        except ImportError:  # pragma: no cover - Pillow is optional
            self.stdout.write("Pillow is not installed; products get no pictures.")
            return

        from io import BytesIO

        from django.core.files.base import ContentFile

        for product in products:
            if product.image:
                continue
            parent = product.category.parent or product.category
            dark, light = PICTURE_COLOURS.get(parent.name, ((80, 80, 80), (200, 200, 200)))
            picture = Image.new("RGB", (480, 360), light)
            draw = ImageDraw.Draw(picture)
            for step in range(12):
                inset = step * 14
                shade = tuple(int(d + (l - d) * step / 12) for d, l in zip(dark, light))
                draw.rounded_rectangle(
                    (inset, inset, 480 - inset, 360 - inset), radius=24, fill=shade
                )
            initials = "".join(word[0] for word in product.name.split()[:2]).upper()
            draw.text((240, 180), initials, fill=(255, 255, 255), anchor="mm", font_size=120)
            buffer = BytesIO()
            picture.save(buffer, format="PNG")
            product.image.save(f"{product.sku.lower()}.png", ContentFile(buffer.getvalue()))

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
