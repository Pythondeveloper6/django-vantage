"""Model pattern detection.

A false positive here costs more than a miss: a model wrongly treated as soft-deleted
hides rows, and one wrongly treated as a tree renders nonsense. So most of these tests
are about the look-alikes the detectors must decline — the near-miss fixtures in
`tests.testapp.models` exist for no other reason.
"""

import pytest
from django.apps import apps
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q

from tests.testapp.models import (
    Archive,
    Author,
    Category,
    Comment,
    Order,
    Post,
    Preference,
    Promo,
    Quote,
    Release,
    Report,
    SaleLine,
    Shipment,
    Snippet,
    Step,
    Tag,
    Ticket,
    TreebeardNode,
    TreeMpttNode,
    Upload,
)
from vantage.panels import ModelPanel
from vantage.patterns import (
    ModelPatterns,
    detect_all,
    detect_money,
    detect_ownership,
    detect_publishable,
    detect_soft_delete,
    detect_sortable,
    detect_timeline,
    detect_tree,
)

EVERY_FIXTURE = (
    Archive,
    Author,
    Promo,
    Category,
    Comment,
    Snippet,
    Report,
    Upload,
    SaleLine,
    Quote,
    Step,
    TreeMpttNode,
    Order,
    Post,
    Preference,
    Release,
    Shipment,
    Tag,
    Ticket,
    TreebeardNode,
    User,
)


def panel(model, **attrs):
    return type("P", (ModelPanel,), attrs)(model)


@pytest.fixture
def installed(monkeypatch):
    """Pretend an optional library is in INSTALLED_APPS, without installing it.

    Soft detection goes through ``apps.is_installed``, so this patches the real call
    rather than a seam invented for the test.
    """

    def install(*labels):
        real = apps.is_installed
        monkeypatch.setattr(apps, "is_installed", lambda label: label in labels or real(label))

    return install


class TestTree:
    def test_a_self_fk_named_parent_is_a_tree(self):
        found = detect_tree(Category)
        assert found.library == "self_fk"
        assert found.parent_field == "parent"

    def test_roots_are_the_rows_with_no_parent(self):
        assert detect_tree(Category).roots == Q(parent__isnull=True)

    def test_it_finds_the_reverse_accessor_for_children(self):
        """ManyToOneRel reports one_to_many=True — the flags describe traversal."""
        assert detect_tree(Category).children_accessor == "children"

    def test_siblings_follow_meta_ordering_when_there_is_one(self):
        class POrderedTree(models.Model):
            name = models.CharField(max_length=30)
            parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE)

            class Meta:
                app_label = "testapp"
                ordering = ["name"]

        assert detect_tree(POrderedTree).ordering == ("name",)

    def test_siblings_otherwise_sort_by_their_label(self):
        """A tree is read alphabetically far more often than newest-first."""
        assert detect_tree(Category).ordering == ("name",)

    @pytest.mark.parametrize(
        ("model", "why"),
        [
            (Report, "a self-FK that is a link between records, not a hierarchy"),
            (Upload, "parent is free text, not a relation"),
            (Post, "an FK to another model is not a self-reference"),
            (Tag, "no relations at all"),
        ],
    )
    def test_near_misses_are_declined(self, model, why):
        assert detect_tree(model) is None, why

    def test_a_non_nullable_parent_is_declined(self):
        """Without a null parent the tree has no roots, so the view renders nothing."""

        class PRooted(models.Model):
            parent = models.ForeignKey("self", on_delete=models.CASCADE)

            class Meta:
                app_label = "testapp"

        assert detect_tree(PRooted) is None

    def test_a_generic_foreign_key_is_not_mistaken_for_a_parent(self):
        """GenericForeignKey says many_to_one=True and has no related_model at all."""
        from django.contrib.contenttypes.fields import GenericForeignKey
        from django.contrib.contenttypes.models import ContentType

        class PAttachment(models.Model):
            content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
            object_id = models.PositiveIntegerField()
            parent = GenericForeignKey("content_type", "object_id")

            class Meta:
                app_label = "testapp"

        assert detect_tree(PAttachment) is None

    def test_mptt_columns_alone_are_not_mptt(self):
        """The library has to be in INSTALLED_APPS; importable says nothing."""
        assert detect_tree(TreeMpttNode).library == "self_fk"

    def test_mptt_is_recognised_when_installed(self, installed):
        installed("mptt")
        found = detect_tree(TreeMpttNode)
        assert found.library == "mptt"
        assert found.parent_field == "parent"
        assert found.depth_field == "level"

    def test_mptt_orders_by_a_pre_order_traversal(self, installed):
        """tree_id then lft puts every parent immediately before its descendants."""
        installed("mptt")
        assert detect_tree(TreeMpttNode).ordering == ("tree_id", "lft")

    def test_treebeard_materialised_path_is_recognised(self, installed):
        installed("treebeard")
        found = detect_tree(TreebeardNode)
        assert found.library == "treebeard"
        assert found.ordering == ("path",)
        assert found.depth_field == "depth"

    def test_treebeard_has_no_parent_field_to_report(self, installed):
        installed("treebeard")
        assert detect_tree(TreebeardNode).parent_field is None

    def test_treebeard_numbers_its_roots_from_one(self, installed):
        installed("treebeard")
        assert detect_tree(TreebeardNode).roots == Q(depth=1)

    def test_treebeard_nested_set_is_told_apart_from_mptt(self, installed):
        """rgt, not rght — the spelling is the only difference between the shapes."""
        installed("treebeard")

        class PNestedNode(models.Model):
            lft = models.PositiveIntegerField(default=0)
            rgt = models.PositiveIntegerField(default=0)
            tree_id = models.PositiveIntegerField(default=0)
            depth = models.PositiveIntegerField(default=1)

            class Meta:
                app_label = "testapp"

        found = detect_tree(PNestedNode)
        assert found.library == "treebeard"
        assert found.ordering == ("tree_id", "lft")

    def test_treebeard_adjacency_list_is_recognised(self, installed):
        installed("treebeard")

        class PAdjacentNode(models.Model):
            parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE)
            sib_order = models.PositiveIntegerField(default=0)

            class Meta:
                app_label = "testapp"

        found = detect_tree(PAdjacentNode)
        assert found.library == "treebeard"
        assert found.ordering == ("sib_order",)

    def test_an_unrelated_installed_library_changes_nothing(self, installed):
        installed("mptt")
        assert detect_tree(Category).library == "self_fk"

    def test_a_panel_can_switch_it_off(self):
        assert detect_tree(panel(Category, tree=False)) is None

    def test_a_panel_can_name_an_unconventional_parent(self):
        found = detect_tree(panel(Report, tree_parent_field="duplicate_of"))
        assert found.parent_field == "duplicate_of"

    def test_asserting_a_tree_waives_the_name_rule(self):
        assert detect_tree(panel(Report, tree=True)).parent_field == "duplicate_of"

    def test_asserting_a_tree_still_needs_an_unambiguous_parent(self):
        """Two self-FKs and the choice is a coin flip, so it declines and waits."""

        class PTwice(models.Model):
            parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE)
            successor = models.ForeignKey(
                "self", null=True, on_delete=models.CASCADE, related_name="predecessors"
            )

            class Meta:
                app_label = "testapp"

        assert detect_tree(panel(PTwice, tree=True)) is None

    def test_a_named_parent_that_does_not_exist_declines_rather_than_crashes(self):
        assert detect_tree(panel(Category, tree_parent_field="nope")) is None


class TestSoftDelete:
    def test_a_nullable_timestamp_is_soft_delete(self):
        found = detect_soft_delete(Archive)
        assert found.field == "deleted_at"
        assert found.kind == "timestamp"

    def test_the_live_and_trashed_predicates_are_complementary(self):
        found = detect_soft_delete(Archive)
        assert found.live == Q(deleted_at__isnull=True)
        assert found.trashed == Q(deleted_at__isnull=False)

    def test_a_boolean_flag_is_soft_delete(self):
        found = detect_soft_delete(Ticket)
        assert found.field == "is_deleted"
        assert found.kind == "boolean"

    def test_a_flag_is_read_as_not_true_rather_than_false(self):
        """So a nullable flag does not silently drop every row nobody has marked."""
        assert detect_soft_delete(Ticket).live == ~Q(is_deleted=True)

    def test_it_reports_that_the_default_manager_already_hides_the_trash(self):
        """The trash view has to go through _base_manager or it sees nothing."""
        assert detect_soft_delete(Ticket).manager_hides_trashed is True

    def test_a_plain_manager_is_not_reported_as_filtering(self):
        assert detect_soft_delete(Archive).manager_hides_trashed is False

    def test_a_manager_that_only_adds_methods_is_not_reported_as_filtering(self):
        class HelpfulManager(models.Manager):
            def recent(self):
                return self.all()

        class PHelped(models.Model):
            deleted_at = models.DateTimeField(null=True)
            objects = HelpfulManager()

            class Meta:
                app_label = "testapp"

        assert detect_soft_delete(PHelped).manager_hides_trashed is False

    def test_a_bare_deleted_boolean_is_soft_delete(self):
        class PFlagged(models.Model):
            deleted = models.BooleanField(default=False)

            class Meta:
                app_label = "testapp"

        assert detect_soft_delete(PFlagged).kind == "boolean"

    @pytest.mark.parametrize(
        ("model", "why"),
        [
            (Shipment, "deleted holds a status string nothing can guess the values of"),
            (Post, "no deletion marker at all"),
            (Comment, "a created_at is not a deleted_at"),
        ],
    )
    def test_near_misses_are_declined(self, model, why):
        assert detect_soft_delete(model) is None, why

    def test_a_non_nullable_timestamp_is_declined(self):
        """Every row would read as trashed, which is the worst outcome available."""

        class PAlwaysGone(models.Model):
            deleted_at = models.DateTimeField(auto_now_add=True)

            class Meta:
                app_label = "testapp"

        assert detect_soft_delete(PAlwaysGone) is None

    def test_archiving_is_not_deletion(self):
        """A synonym is not enough to put a user's records in a trash bin."""

        class PArchived(models.Model):
            archived_at = models.DateTimeField(null=True)

            class Meta:
                app_label = "testapp"

        assert detect_soft_delete(PArchived) is None
        assert detect_soft_delete(panel(PArchived, soft_delete_field="archived_at")).kind == (
            "timestamp"
        )

    def test_a_panel_can_switch_it_off(self):
        assert detect_soft_delete(panel(Archive, soft_delete=False)) is None

    def test_a_named_field_that_does_not_exist_declines_rather_than_crashes(self):
        assert detect_soft_delete(panel(Archive, soft_delete_field="nope")) is None


class TestTimeline:
    def test_a_domain_date_is_preferred_over_an_audit_one(self):
        """ "Posts per week" means the week they were published in."""
        found = detect_timeline(Post)
        assert found.field == "published_at"
        assert found.is_audit is False

    def test_an_audit_timestamp_is_the_fallback(self):
        found = detect_timeline(Comment)
        assert found.field == "created_at"
        assert found.is_audit is True

    def test_it_reports_that_a_nullable_date_leaves_rows_ungrouped(self):
        assert detect_timeline(Post).nullable is True
        assert detect_timeline(Comment).nullable is False

    def test_a_date_field_is_told_apart_from_a_datetime(self):
        class PInvoice(models.Model):
            issued_on = models.DateField()

            class Meta:
                app_label = "testapp"

        assert detect_timeline(PInvoice).kind == "date"
        assert detect_timeline(Order).kind == "datetime"

    def test_a_moving_timestamp_is_never_a_timeline(self):
        """Buckets that change on every save describe edit activity, not the data."""
        assert detect_timeline(Preference) is None

    def test_the_last_something_family_is_declined_too(self):
        """last_login is not auto_now but it is rewritten just as often."""
        assert detect_timeline(User).field == "date_joined"

    def test_a_soft_delete_timestamp_is_not_a_timeline(self):
        assert detect_timeline(Archive) is None

    def test_a_model_with_no_dates_has_no_timeline(self):
        assert detect_timeline(Tag) is None

    def test_a_panel_can_switch_it_off(self):
        assert detect_timeline(panel(Post, timeline=False)) is None

    def test_a_panel_can_name_the_audit_timestamp_instead(self):
        assert detect_timeline(panel(Post, timeline_field="created_at")).field == "created_at"

    def test_a_named_field_that_is_not_a_date_declines(self):
        assert detect_timeline(panel(Post, timeline_field="title")) is None


class TestSortable:
    def test_an_integer_position_is_sortable(self):
        found = detect_sortable(Step)
        assert found.field == "position"
        assert found.library is None

    @pytest.mark.parametrize("name", ["order", "sort_order", "sequence", "display_order"])
    def test_the_other_spellings_are_recognised(self, name):
        model = type(
            f"Positioned{name.title().replace('_', '')}",
            (models.Model,),
            {
                name: models.PositiveIntegerField(default=0),
                "Meta": type("Meta", (), {"app_label": "testapp"}),
                "__module__": __name__,
            },
        )
        assert detect_sortable(model).field == name

    def test_an_order_foreign_key_is_not_a_position(self):
        """The near-miss this detector exists to get right."""
        assert detect_sortable(SaleLine) is None

    def test_a_choice_field_is_not_a_position(self):
        """Its integers are an enumeration; dragging a row would rewrite its meaning."""

        class PRanked(models.Model):
            position = models.IntegerField(choices=[(1, "First"), (2, "Second")])

            class Meta:
                app_label = "testapp"

        assert detect_sortable(PRanked) is None

    def test_a_model_with_no_position_is_declined(self):
        assert detect_sortable(Post) is None

    def test_djangos_own_order_with_respect_to_is_recognised(self):
        class PChapter(models.Model):
            book = models.ForeignKey(Post, on_delete=models.CASCADE)

            class Meta:
                app_label = "testapp"
                order_with_respect_to = "book"

        found = detect_sortable(PChapter)
        assert found.field == "_order"
        assert found.scope == ("book",)

    def test_ordered_model_is_recognised_when_installed(self, installed):
        installed("ordered_model")

        class OrderedModel(models.Model):
            order = models.PositiveIntegerField(default=0)

            class Meta:
                app_label = "testapp"
                abstract = True

        class PInstruction(OrderedModel):
            order_with_respect_to = "recipe"
            recipe = models.ForeignKey(Post, on_delete=models.CASCADE)

            class Meta:
                app_label = "testapp"

        found = detect_sortable(PInstruction)
        assert found.library == "ordered_model"
        assert found.scope == ("recipe",)

    def test_a_panel_can_switch_it_off(self):
        assert detect_sortable(panel(Step, sortable=False)) is None

    def test_a_panel_can_name_the_field(self):
        assert detect_sortable(panel(Post, sortable_field="view_count")).field == "view_count"


class TestPublishable:
    def test_a_status_choice_that_means_published(self):
        found = detect_publishable(Post)
        assert found.field == "status"
        assert found.kind == "status"
        assert found.publish_value == "published"

    def test_it_knows_what_to_write_when_unpublishing(self):
        assert detect_publishable(Post).unpublish_value == "draft"

    def test_unpublished_means_not_published_rather_than_draft(self):
        """A workflow with a review state has more than two values."""
        assert detect_publishable(Post).unpublished == ~Q(status="published")

    def test_a_status_field_outranks_a_timestamp_on_the_same_model(self):
        """Post has both; the status is the authority and the date is a record of it."""
        assert detect_publishable(Post).field == "status"

    def test_a_boolean_flag_is_publishable(self):
        found = detect_publishable(Promo)
        assert found.kind == "boolean"
        assert found.publish_value is True

    def test_a_nullable_timestamp_is_publishable(self):
        found = detect_publishable(Release)
        assert found.field == "published_at"
        assert found.kind == "timestamp"
        assert found.published == Q(published_at__isnull=False)

    def test_a_timestamp_carries_no_value_to_write(self):
        """A pure detector cannot read the clock; the caller supplies timezone.now()."""
        found = detect_publishable(Release)
        assert found.publish_value is None
        assert found.unpublish_value is None

    def test_a_delivery_workflow_is_not_a_publication_state(self):
        """pending / paid / shipped / refunded has no value meaning "visible"."""
        assert detect_publishable(Order) is None

    def test_unpublished_is_not_read_as_published(self):
        """Choice values are matched whole, never as substrings."""

        class PHidden(models.Model):
            status = models.CharField(
                max_length=20, choices=[("unpublished", "Unpublished"), ("gone", "Gone")]
            )

            class Meta:
                app_label = "testapp"

        assert detect_publishable(PHidden) is None

    def test_the_label_counts_when_the_value_is_a_number(self):
        class PNumbered(models.Model):
            state = models.IntegerField(choices=[(1, "Draft"), (2, "Published")])

            class Meta:
                app_label = "testapp"

        found = detect_publishable(PNumbered)
        assert found.publish_value == 2
        assert found.unpublish_value == 1

    def test_a_non_nullable_publication_date_is_declined(self):
        """Every row is permanently published, so unpublish would have nothing to do."""

        class PAlwaysLive(models.Model):
            published_at = models.DateTimeField(auto_now_add=True)

            class Meta:
                app_label = "testapp"

        assert detect_publishable(PAlwaysLive) is None

    def test_a_model_with_no_publication_state(self):
        assert detect_publishable(Comment) is None

    def test_a_panel_can_switch_it_off(self):
        assert detect_publishable(panel(Post, publishable=False)) is None

    def test_a_panel_can_name_the_field(self):
        found = detect_publishable(panel(Post, publishable_field="featured"))
        assert found.field == "featured"
        assert found.kind == "boolean"


class TestOwnership:
    def test_an_fk_to_the_user_model_is_ownership(self):
        found = detect_ownership(Snippet)
        assert found.field == "owner"
        assert found.user_model is User
        assert found.nullable is True

    def test_a_suggestive_name_pointing_elsewhere_is_declined(self):
        """Post.author is a profile model, and the request's user is not one."""
        assert detect_ownership(Post) is None

    @pytest.mark.parametrize("name", ["user", "created_by", "author"])
    def test_the_other_names_are_recognised(self, name):
        model = type(
            f"Owned{name.title().replace('_', '')}",
            (models.Model,),
            {
                name: models.ForeignKey(User, on_delete=models.CASCADE),
                "Meta": type("Meta", (), {"app_label": "testapp"}),
                "__module__": __name__,
            },
        )
        assert detect_ownership(model).field == name

    def test_owner_outranks_created_by(self):
        """One records tenancy, the other records history."""

        class PBoth(models.Model):
            owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
            created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")

            class Meta:
                app_label = "testapp"

        assert detect_ownership(PBoth).field == "owner"

    def test_a_one_to_one_to_the_user_counts(self):
        class PProfile(models.Model):
            user = models.OneToOneField(User, on_delete=models.CASCADE)

            class Meta:
                app_label = "testapp"

        assert detect_ownership(PProfile).field == "user"

    def test_a_model_with_no_user_relation(self):
        assert detect_ownership(Tag) is None

    def test_a_panel_can_switch_it_off(self):
        assert detect_ownership(panel(Snippet, ownership=False)) is None

    def test_a_panel_can_name_the_field(self):
        class PAssigned(models.Model):
            assignee = models.ForeignKey(User, on_delete=models.CASCADE)

            class Meta:
                app_label = "testapp"

        assert detect_ownership(panel(PAssigned, owner_field="assignee")).field == "assignee"

    def test_a_named_field_pointing_at_the_wrong_model_is_still_declined(self):
        assert detect_ownership(panel(Post, owner_field="author")) is None


class TestMoney:
    def test_a_decimal_named_like_currency(self):
        found = detect_money(Order)
        assert [f.name for f in found] == ["total"]
        assert found[0].decimal_places == 2

    def test_a_money_word_may_end_a_longer_name(self):
        assert [f.name for f in detect_money(SaleLine)] == ["unit_price"]

    def test_a_money_word_in_the_middle_does_not_count(self):
        """total_weight is a measurement; anchoring the suffix is what keeps it out."""
        assert detect_money(Quote) == ()

    def test_currency_in_a_char_field_is_not_money(self):
        assert not any(f.name == "price" for f in detect_money(Quote))

    def test_a_decimal_that_is_not_currency(self):
        """A rating is a decimal and reads as nothing like money."""
        assert detect_money(Post) == ()

    def test_a_float_is_never_money(self):
        """Money in a float is a bug; formatting one dresses up a rounding error."""

        class PLoose(models.Model):
            price = models.FloatField()

            class Meta:
                app_label = "testapp"

        assert detect_money(PLoose) == ()

    def test_several_columns_are_all_reported(self):
        class PInvoice2(models.Model):
            subtotal = models.DecimalField(max_digits=8, decimal_places=2)
            tax_amount = models.DecimalField(max_digits=8, decimal_places=2)
            total = models.DecimalField(max_digits=8, decimal_places=3)

            class Meta:
                app_label = "testapp"

        assert [f.name for f in detect_money(PInvoice2)] == ["subtotal", "tax_amount", "total"]
        assert detect_money(PInvoice2)[2].decimal_places == 3

    def test_a_panel_can_switch_it_off(self):
        assert detect_money(panel(Order, money=False)) == ()

    def test_a_panel_can_name_the_columns(self):
        assert [f.name for f in detect_money(panel(Post, money_fields=["rating"]))] == ["rating"]


class TestDetectAll:
    def test_it_returns_every_detection(self):
        found = detect_all(Post)
        assert isinstance(found, ModelPatterns)
        assert found.model is Post
        assert found.publishable.field == "status"
        assert found.timeline.field == "published_at"

    def test_it_names_what_fired(self):
        assert detect_all(Post).detected == ("timeline", "publishable")
        assert detect_all(Order).detected == ("timeline", "money")

    def test_a_model_following_no_convention_detects_nothing(self):
        assert detect_all(Tag).detected == ()

    def test_a_panel_and_its_model_agree(self):
        assert detect_all(panel(Archive)).soft_delete == detect_all(Archive).soft_delete

    def test_a_panel_override_reaches_detect_all(self):
        assert detect_all(panel(Archive, soft_delete=False)).soft_delete is None

    def test_a_model_whose_field_is_called_model_still_resolves(self):
        """The panel/model split cannot go through getattr(target, "model")."""

        class PDevice(models.Model):
            model = models.CharField(max_length=40)

            class Meta:
                app_label = "testapp"

        assert detect_all(PDevice).model is PDevice

    @pytest.mark.parametrize("model", EVERY_FIXTURE, ids=lambda m: m.__name__)
    def test_no_detector_runs_a_query(self, model, django_assert_num_queries, db):
        """Cardinality is not available to this module, and neither are row counts."""
        with django_assert_num_queries(0):
            detect_all(model)

    @pytest.mark.parametrize("model", EVERY_FIXTURE, ids=lambda m: m.__name__)
    def test_every_reported_field_resolves(self, model):
        """A detection must never name a path the query layer will reject."""
        from vantage.introspection import resolve_path

        found = detect_all(model)
        named = [
            getattr(found.tree, "parent_field", None),
            getattr(found.tree, "depth_field", None),
            getattr(found.soft_delete, "field", None),
            getattr(found.timeline, "field", None),
            getattr(found.sortable, "field", None),
            getattr(found.publishable, "field", None),
            getattr(found.ownership, "field", None),
            *[f.name for f in found.money],
        ]
        if found.tree:
            named.extend(found.tree.ordering)
        for name in [n for n in named if n]:
            assert resolve_path(model, name.lstrip("-")), f"{model.__name__}: {name}"
