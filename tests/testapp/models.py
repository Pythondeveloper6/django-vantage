"""Fixture models for the test suite.

This app deliberately covers a wide spread of field types, relationship shapes and
naming conventions, because inference is the core value of the package and gets the
densest coverage. Adding a model here is how you add an inference test case.
"""

from django.conf import settings
from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    bio = models.TextField(blank=True)
    website = models.URLField(blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Category(models.Model):
    """Self-referential — exercises tree detection."""

    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class Post(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        REVIEW = "review", "In review"
        PUBLISHED = "published", "Published"

    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    body = models.TextField()
    summary = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="posts")
    category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="posts"
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="posts")
    featured = models.BooleanField(default=False)
    view_count = models.PositiveIntegerField(default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class Comment(models.Model):
    """Reverse FK — exercises inline handling."""

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")
    author_name = models.CharField(max_length=100)
    body = models.TextField()
    approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Comment by {self.author_name}"


class Order(models.Model):
    """Numeric and temporal fields — exercises list statistics and charts."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        SHIPPED = "shipped", "Shipped"
        REFUNDED = "refunded", "Refunded"

    number = models.CharField(max_length=20, unique=True)
    customer = models.ForeignKey(Author, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.number


class Archive(models.Model):
    """Soft-delete convention — exercises pattern detection."""

    title = models.CharField(max_length=120)
    deleted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.title



# ── pattern detection ───────────────────────────────────────────────────────
#
# Each convention gets a model that must be detected and, where it has a convincing
# look-alike, one that must not be. The near-misses are the point: a false positive
# hides rows or renders nonsense, so they are fixtures in their own right.
#
# Every name here sorts after ``post``. The registry orders panels by (app_label,
# model_name) and the default dashboard caps its stat cards, so a fixture called
# ``Banner`` would push ``Post`` off the dashboard and break a test that has nothing
# to do with patterns. Keep new fixtures late in the alphabet.


class Preference(models.Model):
    """Near-miss for the timeline: the only date on the model moves on every save."""

    key = models.CharField(max_length=60)
    value = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class Promo(models.Model):
    """Publishable as a boolean."""

    headline = models.CharField(max_length=120)
    is_published = models.BooleanField(default=False)

    def __str__(self):
        return self.headline


class Quote(models.Model):
    """Near-miss for money: ``price`` kept as text, and a decimal that is a measurement."""

    name = models.CharField(max_length=80)
    price = models.CharField(max_length=20)
    total_weight = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    def __str__(self):
        return self.name


class Release(models.Model):
    """Publishable as a nullable timestamp, with no status field to outrank it."""

    version = models.CharField(max_length=20)
    published_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.version


class Report(models.Model):
    """Near-miss for the tree: a self-FK that links two records rather than nesting them."""

    title = models.CharField(max_length=120)
    duplicate_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates"
    )

    def __str__(self):
        return self.title


class SaleLine(models.Model):
    """Near-miss for sortable: ``order`` is a foreign key, not a position."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=8, decimal_places=2)

    def __str__(self):
        return f"{self.quantity} x {self.unit_price}"


class Shipment(models.Model):
    """Near-miss for soft delete: ``deleted`` is a status string, not a flag."""

    reference = models.CharField(max_length=40)
    deleted = models.CharField(max_length=20, default="no")

    def __str__(self):
        return self.reference


class Snippet(models.Model):
    """Owned by a real user, which is the hook a tenant scope attaches to."""

    title = models.CharField(max_length=120)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    def __str__(self):
        return self.title


class Step(models.Model):
    """A manual position — the drag-to-reorder case."""

    label = models.CharField(max_length=60)
    position = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.label


class LiveTicketManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class Ticket(models.Model):
    """Soft delete as a flag, with a default manager that already hides the trash."""

    subject = models.CharField(max_length=120)
    is_deleted = models.BooleanField(default=False)

    objects = LiveTicketManager()

    def __str__(self):
        return self.subject


class TreebeardNode(models.Model):
    """django-treebeard's materialised path. No parent FK at all."""

    name = models.CharField(max_length=80)
    path = models.CharField(max_length=255, unique=True)
    depth = models.PositiveIntegerField(default=1)
    numchild = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.name


class TreeMpttNode(models.Model):
    """django-mptt's column shape. Only a tree when ``mptt`` is installed."""

    name = models.CharField(max_length=80)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    lft = models.PositiveIntegerField(default=0)
    rght = models.PositiveIntegerField(default=0)
    tree_id = models.PositiveIntegerField(default=0)
    level = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.name


class Upload(models.Model):
    """Near-miss for the tree: ``parent`` spelled as free text rather than a relation."""

    name = models.CharField(max_length=80)
    parent = models.CharField(max_length=80, blank=True)

    def __str__(self):
        return self.name
