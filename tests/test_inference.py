"""Inference is the core value of the package, so it gets the densest coverage.

Adding a model to `tests.testapp.models` is how you add an inference case.
"""

from django.contrib.auth.models import User
from django.db import models

from tests.testapp.models import Archive, Author, Category, Comment, Order, Post, Tag
from vantage.inference import (
    MAX_LIST_DISPLAY,
    infer_all,
    infer_list_display,
    infer_list_filter,
    infer_list_link,
    infer_list_search,
    infer_ordering,
)


class TestListLink:
    def test_prefers_a_conventional_name_field(self):
        assert infer_list_link(Author) == "name"

    def test_prefers_title_when_there_is_no_name(self):
        assert infer_list_link(Post) == "title"

    def test_falls_back_to_the_first_plain_char_field(self):
        assert infer_list_link(Order) == "number"

    def test_falls_back_to_the_primary_key_when_nothing_is_suitable(self):
        class Numeric(models.Model):
            amount = models.IntegerField()

            class Meta:
                app_label = "testapp"

        assert infer_list_link(Numeric) == "id"

    def test_never_links_a_slug(self):
        """A slug is a URL fragment, not a label."""

        class Sluggy(models.Model):
            slug = models.SlugField()
            headline = models.CharField(max_length=50)

            class Meta:
                app_label = "testapp"

        assert infer_list_link(Sluggy) == "headline"

    def test_never_links_a_choice_field(self):
        class Stateful(models.Model):
            state = models.CharField(max_length=10, choices=[("a", "A")])
            caption = models.CharField(max_length=50)

            class Meta:
                app_label = "testapp"

        assert infer_list_link(Stateful) == "caption"


class TestListDisplay:
    def test_the_linked_column_comes_first(self):
        assert infer_list_display(Post)[0] == "title"

    def test_status_and_relations_are_promoted(self):
        columns = infer_list_display(Post)
        assert "status" in columns
        assert "author" in columns

    def test_long_text_is_never_a_column(self):
        assert "body" not in infer_list_display(Post)

    def test_json_is_never_a_column(self):
        assert "metadata" not in infer_list_display(Post)

    def test_images_are_a_column_and_a_high_ranking_one(self):
        """An image renders as a thumbnail, so it is the fastest cell on the row to read.

        This is the reverse of what the rule used to be, and deliberately so: a file
        *path* in a table cell is noise and was excluded for that reason, but
        ``vantage.media`` turns an ``ImageField`` into a picture. Only ``ImageField``
        is promoted — a plain ``FileField`` still has nothing to show but its path.
        """
        columns = infer_list_display(Author)
        assert "avatar" in columns
        assert columns.index("avatar") == 1  # right behind the record's own name

    def test_plain_files_are_still_never_a_column(self):
        class Attachment(models.Model):
            name = models.CharField(max_length=50)
            document = models.FileField(upload_to="docs/")

            class Meta:
                app_label = "testapp"

        assert infer_list_display(Attachment) == ["name"]

    def test_the_primary_key_is_not_a_column(self):
        """The linked column already opens the record; the pk is redundant."""
        assert "id" not in infer_list_display(Post)

    def test_timestamps_sort_to_the_end(self):
        columns = infer_list_display(Author)
        if "created_at" in columns:
            assert columns[-1] == "created_at"

    def test_capped(self):
        assert len(infer_list_display(Post)) <= MAX_LIST_DISPLAY

    def test_model_with_a_single_field(self):
        assert infer_list_display(Tag) == ["name"]

    def test_a_long_char_field_is_not_a_column(self):
        """max_length=300 is prose in disguise; it wrecks row height."""
        assert "summary" not in infer_list_display(Post)

    def test_a_medium_char_field_is_demoted_but_kept(self):
        class Medium(models.Model):
            name = models.CharField(max_length=50)
            note = models.CharField(max_length=150)
            count = models.IntegerField()

            class Meta:
                app_label = "testapp"

        columns = infer_list_display(Medium)
        assert columns.index("note") > columns.index("count")

    def test_sensitive_fields_are_never_shown(self):
        class Account(models.Model):
            username = models.CharField(max_length=50)
            password = models.CharField(max_length=128)
            api_key = models.CharField(max_length=64)
            reset_token = models.CharField(max_length=64)

            class Meta:
                app_label = "testapp"

        columns = infer_list_display(Account)
        assert columns == ["username"]

    def test_real_world_shape_on_the_auth_user_model(self):
        columns = infer_list_display(User)
        assert "password" not in columns
        assert "username" in columns
        assert len(columns) <= MAX_LIST_DISPLAY

    def test_declaration_order_breaks_ties(self):
        """Equal-scoring fields keep the order the developer wrote them in."""

        class Three(models.Model):
            label = models.CharField(max_length=10)
            alpha = models.IntegerField()
            beta = models.IntegerField()
            gamma = models.IntegerField()

            class Meta:
                app_label = "testapp"

        assert infer_list_display(Three) == ["label", "alpha", "beta", "gamma"]


class TestListFilter:
    def test_choice_fields_are_filters(self):
        assert "status" in infer_list_filter(Post)

    def test_booleans_are_filters(self):
        assert "featured" in infer_list_filter(Post)

    def test_relations_are_filters(self):
        assert "author" in infer_list_filter(Post)

    def test_free_text_is_not_a_filter(self):
        assert "title" not in infer_list_filter(Post)

    def test_at_most_one_date_filter(self):
        dates = [f for f in infer_list_filter(Post) if "_at" in f]
        assert len(dates) <= 1

    def test_prefers_a_domain_date_over_an_audit_timestamp(self):
        assert "published_at" in infer_list_filter(Post)
        assert "created_at" not in infer_list_filter(Post)

    def test_capped(self):
        assert len(infer_list_filter(Post)) <= 5

    def test_model_with_nothing_filterable(self):
        assert infer_list_filter(Tag) == []

    def test_sensitive_fields_are_never_filters(self):
        class Keyed(models.Model):
            active = models.BooleanField(default=True)
            secret_token = models.CharField(max_length=40, choices=[("a", "A")])

            class Meta:
                app_label = "testapp"

        assert infer_list_filter(Keyed) == ["active"]


class TestListSearch:
    def test_text_fields_are_searchable(self):
        assert "title" in infer_list_search(Post)

    def test_reaches_one_hop_into_a_foreign_key(self):
        """Searching an order by its customer's name is what people mean by search."""
        assert "customer__name" in infer_list_search(Order)

    def test_does_not_recurse_into_a_self_relation(self):
        assert not any(f.startswith("parent__") for f in infer_list_search(Category))

    def test_an_indexed_field_outranks_an_unindexed_one_of_the_same_kind(self):
        class Indexed(models.Model):
            plain = models.CharField(max_length=50)
            code = models.CharField(max_length=50, db_index=True)

            class Meta:
                app_label = "testapp"

        assert infer_list_search(Indexed) == ["code", "plain"]

    def test_the_human_label_leads(self):
        """A slug's uniqueness bonus is cancelled by its slug penalty, so the title wins."""
        assert infer_list_search(Post)[0] == "title"

    def test_long_text_ranks_last(self):
        result = infer_list_search(Post)
        if "body" in result:
            assert result.index("body") > result.index("title")

    def test_choice_fields_are_not_searched(self):
        """The column stores 'draft'; the user types 'Draft'. Substring match is useless."""
        assert "status" not in infer_list_search(Post)
        assert "status" not in infer_list_search(Order)

    def test_ties_break_on_declaration_order_not_alphabetically(self):
        class Ordered(models.Model):
            zebra = models.CharField(max_length=50)
            apple = models.CharField(max_length=50)

            class Meta:
                app_label = "testapp"

        assert infer_list_search(Ordered) == ["zebra", "apple"]

    def test_capped(self):
        assert len(infer_list_search(Post)) <= 5

    def test_sensitive_fields_are_never_searched(self):
        class Cred(models.Model):
            email = models.EmailField()
            password_hash = models.CharField(max_length=128)

            class Meta:
                app_label = "testapp"

        assert infer_list_search(Cred) == ["email"]

    def test_numeric_only_model_has_no_search(self):
        class Metric(models.Model):
            value = models.IntegerField()

            class Meta:
                app_label = "testapp"

        assert infer_list_search(Metric) == []


class TestOrdering:
    def test_meta_ordering_always_wins(self):
        assert infer_ordering(Post) == ["-created_at"]
        assert infer_ordering(Author) == ["name"]

    def test_newest_first_when_a_timestamp_exists(self):
        assert infer_ordering(Comment) == ["-created_at"]

    def test_falls_back_to_descending_primary_key(self):
        """Pagination needs a total order, and newest-first is a decent proxy."""
        assert infer_ordering(Tag) == ["-id"]

    def test_archive_without_meta_ordering(self):
        assert infer_ordering(Archive) == ["-id"]


class TestInferAll:
    def test_returns_every_attribute(self):
        result = infer_all(Order)
        assert set(result) == {
            "list_display",
            "list_link",
            "list_filter",
            "list_search",
            "ordering",
        }

    def test_the_linked_column_is_always_displayed(self):
        """Otherwise the row has no way to open the record."""
        for model in (Post, Author, Order, Comment, Tag, Archive, Category, User):
            result = infer_all(model)
            assert result["list_link"] in result["list_display"], model.__name__

    def test_every_inferred_path_resolves(self):
        """Inference must never produce a path the query layer will reject."""
        from vantage.introspection import resolve_path

        for model in (Post, Author, Order, Comment, Tag, Archive, Category, User):
            result = infer_all(model)
            for attr in ("list_display", "list_filter", "list_search"):
                for path in result[attr]:
                    assert resolve_path(model, path), f"{model.__name__}.{attr}: {path}"
            for entry in result["ordering"]:
                assert resolve_path(model, entry.lstrip("-")), model.__name__
