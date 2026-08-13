"""A small wholesale shop, written to be browsed rather than to be complete.

The field mix is deliberate. Between them these models cover choices, booleans,
decimals, dates, a self-referential tree, a many-to-many, a soft-delete column and a
credential column — the shapes inference has to make decisions about. Open the panel
next to this file and every decision is visible without reading any of the library.
"""

from django.db import models
from django.utils import timezone


class Customer(models.Model):
    """Carries the two columns worth watching for: ``api_token`` and ``deleted_at``.

    Neither appears in the list. The token is filtered out by name because rendering a
    credential in a table is a security bug; the soft-delete stamp simply loses to
    higher-signal columns.
    """

    class Tier(models.TextChoices):
        FREE = "free", "Free"
        PLUS = "plus", "Plus"
        PRO = "pro", "Pro"

    class Country(models.TextChoices):
        GB = "GB", "United Kingdom"
        IE = "IE", "Ireland"
        FR = "FR", "France"
        DE = "DE", "Germany"
        ES = "ES", "Spain"
        NL = "NL", "Netherlands"

    name = models.CharField("full name", max_length=120)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=32, blank=True)
    city = models.CharField(max_length=80, blank=True)
    country = models.CharField(max_length=2, choices=Country.choices, default=Country.GB)
    tier = models.CharField(max_length=10, choices=Tier.choices, default=Tier.FREE)
    is_active = models.BooleanField("active", default=True)
    api_token = models.CharField("API token", max_length=64, blank=True)
    joined_on = models.DateField(default=timezone.localdate)
    deleted_at = models.DateTimeField("archived at", null=True, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "customer"
        verbose_name_plural = "customers"

    def __str__(self):
        return self.name


class Category(models.Model):
    """Self-referential, so the demo has a two-level tree to look at."""

    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
        verbose_name="parent category",
    )
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "category"
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=40, unique=True)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "tag"
        verbose_name_plural = "tags"

    def __str__(self):
        return self.name


class Product(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        DISCONTINUED = "discontinued", "Discontinued"

    name = models.CharField(max_length=160)
    sku = models.CharField("SKU", max_length=16, unique=True)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="products"
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="products")
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.ACTIVE)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    cost = models.DecimalField(max_digits=8, decimal_places=2)
    stock = models.PositiveIntegerField("units in stock", default=0)
    is_featured = models.BooleanField("featured", default=False)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        verbose_name = "product"
        verbose_name_plural = "products"

    def __str__(self):
        return self.name

    @property
    def margin(self):
        return self.price - self.cost


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        SHIPPED = "shipped", "Shipped"
        REFUNDED = "refunded", "Refunded"
        CANCELLED = "cancelled", "Cancelled"

    number = models.CharField("order number", max_length=20, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_city = models.CharField(max_length=80, blank=True)
    note = models.TextField(blank=True)
    # A plain default rather than auto_now_add, so the seed command can backdate six
    # months of history and the date filter has something to bite on.
    created_at = models.DateTimeField("placed at", default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "order"
        verbose_name_plural = "orders"

    def __str__(self):
        return self.number


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="order_items")
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "order item"
        verbose_name_plural = "order items"

    def __str__(self):
        return f"{self.quantity} × {self.product}"

    @property
    def line_total(self):
        return self.quantity * self.unit_price


class Review(models.Model):
    RATINGS = [(n, f"{n} star{'' if n == 1 else 's'}") for n in range(1, 6)]

    headline = models.CharField(max_length=120)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField(choices=RATINGS, default=5)
    body = models.TextField()
    is_published = models.BooleanField("published", default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "review"
        verbose_name_plural = "reviews"

    def __str__(self):
        return self.headline
