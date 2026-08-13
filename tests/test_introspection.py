"""The introspection facade and the query planner."""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models

from tests.testapp.models import Archive, Author, Category, Comment, Order, Post, Tag
from vantage.introspection import (
    concrete_fields,
    editable_fields,
    generic_foreign_keys,
    is_auto_created_through,
    m2m_fields,
    relation_attr_name,
    relation_query_name,
    resolve_path,
    reverse_relations,
    through_model,
)
from vantage.optimizer import plan_related


def names(fields):
    return [f.name for f in fields]


class TestFieldEnumeration:
    def test_concrete_fields_include_forward_relations(self):
        result = names(concrete_fields(Post))
        assert "title" in result
        assert "author" in result  # forward FK is concrete

    def test_concrete_fields_exclude_m2m(self):
        assert "tags" not in names(concrete_fields(Post))

    def test_concrete_fields_exclude_reverse_relations(self):
        assert "comments" not in names(concrete_fields(Post))

    def test_editable_fields_exclude_auto_timestamps(self):
        result = names(editable_fields(Post))
        assert "title" in result
        assert "created_at" not in result  # auto_now_add
        assert "updated_at" not in result  # auto_now


class TestRelations:
    def test_reverse_relations_found(self):
        rels = reverse_relations(Post)
        assert "comments" in {r.accessor_name for r in rels}

    def test_reverse_fk_is_one_to_many_not_many_to_one(self):
        """Django's flags describe traversal direction, not the class name."""
        rel = next(r for r in reverse_relations(Post) if r.accessor_name == "comments")
        assert rel.one_to_many is True
        assert rel.many_to_one is False

    def test_m2m_fields(self):
        assert names(m2m_fields(Post)) == ["tags"]

    def test_reverse_m2m_is_not_a_forward_m2m(self):
        assert names(m2m_fields(Tag)) == []

    def test_auto_created_through_is_detected(self):
        """Meta.auto_created holds the owning model class, so truthiness is the test."""
        through = through_model(Post._meta.get_field("tags"))
        assert is_auto_created_through(through) is True
        assert through._meta.auto_created is not True  # it is the model class

    def test_query_name_and_attr_name(self):
        rel = next(r for r in reverse_relations(Post) if r.accessor_name == "comments")
        assert relation_attr_name(rel) == "comments"
        assert relation_query_name(rel) == "comments"

    def test_self_referential_fk(self):
        rels = reverse_relations(Category)
        assert "children" in {r.accessor_name for r in rels}


class TestGenericForeignKeys:
    def test_gfk_is_found_in_private_fields(self):
        class Tagged(models.Model):
            content_type = models.ForeignKey(
                "contenttypes.ContentType", on_delete=models.CASCADE
            )
            object_id = models.PositiveIntegerField()
            item = GenericForeignKey("content_type", "object_id")

            class Meta:
                app_label = "testapp"

        gfks = generic_foreign_keys(Tagged)
        assert [f.name for f in gfks] == ["item"]

    def test_gfk_claims_many_to_one_but_has_no_related_model(self):
        """The assumption that breaks naive introspection."""

        class Tagged2(models.Model):
            content_type = models.ForeignKey(
                "contenttypes.ContentType", on_delete=models.CASCADE
            )
            object_id = models.PositiveIntegerField()
            item = GenericForeignKey("content_type", "object_id")

            class Meta:
                app_label = "testapp"

        gfk = generic_foreign_keys(Tagged2)[0]
        assert gfk.many_to_one is True
        assert gfk.related_model is None


class TestResolvePath:
    def test_local_field(self):
        hops = resolve_path(Post, "title")
        assert [h.name for h in hops] == ["title"]
        assert hops[0].is_relation is False

    def test_forward_fk_path(self):
        hops = resolve_path(Post, "author__name")
        assert [h.name for h in hops] == ["author", "name"]
        assert hops[0].is_joinable is True
        assert hops[1].is_relation is False

    def test_nested_fk_path(self):
        hops = resolve_path(Post, "category__parent__name")
        assert [h.name for h in hops] == ["category", "parent", "name"]

    def test_reverse_fk_is_multivalued(self):
        hops = resolve_path(Post, "comments__body")
        assert hops[0].is_multivalued is True
        assert hops[0].is_joinable is False

    def test_m2m_is_multivalued(self):
        hops = resolve_path(Post, "tags__name")
        assert hops[0].is_multivalued is True

    def test_unknown_first_segment_resolves_to_nothing(self):
        assert resolve_path(Post, "not_a_field") == []

    def test_path_stops_at_a_lookup_suffix(self):
        hops = resolve_path(Post, "created_at__gte")
        assert [h.name for h in hops] == ["created_at"]


class TestPlanRelated:
    def test_local_field_needs_nothing(self):
        assert plan_related(Post, ["title"]) == (set(), set())

    def test_forward_fk_becomes_select_related(self):
        select, prefetch = plan_related(Post, ["author__name"])
        assert select == {"author"}
        assert prefetch == set()

    def test_nested_forward_fks_collapse_to_the_longest_path(self):
        select, prefetch = plan_related(Post, ["category__parent__name"])
        assert select == {"category__parent"}
        assert prefetch == set()

    def test_shorter_path_is_absorbed_by_the_longer_one(self):
        select, _ = plan_related(Post, ["category__name", "category__parent__name"])
        assert select == {"category__parent"}

    def test_m2m_becomes_prefetch_related(self):
        select, prefetch = plan_related(Post, ["tags__name"])
        assert select == set()
        assert prefetch == {"tags"}

    def test_reverse_fk_becomes_prefetch_related(self):
        _, prefetch = plan_related(Post, ["comments__body"])
        assert prefetch == {"comments"}

    def test_relation_beyond_a_multivalued_hop_stays_in_one_prefetch(self):
        _, prefetch = plan_related(Comment, ["post__author__name"])
        assert prefetch == set()  # all forward — joinable

        _, prefetch = plan_related(Author, ["posts__category__name"])
        assert prefetch == {"posts"}

    def test_mixed_paths(self):
        select, prefetch = plan_related(
            Post, ["title", "author__name", "category__parent__slug", "tags__name"]
        )
        assert select == {"author", "category__parent"}
        assert prefetch == {"tags"}

    def test_unresolvable_paths_are_ignored_not_raised(self):
        """list_display legitimately contains panel methods; checks report bad paths."""
        assert plan_related(Post, ["some_panel_method"]) == (set(), set())

    def test_protected_fk_is_still_joinable(self):
        select, _ = plan_related(Order, ["customer__email"])
        assert select == {"customer"}

    def test_model_with_no_relations(self):
        assert plan_related(Archive, ["title", "deleted_at"]) == (set(), set())
