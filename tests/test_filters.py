"""The filter engine, with the query counts asserted.

Faceting exists to be cheap, so the tests that matter most here are the ones counting
queries: one aggregate for every single-select filter together, one more for each
multi-select filter that has a value ticked, and none at all unless counts were asked
for.
"""

import datetime as dt

import pytest
from django.test import RequestFactory
from django.utils import timezone

from tests.testapp.models import Author, Category, Comment, Post, Tag
from vantage.filters import (
    DATE_PRESETS,
    EMPTY,
    BooleanFilter,
    ChoiceFilter,
    DateFilter,
    FacetCost,
    NumericRangeFilter,
    RelatedFilter,
    apply_facet_counts,
    apply_filters,
    build_filter,
    build_filters,
    facets_requested,
)
from vantage.panels import ModelPanel


def request_for(query=""):
    return RequestFactory().get(f"/?{query}")


def panel_for(model, **attrs):
    return type("Panel", (ModelPanel,), attrs)(model)


def values_of(filt):
    return [choice.value for choice in filt.choices()]


def counts_of(filt):
    return {choice.value: choice.count for choice in filt.choices()}


# ── construction ────────────────────────────────────────────────────────────


class TestBuildFilters:
    def test_picks_a_class_per_field_type(self):
        panel = panel_for(
            Post, list_filter=["status", "featured", "published_at", "author", "view_count"]
        )
        built = build_filters(panel, request_for())
        assert [type(f).__name__ for f in built] == [
            "ChoiceFilter",
            "BooleanFilter",
            "DateFilter",
            "RelatedFilter",
            "NumericRangeFilter",
        ]

    def test_follows_the_panel_rather_than_inference_when_configured(self):
        panel = panel_for(Post, list_filter=["status"])
        assert [f.path for f in build_filters(panel, request_for())] == ["status"]

    def test_falls_back_to_inference(self):
        built = build_filters(panel_for(Post), request_for())
        assert [f.path for f in built] == [
            "status",
            "featured",
            "published_at",
            "author",
            "category",
        ]

    def test_free_text_and_json_never_become_filters(self):
        panel = panel_for(Post, list_filter=["body", "metadata", "title", "slug"])
        assert build_filters(panel, request_for()) == []

    def test_unknown_paths_are_ignored(self):
        panel = panel_for(Post, list_filter=["nonexistent", "status"])
        assert [f.path for f in build_filters(panel, request_for())] == ["status"]

    def test_a_path_ending_in_a_lookup_is_rejected(self):
        """Half an understood path is how a filter silently widens a result set."""
        assert build_filter(Post, "created_at__year") is None

    def test_reaches_through_a_relation(self):
        filt = build_filter(Post, "author__is_active")
        assert isinstance(filt, BooleanFilter)
        assert filt.label == "Author is active"

    def test_parameter_names(self):
        assert build_filter(Post, "status").expected_parameters() == ["status"]
        assert build_filter(Post, "view_count").expected_parameters() == [
            "view_count__gte",
            "view_count__lte",
        ]

    def test_facets_are_opt_in_by_request(self):
        assert facets_requested(request_for()) is False
        assert facets_requested(request_for("_facets=1")) is True


class TestCostClassification:
    def test_choices_and_booleans_are_cheap(self):
        assert build_filter(Post, "status").cost is FacetCost.CHEAP
        assert build_filter(Post, "featured").cost is FacetCost.CHEAP

    def test_an_unindexed_date_is_costly(self):
        assert build_filter(Post, "published_at").cost is FacetCost.COSTLY

    def test_a_numeric_range_is_never_counted(self):
        filt = build_filter(Post, "view_count")
        assert filt.cost is FacetCost.NEVER
        assert filt.choices() == []

    def test_a_multivalued_path_is_never_counted(self):
        """A count across an m2m join double-counts rows."""
        filt = build_filter(Post, "tags")
        assert filt.is_multivalued
        assert filt.cost is FacetCost.NEVER
        assert filt.facet_pairs() is None


# ── query-string handling ───────────────────────────────────────────────────


class TestHostileInput:
    def test_unknown_choice_values_are_dropped(self):
        filt = build_filter(Post, "status", {"status": ["draft", "'; DROP TABLE--"]})
        assert filt.values == ["draft"]

    def test_a_filter_with_only_junk_is_inactive(self):
        filt = build_filter(Post, "status", {"status": "nope"})
        assert filt.is_active is False
        assert filt.q() is None

    def test_junk_booleans_are_dropped(self):
        assert build_filter(Post, "featured", {"featured": "yes"}).values == []
        assert build_filter(Post, "featured", {"featured": "1"}).values == ["1"]

    def test_junk_numbers_are_dropped(self):
        filt = build_filter(Post, "view_count", {"view_count__gte": "ten"})
        assert filt.low is None
        assert filt.q() is None

    def test_junk_dates_are_dropped(self):
        assert build_filter(Post, "published_at", {"published_at": "P7X"}).values == []
        assert build_filter(Post, "published_at", {"published_at": "yesterday"}).values == []

    def test_junk_related_keys_are_dropped(self):
        assert build_filter(Post, "author", {"author": "abc"}).values == []

    def test_duplicate_values_collapse(self):
        filt = build_filter(Post, "status", {"status": ["draft", "draft"]})
        assert filt.values == ["draft"]

    def test_a_single_select_filter_keeps_only_the_first_value(self):
        filt = build_filter(Post, "featured", {"featured": ["1", "0"]})
        assert filt.values == ["1"]


class TestDateWindows:
    def test_presets_serialise_as_durations(self):
        filt = build_filter(Post, "published_at")
        assert values_of(filt)[:4] == ["P1D", "P7D", "P30D", "P90D"]

    def test_a_selected_preset_stays_a_duration(self):
        """A saved view must still mean "the last 7 days" next month."""
        filt = build_filter(Post, "published_at", {"published_at": "P7D"})
        assert filt.values == ["P7D"]

    def test_the_window_includes_today_as_day_one(self):
        filt = build_filter(Post, "published_at", {"published_at": "P7D"})
        today = timezone.localdate()
        children = dict(filt.q().children)
        assert children["published_at__gte"].date() == today - dt.timedelta(days=6)
        assert children["published_at__lt"].date() == today + dt.timedelta(days=1)

    def test_today_is_a_single_day(self):
        filt = build_filter(Post, "published_at", {"published_at": "P1D"})
        children = dict(filt.q().children)
        assert children["published_at__gte"].date() == timezone.localdate()

    def test_this_year_starts_in_january(self):
        filt = build_filter(Post, "published_at", {"published_at": "current-year"})
        children = dict(filt.q().children)
        assert children["published_at__gte"].date() == dt.date(timezone.localdate().year, 1, 1)

    def test_a_custom_range_is_accepted(self):
        filt = build_filter(Post, "published_at", {"published_at": "2026-01-01..2026-03-31"})
        assert filt.values == ["2026-01-01..2026-03-31"]
        assert filt.choices()[-1].label == "2026-01-01 to 2026-03-31"

    def test_an_inverted_range_is_rejected(self):
        filt = build_filter(Post, "published_at", {"published_at": "2026-03-31..2026-01-01"})
        assert filt.values == []

    def test_an_open_ended_range_is_accepted(self):
        filt = build_filter(Post, "published_at", {"published_at": "2026-01-01.."})
        assert dict(filt.q().children).keys() == {"published_at__gte"}

    def test_a_nullable_date_offers_an_empty_option(self):
        assert EMPTY in values_of(build_filter(Post, "published_at"))

    def test_a_plain_date_field_uses_inclusive_bounds(self):
        """Only a datetime column needs the half-open window."""
        from django.db import models as django_models

        class Occasion(django_models.Model):
            happened_on = django_models.DateField()

            class Meta:
                app_label = "testapp"

        filt = build_filter(Occasion, "happened_on", {"happened_on": "P1D"})
        assert isinstance(filt, DateFilter)
        today = timezone.localdate()
        assert dict(filt.q().children) == {
            "happened_on__gte": today,
            "happened_on__lte": today,
        }


# ── applying filters ────────────────────────────────────────────────────────


@pytest.fixture
def library(db):
    author = Author.objects.create(name="Ada", email="ada@example.com")
    other = Author.objects.create(name="Grace", email="grace@example.com")
    news = Category.objects.create(name="News", slug="news")
    now = timezone.now()
    posts = {
        "draft": Post.objects.create(
            title="Draft one",
            slug="draft-one",
            body="x",
            status="draft",
            author=author,
            category=news,
            view_count=5,
            published_at=now,
        ),
        "review": Post.objects.create(
            title="Review one",
            slug="review-one",
            body="x",
            status="review",
            author=author,
            featured=True,
            view_count=50,
            published_at=now - dt.timedelta(days=40),
        ),
        "published": Post.objects.create(
            title="Published one",
            slug="published-one",
            body="x",
            status="published",
            author=other,
            category=news,
            view_count=500,
        ),
    }
    return {"author": author, "other": other, "category": news, "posts": posts}


def titles(queryset):
    return sorted(queryset.values_list("title", flat=True))


@pytest.mark.django_db
class TestApplying:
    def test_choice_filter(self, library):
        filt = build_filter(Post, "status", {"status": "draft"})
        assert titles(filt.queryset(Post.objects.all())) == ["Draft one"]

    def test_multiple_values_are_ored_within_one_filter(self, library):
        filt = build_filter(Post, "status", {"status": ["draft", "review"]})
        assert titles(filt.queryset(Post.objects.all())) == ["Draft one", "Review one"]

    def test_filters_are_anded_across_one_another(self, library):
        panel = panel_for(Post, list_filter=["status", "featured"])
        built = build_filters(panel, request_for("status=draft&status=review&featured=1"))
        assert titles(apply_filters(Post.objects.all(), built)) == ["Review one"]

    def test_boolean_filter(self, library):
        panel = panel_for(Post, list_filter=["featured"])
        built = build_filters(panel, request_for("featured=0"))
        assert titles(apply_filters(Post.objects.all(), built)) == [
            "Draft one",
            "Published one",
        ]

    def test_related_filter(self, library):
        filt = build_filter(Post, "author", {"author": str(library["author"].pk)})
        assert titles(filt.queryset(Post.objects.all())) == ["Draft one", "Review one"]

    def test_related_filter_ors_across_keys(self, library):
        filt = build_filter(
            Post, "author", {"author": [str(library["author"].pk), str(library["other"].pk)]}
        )
        assert len(filt.queryset(Post.objects.all())) == 3

    def test_related_filter_matches_null(self, library):
        filt = build_filter(Post, "category", {"category": EMPTY})
        assert titles(filt.queryset(Post.objects.all())) == ["Review one"]

    def test_related_options_are_labelled_and_ordered(self, library):
        filt = build_filter(Post, "author")
        assert [choice.label for choice in filt.choices()] == ["Ada", "Grace"]

    def test_date_filter(self, library):
        filt = build_filter(Post, "published_at", {"published_at": "P7D"})
        assert titles(filt.queryset(Post.objects.all())) == ["Draft one"]

    def test_date_filter_matches_null(self, library):
        filt = build_filter(Post, "published_at", {"published_at": EMPTY})
        assert titles(filt.queryset(Post.objects.all())) == ["Published one"]

    def test_numeric_range_filter(self, library):
        filt = build_filter(Post, "view_count", {"view_count__gte": "5", "view_count__lte": "50"})
        assert titles(filt.queryset(Post.objects.all())) == ["Draft one", "Review one"]

    def test_a_multivalued_filter_does_not_duplicate_rows(self, library):
        post = library["posts"]["draft"]
        first = Tag.objects.create(name="one")
        second = Tag.objects.create(name="two")
        post.tags.set([first, second])
        filt = build_filter(Post, "tags", {"tags": [str(first.pk), str(second.pk)]})
        assert titles(filt.queryset(Post.objects.all())) == ["Draft one"]

    def test_nothing_selected_leaves_the_queryset_alone(self, library):
        panel = panel_for(Post, list_filter=["status", "featured"])
        built = build_filters(panel, request_for())
        assert len(apply_filters(Post.objects.all(), built)) == 3


# ── faceting ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestFacetCounts:
    def test_counts_are_off_by_default(self, library, django_assert_num_queries):
        panel = panel_for(Post, list_filter=["status", "featured"])
        with django_assert_num_queries(0):
            built = build_filters(panel, request_for())
            assert all(choice.count is None for f in built for choice in f.choices())

    def test_one_query_for_every_filter_when_nothing_is_selected(
        self, library, django_assert_num_queries
    ):
        panel = panel_for(Post, list_filter=["status", "featured"])
        with django_assert_num_queries(1):
            built = build_filters(panel, request_for(), facets=True)
        assert counts_of(built[0]) == {"draft": 1, "review": 1, "published": 1}
        assert counts_of(built[1]) == {"1": 1, "0": 2}

    def test_a_selected_multi_filter_costs_one_extra_query(
        self, library, django_assert_num_queries
    ):
        panel = panel_for(Post, list_filter=["status", "featured"])
        with django_assert_num_queries(2):
            build_filters(panel, request_for("status=draft"), facets=True)

    def test_unselected_options_stay_navigable(self, library):
        """The disjunctive rule: a facet's own predicate is removed from its counts."""
        panel = panel_for(Post, list_filter=["status"])
        status = build_filters(panel, request_for("status=draft"), facets=True)[0]
        assert counts_of(status) == {"draft": 1, "review": 1, "published": 1}

    def test_other_filters_still_narrow_a_disjunctive_count(self, library):
        panel = panel_for(Post, list_filter=["status", "featured"])
        built = build_filters(panel, request_for("status=draft&featured=1"), facets=True)
        # Only the featured post survives the other filter, and it is in review.
        assert counts_of(built[0]) == {"draft": 0, "review": 1, "published": 0}

    def test_a_single_select_filter_counts_within_the_selection(self, library):
        panel = panel_for(Post, list_filter=["status", "featured"])
        built = build_filters(panel, request_for("status=draft"), facets=True)
        assert counts_of(built[1]) == {"1": 0, "0": 1}

    def test_related_counts_cost_one_option_load_plus_the_aggregate(
        self, library, django_assert_num_queries
    ):
        panel = panel_for(Post, list_filter=["author"])
        with django_assert_num_queries(2):
            built = build_filters(panel, request_for(), facets=True)
        assert counts_of(built[0]) == {str(library["author"].pk): 2, str(library["other"].pk): 1}

    def test_costly_filters_are_never_counted(self, library, django_assert_num_queries):
        panel = panel_for(Post, list_filter=["published_at"])
        with django_assert_num_queries(0):
            built = build_filters(panel, request_for(), facets=True)
        assert all(choice.count is None for choice in built[0].choices())

    def test_a_date_filter_counts_its_buckets_once_it_is_cheap(
        self, library, django_assert_num_queries
    ):
        filt = build_filter(Post, "published_at")
        filt.cost = FacetCost.CHEAP
        with django_assert_num_queries(1):
            apply_facet_counts([filt], Post.objects.all())
        assert counts_of(filt)["P7D"] == 1
        assert counts_of(filt)["P90D"] == 2

    def test_the_facet_budget_drops_counts_rather_than_trimming_them(
        self, library, django_assert_num_queries
    ):
        panel = panel_for(Post, list_filter=["status", "category"])
        built = build_filters(
            panel, request_for("status=draft&category=" + str(library["category"].pk))
        )
        with django_assert_num_queries(1):  # the category option load, then nothing
            apply_facet_counts(built, Post.objects.all(), budget=1)
        assert all(choice.count is None for f in built for choice in f.choices())

    def test_counts_use_the_supplied_base_queryset(self, library):
        panel = panel_for(Post, list_filter=["status"])
        scoped = Post.objects.filter(author=library["author"])
        built = build_filters(panel, request_for(), facets=True, queryset=scoped)
        assert counts_of(built[0]) == {"draft": 1, "review": 1, "published": 0}

    def test_counts_are_not_double_counted_across_a_multivalued_join(self, library):
        post = library["posts"]["draft"]
        post.tags.set([Tag.objects.create(name="one"), Tag.objects.create(name="two")])
        panel = panel_for(Post, list_filter=["tags", "status"])
        built = build_filters(panel, request_for(f"tags={post.tags.first().pk}"), facets=True)
        status = built[1]
        assert counts_of(status) == {"draft": 1, "review": 0, "published": 0}


@pytest.mark.django_db
class TestRelatedOptionCap:
    def test_a_high_cardinality_relation_becomes_costly(self, monkeypatch):
        monkeypatch.setattr("vantage.filters.MAX_FACET_CHOICES", 2)
        for index in range(3):
            Post.objects.create(
                title=f"P{index}",
                slug=f"p{index}",
                body="x",
                author=Author.objects.create(name=f"A{index}", email=f"a{index}@example.com"),
            )
        filt = build_filter(Post, "author")
        assert filt.facet_pairs() is None
        assert filt.cost is FacetCost.COSTLY
        assert filt.choices() == []

    def test_a_capped_relation_still_renders_its_selection(self, monkeypatch):
        monkeypatch.setattr("vantage.filters.MAX_FACET_CHOICES", 2)
        authors = [
            Author.objects.create(name=f"A{index}", email=f"a{index}@example.com")
            for index in range(3)
        ]
        filt = build_filter(Post, "author", {"author": str(authors[0].pk)})
        assert [choice.label for choice in filt.choices()] == ["A0"]


# ── other models ────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAcrossModels:
    def test_a_reverse_relation_is_not_offered_as_a_filter(self):
        assert build_filter(Author, "posts") is None

    def test_a_self_referential_relation_works(self):
        parent = Category.objects.create(name="Root", slug="root")
        child = Category.objects.create(name="Child", slug="child", parent=parent)
        filt = build_filter(Category, "parent", {"parent": str(parent.pk)})
        assert list(filt.queryset(Category.objects.all())) == [child]

    def test_a_boolean_on_another_model(self, library):
        Comment.objects.create(
            post=library["posts"]["draft"], author_name="Ada", body="hi", approved=True
        )
        Comment.objects.create(
            post=library["posts"]["draft"], author_name="Bob", body="no", approved=False
        )
        filt = build_filter(Comment, "approved", {"approved": "1"})
        assert filt.queryset(Comment.objects.all()).count() == 1


class TestChoiceConstruction:
    def test_optgroups_are_flattened(self):
        from django.db import models as django_models

        class Grouped(django_models.Model):
            kind = django_models.CharField(
                max_length=10, choices=[("Group", [("a", "A"), ("b", "B")])]
            )

            class Meta:
                app_label = "testapp"

        filt = build_filter(Grouped, "kind", {"kind": "b"})
        assert isinstance(filt, ChoiceFilter)
        assert values_of(filt) == ["a", "b"]
        assert filt.values == ["b"]

    def test_a_non_string_choice_key_round_trips(self):
        from django.db import models as django_models

        class Numbered(django_models.Model):
            level = django_models.IntegerField(choices=[(1, "Low"), (2, "High")])

            class Meta:
                app_label = "testapp"

        filt = build_filter(Numbered, "level", {"level": "2"})
        assert isinstance(filt, ChoiceFilter)
        assert dict(filt.q().children) == {"level": 2}

    def test_a_numeric_field_without_choices_is_a_range(self):
        assert isinstance(build_filter(Post, "rating"), NumericRangeFilter)

    def test_a_related_filter_is_multi_select(self):
        assert RelatedFilter.multi is True
        assert ChoiceFilter.multi is True
        assert BooleanFilter.multi is False
        assert DateFilter.multi is False


class TestPresets:
    def test_every_preset_is_an_iso_duration(self):
        assert set(DATE_PRESETS) == {"P1D", "P7D", "P30D", "P90D"}
