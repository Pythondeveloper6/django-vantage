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


# ── multi-tenancy ────────────────────────────────────────────────────────────
#
# One small constellation for tests/test_tenancy.py: a tenant, something scoped to it
# directly, something scoped through an intermediate model, something ambiguous,
# something with no path to it at all, something meant to be shared by every tenant,
# and something whose tenant column a form never shows. Every name is prefixed ``Tnt``
# so the group reads together and stays out of the way of the fixtures above it. Kept
# separate from ``Snippet`` — the pre-existing single-FK tenancy fixture ``tests/test_
# api.py`` already conscripts ``auth.User`` itself as the tenant for — because several
# of these scenarios (ambiguous, multi-hop, exempt, orphaned) have no honest model to
# reuse there.


class TntOrg(models.Model):
    """The tenant. A model in its own right, not ``auth.User`` wearing a second hat."""

    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)

    def __str__(self):
        return self.name


class TntWidget(models.Model):
    """Owned directly by an org — the single-foreign-key case inference handles alone."""

    org = models.ForeignKey(TntOrg, on_delete=models.CASCADE, related_name="widgets")
    name = models.CharField(max_length=80)
    is_published = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class TntSecret(models.Model):
    """Owned by an org through a field no form ever shows.

    ``editable=False`` drops it from every ModelForm the package builds, so the only
    way this column is ever set is ``vantage.tenancy.enforce`` stamping it on create —
    there is no dropdown standing in front of it to do that job first.
    """

    org = models.ForeignKey(TntOrg, on_delete=models.CASCADE, editable=False, null=True, blank=True)
    label = models.CharField(max_length=80)

    def __str__(self):
        return self.label


class TntGadget(models.Model):
    """Owned through a widget, not directly.

    The multi-hop case inference cannot see on its own — it only ever walks a single
    direct foreign key — so a project has to spell the path out in ``TENANT_SCOPE``.
    """

    widget = models.ForeignKey(TntWidget, on_delete=models.CASCADE, related_name="gadgets")
    name = models.CharField(max_length=80)

    def __str__(self):
        return self.name


class TntAmbiguous(models.Model):
    """Two foreign keys to the tenant — which one owns the row is not knowable from
    the model, so scoping must refuse to guess and fail closed instead."""

    primary_org = models.ForeignKey(TntOrg, on_delete=models.CASCADE, related_name="+")
    secondary_org = models.ForeignKey(TntOrg, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=80)

    def __str__(self):
        return self.name


class TntOrphan(models.Model):
    """No relation to the tenant at all — the forgot-to-configure-it case that must
    still show no rows rather than every tenant's."""

    name = models.CharField(max_length=80)

    def __str__(self):
        return self.name


class TntReference(models.Model):
    """Shared lookup data every tenant reads — the ``TENANT_EXEMPT`` case."""

    label = models.CharField(max_length=80)

    def __str__(self):
        return self.label


class LedgerEntry(models.Model):
    """Deliberately dull, and the only model here meant to be created in bulk.

    The scale tests need a table with tens of thousands of rows in it, so every column
    is cheap to generate and none of them is interesting. The field mix is chosen to
    cover each way a keyset ordering can be refused: ``reference`` is unique and
    non-null, so it is a total order on its own; ``bucket`` repeats, so it is not;
    ``settled_on`` is nullable, so a cursor over it would silently drop rows.
    """

    reference = models.CharField(max_length=40, unique=True)
    bucket = models.CharField(max_length=20)
    amount = models.IntegerField(default=0)
    settled_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name_plural = "ledger entries"

    def __str__(self):
        return self.reference


# ── inlines (tests/test_inlines.py) ─────────────────────────────────────────
#
# Prefixed ``ZInline`` rather than reusing ``Order``/``SaleLine``: these are registered
# with an *explicit* panel carrying ``inlines`` on a dedicated ``VantageSite`` built in
# the test module, and giving them their own models keeps that registration from ever
# touching the panels the rest of the suite infers for ``Order``. The ``Z`` keeps them
# last in the (app_label, model_name) ordering the registry sorts by, for the same
# reason the pattern-detection fixtures above stay after ``post``.


class ZInlinePart(models.Model):
    """A small reference table: the ``product`` on a line item, and the M2M target."""

    name = models.CharField(max_length=80)

    def __str__(self):
        return self.name


class ZInlineOrder(models.Model):
    """Parent for the FK-inline tests — ``ZInlineItem`` is its ``OrderItem``."""

    reference = models.CharField(max_length=40, unique=True)

    def __str__(self):
        return self.reference


class ZInlineItem(models.Model):
    """Child of :class:`ZInlineOrder`.

    Eight columns beyond the parent link on purpose: bound with the first three named
    explicitly an inline infers the short-and-plain tabular layout; bound with every
    field it infers stacked from the column count alone, with no long-text or
    relation-many field anywhere in the model to confound that with the kind-based
    rule. ``unit_price`` has no default and is not nullable, so leaving it blank is a
    row-level validation failure to test atomicity and per-row error rendering against.
    """

    order = models.ForeignKey(ZInlineOrder, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(ZInlinePart, on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=8, decimal_places=2)
    sku = models.CharField(max_length=30, blank=True)
    note = models.CharField(max_length=120, blank=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    taxable = models.BooleanField(default=True)
    backordered = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.quantity} x {self.product}"


class ZInlineWidget(models.Model):
    """Parent for the M2M-through inline tests."""

    name = models.CharField(max_length=80)
    parts = models.ManyToManyField(ZInlinePart, through="ZInlineWidgetPart", related_name="widgets")

    def __str__(self):
        return self.name


class ZInlineWidgetPart(models.Model):
    """A hand-written through model — the case ``is_auto_created_through`` must say
    ``False`` for, unlike ``Post.tags``'s Django-generated one."""

    widget = models.ForeignKey(ZInlineWidget, on_delete=models.CASCADE)
    part = models.ForeignKey(ZInlinePart, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.quantity} x {self.part}"


class Venue(models.Model):
    """Exercises the alternative list views' inference in one model.

    A ``(latitude, longitude)`` pair of ``DecimalField``\\ s for the map, a scheduling
    date (``starts_on``) that must beat the audit stamp for the calendar, a ``stage``
    choices field for the board, and a picture stored in a plain ``FileField`` that is
    only recognisable as one by its name.
    """

    class Stage(models.TextChoices):
        PLANNED = "planned", "Planned"
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    name = models.CharField(max_length=80)
    stage = models.CharField(max_length=10, choices=Stage.choices, default=Stage.PLANNED)
    city = models.CharField(max_length=80, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    photo = models.FileField(upload_to="venues/", blank=True)
    starts_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name
