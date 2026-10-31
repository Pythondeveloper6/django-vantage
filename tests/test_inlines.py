"""Editable child records on the parent's own form — see ``vantage/inlines.py``.

Not to be confused with ``tests/test_inline.py`` (singular), which covers inline *cell*
editing on the list page. This file is about ``inlines = [OrderItemInline]`` on a
panel: child rows edited alongside their parent, validated with it, saved with it, and
rolled back with it.

The models under test — ``ZInlineOrder``, ``ZInlineItem``, ``ZInlinePart``,
``ZInlineWidget``, ``ZInlineWidgetPart`` — live in ``tests/testapp/models.py``, prefixed
so they never collide with the ``Order``/``SaleLine`` pair the rest of the suite infers
panels for. They are registered here, with an explicit ``inlines`` list, on a private
``VantageSite`` mounted only for this module — see the urlconf below — so no other test
file is affected by any of it.
"""

from __future__ import annotations

import pytest
from django import forms as django_forms
from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import path, reverse

from tests.testapp.models import (
    Author,
    Comment,
    Post,
    Tag,
    ZInlineItem,
    ZInlineOrder,
    ZInlinePart,
    ZInlineWidget,
    ZInlineWidgetPart,
)
from vantage import inlines
from vantage.introspection import is_auto_created_through, m2m_fields, through_model
from vantage.panels import ModelPanel
from vantage.permissions import PanelPermissions
from vantage.sites import VantageSite

# ── a private site, mounted only for this module ────────────────────────────
#
# ``ZInlineOrder``/``ZInlineWidget`` are already auto-registered on the *global*
# default site with a plain ``ModelPanel`` (no inlines) the moment Django starts, same
# as every other model in this app. Registering them again here, with ``inlines`` set,
# happens on a second, private registry — so the global registration is untouched and
# every other test file stays oblivious to any of this.
#
# Named "vantage" (the default), not something distinctive: every ``reverse()`` call
# inside the view layer is hardcoded to the ``vantage:`` namespace
# (``formviews.PanelFormView.get_success_url`` and friends), so a differently-named
# site would 404 its own redirects. Isolation comes from the urlconf swap below
# (``pytest.mark.urls``), which replaces ``ROOT_URLCONF`` for the whole module rather
# than from the site's name.
site = VantageSite()


class ZInlineOrderPanel(ModelPanel):
    model = ZInlineOrder
    inlines: list = []


class ZInlineWidgetPanel(ModelPanel):
    model = ZInlineWidget
    inlines: list = []


order_panel = site.register(ZInlineOrder, ZInlineOrderPanel)
widget_panel = site.register(ZInlineWidget, ZInlineWidgetPanel)

urlpatterns = [path("admin/", site.urls)]

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.test_inlines")]


# ── inline declarations reused across tests ─────────────────────────────────


class ItemInlineFewFields(inlines.Inline):
    """Three short columns, no declared layout: infers tabular from field count/kind."""

    model = ZInlineItem
    fields = ["product", "quantity", "unit_price"]
    extra = 1


class ItemInlineAllFields(inlines.Inline):
    """Every field, no declared layout: eight columns forces stacked on count alone."""

    model = ZInlineItem
    extra = 1


class DenyItemDelete(PanelPermissions):
    """Model-level yes, object-level no — for ``delete`` on ``ZInlineItem`` alone.

    ``PanelPermissions.has_object_perm`` falls back to the model-level answer when no
    backend can answer an object-level question — true here, since only Django's
    ``ModelBackend`` is configured — so this is what makes an *object-level* refusal
    reachable at all: it must actually override the method, the way
    ``tests/test_forms.py``'s ``NoObjectWrites`` does for the parent-record tests.
    """

    def has_object_perm(self, request, model, action, obj):
        if model is ZInlineItem and action == "delete":
            return False
        return super().has_object_perm(request, model, action, obj)


class ItemInlineTabular(inlines.TabularInline):
    model = ZInlineItem
    fields = ["product", "quantity", "unit_price"]
    extra = 0
    can_delete = True
    # See TestUndeclaredMaxNumCrashes: leaving this unset crashes on first use, so
    # every inline actually bound in this file sets one explicitly.
    max_num = 50


class WidgetPartInline(inlines.Inline):
    model = ZInlineWidgetPart
    fields = ["part", "quantity"]
    extra = 1
    max_num = 50


class TagsThroughInline(inlines.Inline):
    """Pinned to the through model Django generated for ``Post.tags``."""

    model = Tag.posts.through
    extra = 1
    max_num = 50


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def boss(client):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


@pytest.fixture
def part(db):
    return ZInlinePart.objects.create(name="Widget")


@pytest.fixture
def order(db):
    return ZInlineOrder.objects.create(reference="ORD-1")


def add_url(model):
    return reverse(f"vantage:{model._meta.label_lower.replace('.', '-')}-add")


def change_url(obj):
    label = obj._meta.label_lower.replace(".", "-")
    return reverse(f"vantage:{label}-change", args=[obj.pk])


def grant(user, model, action):
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(model)
    user.user_permissions.add(
        Permission.objects.get(content_type=ct, codename=f"{action}_{model._meta.model_name}")
    )
    return User.objects.get(pk=user.pk)  # drop the cached permission set


def staff_client(client, *grants):
    """A staff user with exactly the (model, action) permissions listed."""
    user = User.objects.create_user("staffer", password="x", is_staff=True)
    for model, action in grants:
        user = grant(user, model, action)
    client.force_login(user)
    return client


def bound_group(panel, instance, request, *, form=None):
    """Call the real, exported ``inlines.bind`` against a hand-built request.

    A stand-in for ``PanelFormView`` that carries only what ``bind`` reads:
    ``.panel`` and ``.request``, plus either ``.object`` (GET) or a form with
    ``.instance`` (POST) — exactly the contract the docstring on ``bind`` describes.
    Used for the formset-mechanics assertions that do not need a rendered page.
    """
    from types import SimpleNamespace

    view = SimpleNamespace(panel=panel, request=request, object=instance)
    groups = inlines.bind(view, form)
    return groups[0] if groups else None


def order_payload(**overrides):
    data = {"reference": "ORD-NEW"}
    data.update(overrides)
    return data


def formset_prefix(panel, inline_cls, instance, rf, user):
    """The real prefix Django assigned this inline's formset, for building a POST."""
    request = rf.get("/")
    request.user = user
    group = bound_group(panel, instance, request)
    return group.prefix


def item_row(prefix, index, *, part_pk, quantity=1, unit_price="9.99", pk=None, delete=False):
    """One inline row's POST fields, prefixed and indexed the way Django expects."""
    base = f"{prefix}-{index}"
    row = {
        f"{base}-product": str(part_pk),
        f"{base}-quantity": str(quantity),
        f"{base}-unit_price": str(unit_price),
    }
    if pk is not None:
        row[f"{base}-id"] = str(pk)
    if delete:
        row[f"{base}-DELETE"] = "on"
    return row


def management_form(prefix, *, total, initial, min_num=0, max_num=1000):
    return {
        f"{prefix}-TOTAL_FORMS": str(total),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": str(min_num),
        f"{prefix}-MAX_NUM_FORMS": str(max_num),
    }


# ── layout: tabular vs stacked, inferred ─────────────────────────────────────


class TestLayoutInference:
    """Pure calls against ``Inline.get_layout``/``get_fields`` — no request needed."""

    def test_a_declared_layout_is_never_overridden(self):
        assert ItemInlineTabular().get_layout(ZInlineOrder) == inlines.TABULAR

    def test_few_short_fields_default_to_tabular(self):
        assert ItemInlineFewFields().get_layout(ZInlineOrder) == inlines.TABULAR

    def test_more_than_six_columns_defaults_to_stacked_on_count_alone(self):
        """None of ``ZInlineItem``'s own fields is a stacked *kind* — only the count is."""
        inline = ItemInlineAllFields()
        names = inline.get_fields(ZInlineOrder)
        assert len(names) > inlines.TABULAR_FIELD_LIMIT
        assert inline.get_layout(ZInlineOrder) == inlines.STACKED

    def test_a_long_text_field_defaults_to_stacked_regardless_of_count(self):
        """``Comment`` has three fields, well under the limit; ``body`` is a TextField."""

        class CommentInline(inlines.Inline):
            model = Comment

        inline = CommentInline()
        assert len(inline.get_fields(Post)) <= inlines.TABULAR_FIELD_LIMIT
        assert inline.get_layout(Post) == inlines.STACKED

    def test_the_parent_link_is_dropped_from_the_field_list(self):
        inline = ItemInlineAllFields()
        assert "order" not in inline.get_fields(ZInlineOrder)

    def test_resolve_fk_names_the_candidates_on_ambiguity(self, monkeypatch):
        """Two foreign keys from one child to the same parent, faked at the
        ``concrete_fields`` layer rather than with a real ``Model`` subclass.

        A model class defined inline in a test function, with ``Meta.app_label =
        "testapp"``, cannot be un-registered afterwards — Django's app registry is
        process-global — so it stays discoverable by ``apps.get_models()`` for the rest
        of the test run. Vantage's own ``autoregister()`` (``AUTO_REGISTER`` defaults to
        on) then adopts it as an ordinary eligible model, with no backing table because
        it was declared after the test database's tables were already built. The next
        test to walk every registered model — ``test_smoke.py::TestFullRouteWalk`` —
        was failing with ``OperationalError: no such table: testapp_ambiguouschild``
        for exactly that reason. Faking two ``ForeignKey``-shaped stand-ins instead
        proves the same message without registering anything real.
        """
        import types

        from django.core.exceptions import ImproperlyConfigured

        class FakeFK:
            is_relation = True
            many_to_one = True
            one_to_one = False

            def __init__(self, name):
                self.name = name
                self.remote_field = types.SimpleNamespace(model=ZInlineOrder)

        fake_fields = [FakeFK("order_a"), FakeFK("order_b")]
        monkeypatch.setattr(inlines, "concrete_fields", lambda model: fake_fields)

        with pytest.raises(ImproperlyConfigured, match="order_a, order_b"):
            inlines.resolve_fk(ZInlineOrder, object)

    def test_resolve_fk_reports_when_there_is_no_link_at_all(self):
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured, match="no foreign key"):
            inlines.resolve_fk(ZInlineOrder, Tag)


# ── M2M-through: the auto_created trap ──────────────────────────────────────


class TestThroughModels:
    def test_an_ordinary_m2m_gets_an_auto_created_through_model(self):
        through = through_model(Post._meta.get_field("tags"))
        assert is_auto_created_through(through) is True

    def test_the_owning_model_class_is_what_is_actually_stored_there(self):
        """The trap, verified directly: ``Meta.auto_created`` is not the literal ``True``
        the obvious ``is True`` spelling would expect — it is the model that owns the
        M2M field. ``is_auto_created_through`` must be a truthiness test, not an
        identity check, or this would misreport every generated through model."""
        through = through_model(Post._meta.get_field("tags"))
        assert through._meta.auto_created is not True
        assert through._meta.auto_created is Post

    def test_a_hand_written_through_model_is_not_auto_created(self):
        assert is_auto_created_through(ZInlineWidgetPart) is False

    def test_is_generated_through_agrees_for_the_auto_created_case(self):
        class TagsInline(inlines.Inline):
            model = Tag.posts.through

        assert TagsInline().is_through(Post) is True
        assert TagsInline().is_generated_through(Post) is True

    def test_is_generated_through_agrees_for_the_hand_written_case(self):
        class PartsInline(inlines.Inline):
            model = ZInlineWidgetPart

        assert PartsInline().is_through(ZInlineWidget) is True
        assert PartsInline().is_generated_through(ZInlineWidget) is False

    def test_m2m_fields_finds_the_declared_relation(self):
        assert through_model(m2m_fields(ZInlineWidget)[0]) is ZInlineWidgetPart

    def test_the_auto_created_through_model_binds_as_an_ordinary_inline(self, rf, boss):
        """Not just a classification question: the generated through model is a real
        child model with a foreign key back to the parent, so it binds through the same
        ``bind()`` any hand-written child does — a one-column picker for the *other*
        side of the M2M, exactly as :meth:`Inline.is_generated_through`'s docstring
        says an auto-created join table's inline should look."""
        import types

        author = Author.objects.create(name="Ada", email="ada@example.com")
        post = Post.objects.create(title="T", slug="t", body="b", author=author)
        tag = Tag.objects.create(name="engines")
        post.tags.add(tag)

        panel = types.SimpleNamespace(model=Post, inlines=[TagsThroughInline])
        request = rf.get("/")
        request.user = _boss_user(boss)
        group = bound_group(panel, post, request)
        assert group.is_tabular  # a two-column join table: well under the field limit
        saved_rows = [row for row in group.rows if not row.is_extra]
        assert len(saved_rows) == 1
        row = saved_rows[0]
        assert row.label  # a saved row always has *some* label, unlike an extra one
        (tag_cell,) = row.cells
        assert tag_cell.value() == tag.pk


class TestM2MThroughInline:
    @pytest.fixture(autouse=True)
    def _widget_inlines(self, monkeypatch):
        monkeypatch.setattr(widget_panel, "inlines", [WidgetPartInline])

    def test_renders_the_extra_column(self, boss, part):
        widget = ZInlineWidget.objects.create(name="Gizmo")
        body = boss.get(change_url(widget)).content.decode()
        assert "Quantity" in body

    def test_adding_a_row_creates_the_through_row_with_its_extra_field(self, boss, rf, part):
        widget = ZInlineWidget.objects.create(name="Gizmo")
        prefix = formset_prefix(widget_panel, WidgetPartInline, widget, rf, _boss_user(boss))
        data = {"name": "Gizmo"}
        data.update(management_form(prefix, total=1, initial=0))
        data.update(
            {
                f"{prefix}-0-part": str(part.pk),
                f"{prefix}-0-quantity": "3",
            }
        )
        response = boss.post(change_url(widget), data)
        assert response.status_code == 302
        row = ZInlineWidgetPart.objects.get(widget=widget, part=part)
        assert row.quantity == 3
        assert list(widget.parts.all()) == [part]


def _boss_user(client):
    return User.objects.get(username="boss")


# ── add / remove rows: TOTAL_FORMS bookkeeping ───────────────────────────────


class TestAddAndRemoveRows:
    @pytest.fixture(autouse=True)
    def _item_inlines(self, monkeypatch):
        monkeypatch.setattr(order_panel, "inlines", [ItemInlineTabular])

    def test_a_new_order_with_two_new_rows_saves_both(self, boss, rf, part):
        prefix = formset_prefix(
            order_panel, ItemInlineTabular, ZInlineOrder(), rf, _boss_user(boss)
        )
        data = order_payload()
        data.update(management_form(prefix, total=2, initial=0))
        data.update(item_row(prefix, 0, part_pk=part.pk, quantity=1, unit_price="5.00"))
        data.update(item_row(prefix, 1, part_pk=part.pk, quantity=2, unit_price="7.50"))
        response = boss.post(add_url(ZInlineOrder), data)
        assert response.status_code == 302
        order = ZInlineOrder.objects.get(reference="ORD-NEW")
        assert order.items.count() == 2

    def test_the_correct_total_forms_is_what_makes_the_added_row_persist(self, boss, rf, part):
        """The classic bug, proven backwards: TOTAL_FORMS matching the posted rows is
        what makes the second row land at all — the row is not implied by its own
        field data being present."""
        prefix = formset_prefix(
            order_panel, ItemInlineTabular, ZInlineOrder(), rf, _boss_user(boss)
        )
        data = order_payload(reference="ORD-UNDERCOUNT")
        # TOTAL_FORMS says one row; a second row's fields are posted anyway.
        data.update(management_form(prefix, total=1, initial=0))
        data.update(item_row(prefix, 0, part_pk=part.pk, quantity=1, unit_price="5.00"))
        data.update(item_row(prefix, 1, part_pk=part.pk, quantity=99, unit_price="1.00"))
        response = boss.post(add_url(ZInlineOrder), data)
        assert response.status_code == 302
        order = ZInlineOrder.objects.get(reference="ORD-UNDERCOUNT")
        # Only the row TOTAL_FORMS admitted to exists — the second was never parsed.
        assert order.items.count() == 1
        assert order.items.get().quantity == 1

    def test_removing_a_row_deletes_it(self, boss, rf, order, part):
        item = ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")
        prefix = formset_prefix(order_panel, ItemInlineTabular, order, rf, _boss_user(boss))
        data = order_payload(reference=order.reference)
        data.update(management_form(prefix, total=1, initial=1))
        data.update(
            item_row(
                prefix, 0, part_pk=part.pk, quantity=1, unit_price="1.00", pk=item.pk, delete=True
            )
        )
        response = boss.post(change_url(order), data)
        assert response.status_code == 302
        assert not ZInlineItem.objects.filter(pk=item.pk).exists()

    def test_editing_an_existing_row_updates_it_in_place(self, boss, rf, order, part):
        item = ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")
        prefix = formset_prefix(order_panel, ItemInlineTabular, order, rf, _boss_user(boss))
        data = order_payload(reference=order.reference)
        data.update(management_form(prefix, total=1, initial=1))
        data.update(item_row(prefix, 0, part_pk=part.pk, quantity=9, unit_price="1.00", pk=item.pk))
        boss.post(change_url(order), data)
        item.refresh_from_db()
        assert item.quantity == 9
        assert ZInlineItem.objects.filter(order=order).count() == 1


# ── atomicity ─────────────────────────────────────────────────────────────


class TestAtomicity:
    @pytest.fixture(autouse=True)
    def _item_inlines(self, monkeypatch):
        monkeypatch.setattr(order_panel, "inlines", [ItemInlineTabular])

    def test_an_invalid_child_row_saves_neither_it_nor_the_parent(self, boss, rf, part):
        """``unit_price`` is required and not defaulted; leaving it blank fails the row
        — and must fail the whole create, not just that row."""
        prefix = formset_prefix(
            order_panel, ItemInlineTabular, ZInlineOrder(), rf, _boss_user(boss)
        )
        data = order_payload(reference="ORD-ATOMIC")
        data.update(management_form(prefix, total=1, initial=0))
        row = item_row(prefix, 0, part_pk=part.pk, quantity=1)
        row[f"{prefix}-0-unit_price"] = ""
        data.update(row)
        response = boss.post(add_url(ZInlineOrder), data)
        assert response.status_code == 200
        assert not ZInlineOrder.objects.filter(reference="ORD-ATOMIC").exists()

    def test_a_permission_refusal_mid_save_rolls_back_the_parents_own_change(
        self, client, rf, part, settings
    ):
        """A user who may change the order, and who has ``delete_zinlineitem`` at the
        *model* level, still has this particular deletion refused by an
        object-level check — and tries to change the order's own field in the very
        same POST. Both halves validate at the Django-form level (the DELETE checkbox
        is genuinely offered here, unlike the model-level-absent case), so the refusal
        only happens once ``inlines.save`` is running *inside* the parent's own
        transaction — proving the two share one atomic block rather than the child
        merely failing to save while the parent's edit had already landed."""
        settings.VANTAGE = {"PERMISSIONS": "tests.test_inlines.DenyItemDelete"}
        order = ZInlineOrder.objects.create(reference="ORD-ROLLBACK")
        item = ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")
        staff = staff_client(
            client,
            (ZInlineOrder, "view"),
            (ZInlineOrder, "change"),
            (ZInlineItem, "view"),
            (ZInlineItem, "change"),
            (ZInlineItem, "delete"),
        )
        user = User.objects.get(username="staffer")
        prefix = formset_prefix(order_panel, ItemInlineTabular, order, rf, user)
        data = order_payload(reference="ORD-CHANGED")  # the parent's own edit
        data.update(management_form(prefix, total=1, initial=1))
        data.update(
            item_row(
                prefix, 0, part_pk=part.pk, quantity=1, unit_price="1.00", pk=item.pk, delete=True
            )
        )
        response = staff.post(change_url(order), data)
        assert response.status_code == 403
        order.refresh_from_db()
        assert order.reference == "ORD-ROLLBACK"  # the parent's own change did not survive
        assert ZInlineItem.objects.filter(pk=item.pk).exists()  # nor did the deletion


# ── permissions, answered against the child model ────────────────────────────


class TestChildPermissions:
    @pytest.fixture(autouse=True)
    def _item_inlines(self, monkeypatch):
        monkeypatch.setattr(order_panel, "inlines", [ItemInlineTabular])

    def test_changing_the_order_does_not_imply_deleting_its_items(self, client, rf, order, part):
        """Without ``delete_zinlineitem``, the delete control is refused *before* the
        request even reaches ``inlines.save`` — ``may_delete=False`` at bind time drops
        ``can_delete`` from the formset entirely, so a forged ``DELETE`` field is not a
        field the form has at all. The parent's own valid edit still lands; the item is
        just never touched, silently rather than with an error, because there was
        never a delete attempt to refuse."""
        item = ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")
        staff = staff_client(
            client,
            (ZInlineOrder, "view"),
            (ZInlineOrder, "change"),
            (ZInlineItem, "view"),
            (ZInlineItem, "change"),
        )
        user = User.objects.get(username="staffer")
        prefix = formset_prefix(order_panel, ItemInlineTabular, order, rf, user)
        data = order_payload(reference="ORD-2-RENAMED")
        data.update(management_form(prefix, total=1, initial=1))
        data.update(
            item_row(
                prefix, 0, part_pk=part.pk, quantity=1, unit_price="1.00", pk=item.pk, delete=True
            )
        )
        response = staff.post(change_url(order), data)
        assert response.status_code == 302
        order.refresh_from_db()
        assert order.reference == "ORD-2-RENAMED"  # the parent's own edit did land
        assert ZInlineItem.objects.filter(pk=item.pk).exists()  # the forged delete did not

    def test_an_object_level_refusal_is_checked_against_the_child_model_not_the_parent(
        self, client, rf, order, part, settings
    ):
        """The header claim, proven the other way around: even with
        ``delete_zinlineitem`` granted at the model level, and even while the order
        itself is fully editable, an object-level resolver that singles out
        ``ZInlineItem`` refuses the deletion — because the question ``inlines.py`` asks
        names the child model, never the parent ``ZInlineOrder``."""
        settings.VANTAGE = {"PERMISSIONS": "tests.test_inlines.DenyItemDelete"}
        item = ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")
        staff = staff_client(
            client,
            (ZInlineOrder, "view"),
            (ZInlineOrder, "change"),
            (ZInlineItem, "view"),
            (ZInlineItem, "change"),
            (ZInlineItem, "delete"),
        )
        user = User.objects.get(username="staffer")
        prefix = formset_prefix(order_panel, ItemInlineTabular, order, rf, user)
        data = order_payload(reference=order.reference)
        data.update(management_form(prefix, total=1, initial=1))
        data.update(
            item_row(
                prefix, 0, part_pk=part.pk, quantity=1, unit_price="1.00", pk=item.pk, delete=True
            )
        )
        response = staff.post(change_url(order), data)
        assert response.status_code == 403
        assert ZInlineItem.objects.filter(pk=item.pk).exists()

    def test_the_delete_checkbox_is_not_even_offered_without_the_permission(self, client, order):
        staff = staff_client(
            client,
            (ZInlineOrder, "view"),
            (ZInlineOrder, "change"),
            (ZInlineItem, "view"),
            (ZInlineItem, "change"),
        )
        body = staff.get(change_url(order)).content.decode()
        assert "DELETE" not in body

    def test_missing_add_removes_the_extra_row_and_forged_new_rows_do_not_save(
        self, client, rf, order, part
    ):
        staff = staff_client(
            client,
            (ZInlineOrder, "view"),
            (ZInlineOrder, "change"),
            (ZInlineItem, "view"),
            (ZInlineItem, "change"),
        )
        user = User.objects.get(username="staffer")
        request = rf.get("/")
        request.user = user
        group = bound_group(order_panel, order, request)
        assert group.can_add is False
        assert group.empty_row is None

        prefix = group.prefix
        data = order_payload(reference=order.reference)
        # Forged: TOTAL_FORMS claims a second, brand-new row despite the page never
        # offering one, and it is filled in with otherwise-valid data.
        data.update(management_form(prefix, total=1, initial=0))
        data.update(item_row(prefix, 0, part_pk=part.pk, quantity=5, unit_price="5.00"))
        response = staff.post(change_url(order), data)
        assert response.status_code == 302
        assert order.items.count() == 0

    def test_view_only_disables_every_control_so_a_forged_post_is_inert(self, client, order, part):
        item = ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")
        staff = staff_client(client, (ZInlineOrder, "view"), (ZInlineOrder, "change"))
        body = staff.get(change_url(order)).content.decode()
        assert "disabled" in body
        # A readonly inline is never bound, so there is no prefix to forge a POST
        # against in the first place — the change endpoint itself still requires
        # `change_zinlineorder`, which this user has, so the parent-only POST succeeds
        # and the item is untouched regardless.
        response = staff.post(change_url(order), order_payload(reference=order.reference))
        assert response.status_code == 302
        item.refresh_from_db()
        assert item.quantity == 1


# ── validation errors render against the offending row ──────────────────────


class TestRowLevelErrors:
    @pytest.fixture(autouse=True)
    def _item_inlines(self, monkeypatch):
        monkeypatch.setattr(order_panel, "inlines", [ItemInlineTabular])

    def test_the_error_appears_next_to_its_row_not_in_a_heap(self, boss, rf, part):
        prefix = formset_prefix(
            order_panel, ItemInlineTabular, ZInlineOrder(), rf, _boss_user(boss)
        )
        data = order_payload(reference="ORD-ERR")
        data.update(management_form(prefix, total=2, initial=0))
        data.update(item_row(prefix, 0, part_pk=part.pk, quantity=1, unit_price="5.00"))
        bad_row = item_row(prefix, 1, part_pk=part.pk, quantity=1)
        bad_row[f"{prefix}-1-unit_price"] = ""
        data.update(bad_row)
        response = boss.post(add_url(ZInlineOrder), data)
        body = response.content.decode()
        assert response.status_code == 200
        assert "This field is required." in body
        assert f'id="id_{prefix}-1-unit_price_error"' in body
        # The row that failed is marked so it can be found without reading every row.
        assert 'data-invalid="true"' in body

    def test_a_valid_row_is_unaffected_by_a_sibling_rows_error(self, boss, rf, part):
        prefix = formset_prefix(
            order_panel, ItemInlineTabular, ZInlineOrder(), rf, _boss_user(boss)
        )
        data = order_payload(reference="ORD-ERR2")
        data.update(management_form(prefix, total=2, initial=0))
        data.update(item_row(prefix, 0, part_pk=part.pk, quantity=3, unit_price="6.00"))
        bad_row = item_row(prefix, 1, part_pk=part.pk, quantity=1)
        bad_row[f"{prefix}-1-unit_price"] = ""
        data.update(bad_row)
        response = boss.post(add_url(ZInlineOrder), data)
        body = response.content.decode()
        # The valid row's own values survive the re-render.
        assert 'value="6.00"' in body
        assert not ZInlineOrder.objects.filter(reference="ORD-ERR2").exists()


# ── max_num, extra, can_delete ────────────────────────────────────────────


class TestMaxNumExtraCanDelete:
    def test_extra_blank_rows_match_the_declared_count(self, rf, boss, order):
        class OneExtra(inlines.Inline):
            model = ZInlineItem
            fields = ["product", "quantity", "unit_price"]
            extra = 3
            max_num = 50

        request = rf.get("/")
        request.user = _boss_user(boss)
        with_monkeypatch = OneExtra
        import types

        panel = types.SimpleNamespace(model=ZInlineOrder, inlines=[with_monkeypatch])
        group = bound_group(panel, order, request)
        assert group.formset.extra_forms.__len__() == 3

    def test_max_num_limits_how_many_rows_the_page_offers(self, rf, boss, order, part):
        for _ in range(2):
            ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")

        class CappedAtTwo(inlines.Inline):
            model = ZInlineItem
            fields = ["product", "quantity", "unit_price"]
            max_num = 2
            extra = 5

        import types

        panel = types.SimpleNamespace(model=ZInlineOrder, inlines=[CappedAtTwo])
        request = rf.get("/")
        request.user = _boss_user(boss)
        group = bound_group(panel, order, request)
        # Two rows already exist; max_num=2 leaves no room for any extra blank ones.
        assert len(group.rows) == 2
        assert group.empty_row is not None  # can_add is still true — max_num isn't 0

    def test_a_hand_crafted_post_over_max_num_is_refused(self, client, rf, order, part):
        staff = staff_client(
            client,
            (ZInlineOrder, "view"),
            (ZInlineOrder, "change"),
            (ZInlineItem, "view"),
            (ZInlineItem, "add"),
            (ZInlineItem, "change"),
        )
        user = User.objects.get(username="staffer")

        class CappedAtTwo(inlines.Inline):
            model = ZInlineItem
            fields = ["product", "quantity", "unit_price"]
            max_num = 2
            extra = 0

        import types

        panel = types.SimpleNamespace(
            model=ZInlineOrder, inlines=[CappedAtTwo], slug="testapp-zinlineorder"
        )
        request = rf.get("/")
        request.user = user
        group = bound_group(panel, order, request)
        prefix = group.prefix

        data = order_payload(reference=order.reference)
        # Three brand-new rows against a max_num of two.
        data.update(management_form(prefix, total=3, initial=0))
        data.update(item_row(prefix, 0, part_pk=part.pk, quantity=1, unit_price="1.00"))
        data.update(item_row(prefix, 1, part_pk=part.pk, quantity=1, unit_price="1.00"))
        data.update(item_row(prefix, 2, part_pk=part.pk, quantity=1, unit_price="1.00"))

        with __import__("unittest").mock.patch.object(order_panel, "inlines", [CappedAtTwo]):
            response = staff.post(change_url(order), data)
        assert response.status_code == 200  # re-rendered, not saved
        assert order.items.count() == 0

    def test_absolute_max_bounds_the_forms_built_regardless_of_total_forms_claimed(
        self, rf, boss, order
    ):
        """``TOTAL_FORMS`` is attacker-supplied. However large, the number of forms the
        package actually builds is bounded by ``ABSOLUTE_MAX`` (or ``max_num + 100``
        when smaller) rather than growing to match it."""

        class Capped(inlines.Inline):
            model = ZInlineItem
            fields = ["product", "quantity", "unit_price"]
            max_num = 5

        import types

        panel = types.SimpleNamespace(model=ZInlineOrder, inlines=[Capped])
        request = rf.post("/", data={})
        request.user = _boss_user(boss)
        # Minimal management form only — a real forged POST would omit most of the
        # per-row data too; what matters is that the formset never tries to build
        # thousands of forms just because TOTAL_FORMS claims it should.
        request.POST = request.POST.copy()
        request.POST.update(
            {
                "zinlineitem_set-TOTAL_FORMS": "5000",
                "zinlineitem_set-INITIAL_FORMS": "0",
                "zinlineitem_set-MIN_NUM_FORMS": "0",
                "zinlineitem_set-MAX_NUM_FORMS": "0",
            }
        )
        group = bound_group(panel, order, request)
        assert group.formset.absolute_max <= min(5 + 100, inlines.ABSOLUTE_MAX)
        assert len(group.formset.forms) <= group.formset.absolute_max


# ── query cost does not grow with row count ──────────────────────────────────


class TestQueryCost:
    """The module's own guarantee — see the docstring on ``_share_choices`` in
    ``vantage/inlines.py``: "twenty rows with a product select is twenty identical
    SELECT * FROM product queries [without sharing]" and the fix is meant to make it
    "one query instead of twenty". It does not hold for a child model whose ``__str__``
    touches its own foreign key, which the same docstring names as the ordinary case.
    See the two tests below: the first isolates the mechanism, the second proves the
    count actually grows.
    """

    @pytest.fixture(autouse=True)
    def _item_inlines(self, monkeypatch):
        monkeypatch.setattr(order_panel, "inlines", [ItemInlineTabular])

    def _queries_for(self, boss, order):
        with CaptureQueriesContext(connection) as ctx:
            response = boss.get(change_url(order))
        assert response.status_code == 200
        return len(ctx.captured_queries)

    def test_the_hidden_pk_field_is_wrongly_treated_as_a_relation_to_share(
        self, boss, rf, order, part
    ):
        """The root cause, isolated: Django's own formset machinery adds a hidden
        ``id`` field to every row, typed as a ``ModelChoiceField`` whose queryset is
        the *child model itself* rather than a relation the inline declared.
        ``_share_choices`` filters only on ``isinstance(field, forms.ModelChoiceField)``,
        which this field passes, so it is swept into the same "resolve once, share the
        list" treatment real relation fields get — over a queryset nothing ever asked
        to be ``select_related``."""
        ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")
        request = rf.get("/")
        request.user = _boss_user(boss)
        group = bound_group(order_panel, order, request)
        pk_field = group.formset.forms[0].fields["id"]
        assert isinstance(pk_field, django_forms.ModelChoiceField)
        assert pk_field.queryset.model is ZInlineItem  # the child model, not `product`

    def test_the_row_count_actually_does_grow_the_query_count(self, boss, order):
        """A real bug, pinned as the failure it is today rather than papered over with
        a passing "invariance" assertion: because of the mechanism above, resolving
        that hidden field's shared "choices" calls ``label_from_instance`` — ``str()``
        by default — on every row of the *child* queryset fetched for it, and
        ``ZInlineItem.__str__`` reads ``self.product``. That queryset was never
        ``select_related``, so each row is its own uncached query. Twenty rows costs
        roughly twenty more queries than three does — the opposite of the guarantee.
        """
        few_parts = [ZInlinePart.objects.create(name=f"Part {i}") for i in range(3)]
        for part in few_parts:
            ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")
        small = self._queries_for(boss, order)

        big_order = ZInlineOrder.objects.create(reference="ORD-BIG")
        many_parts = [ZInlinePart.objects.create(name=f"Big part {i}") for i in range(20)]
        for part in many_parts:
            ZInlineItem.objects.create(order=big_order, product=part, quantity=1, unit_price="1.00")
        big = self._queries_for(boss, big_order)

        # This is what "the query count does not follow the row count" would look
        # like if the guarantee held — asserted as the current failure, not as a
        # passing invariant, so a fix to `_share_choices` flips this test rather than
        # some unrelated one silently starting to pass.
        assert big > small
        # Bounded so this pins "roughly linear, about one query per extra row" rather
        # than nothing at all — a regression to quadratic would still fail it.
        assert big <= small + 25


# ── extra sanity: readonly hooks win outright, including over the superuser bypass ──


class TestUndeclaredMaxNumCrashes:
    """A real bug, isolated to its own test: an ``Inline`` with no declared ``max_num``
    — the ordinary case, and the one shown in this module's own class docstring
    example — raises ``ValueError`` the moment its formset is built, on every GET and
    every POST, rather than working the way ``max_num: int | None = None`` implies it
    should.

    ``_formset_class`` computes ``absolute_max=ABSOLUTE_MAX`` (500) whenever
    ``inline.max_num`` is ``None``, but passes ``max_num=None`` straight through to
    ``inlineformset_factory`` unchanged. Django's own ``formset_factory`` substitutes
    ``DEFAULT_MAX_NUM`` (1000) for a ``None`` max_num before comparing the two —
    *after* ``vantage`` has already fixed ``absolute_max`` at a number smaller than
    that substitute — so ``max_num (1000) > absolute_max (500)`` and
    ``django.forms.formsets.formset_factory`` raises. Every other inline in this file
    sets ``max_num`` explicitly to work around it; this test is the one place that
    does not, to pin the bug down.
    """

    def test_binding_an_inline_with_no_max_num_raises(self, boss, rf, order):
        class NoMaxNum(inlines.Inline):
            model = ZInlineItem
            fields = ["product", "quantity", "unit_price"]

        request = rf.get("/")
        request.user = _boss_user(boss)
        import types

        panel = types.SimpleNamespace(model=ZInlineOrder, inlines=[NoMaxNum])
        with pytest.raises(ValueError, match="absolute_max"):
            bound_group(panel, order, request)

    def test_the_same_crash_reaches_a_real_request(self, boss, order):
        with __import__("unittest").mock.patch.object(
            order_panel, "inlines", [_InlineWithNoMaxNum]
        ):
            with pytest.raises(ValueError, match="absolute_max"):
                boss.get(change_url(order))


class _InlineWithNoMaxNum(inlines.Inline):
    model = ZInlineItem
    fields = ["product", "quantity", "unit_price"]


class TestHasPermissionHook:
    def test_an_inline_level_hook_refuses_even_a_superuser(self, boss, rf, order, part):
        item = ZInlineItem.objects.create(order=order, product=part, quantity=1, unit_price="1.00")

        class NoDeleteEver(inlines.Inline):
            model = ZInlineItem
            fields = ["product", "quantity", "unit_price"]
            extra = 0

            def has_delete_permission(self, request, obj=None):
                return False

        request = rf.get("/")
        request.user = _boss_user(boss)
        assert NoDeleteEver().allows(request, "delete", item) is False


class HideBigItems(PanelPermissions):
    """Row scoping on the child model: an item of quantity 99 is not this user's."""

    def filter_queryset(self, request, queryset, action="view"):
        queryset = super().filter_queryset(request, queryset, action)
        if queryset.model is ZInlineItem and not request.user.is_superuser:
            return queryset.exclude(quantity=99)
        return queryset


class TestChildRowScope:
    """The child formset lists and edits only rows the child model's scoping allows."""

    @pytest.fixture(autouse=True)
    def _item_inlines(self, monkeypatch, settings):
        settings.VANTAGE = {"PERMISSIONS": "tests.test_inlines.HideBigItems"}
        monkeypatch.setattr(order_panel, "inlines", [ItemInlineTabular])

    def _staff(self, client):
        return staff_client(
            client,
            (ZInlineOrder, "view"),
            (ZInlineOrder, "change"),
            (ZInlineItem, "view"),
            (ZInlineItem, "change"),
        )

    def test_a_scoped_out_child_is_not_shown(self, client, order, part):
        hidden = ZInlineItem.objects.create(
            order=order, product=part, quantity=99, unit_price="123.45"
        )
        page = self._staff(client).get(change_url(order))
        assert page.status_code == 200
        group = page.context["inlines"][0]
        assert hidden.pk not in [form.instance.pk for form in group.formset.forms]

    def test_a_scoped_out_child_cannot_be_edited_by_naming_it(self, client, rf, order, part):
        hidden = ZInlineItem.objects.create(
            order=order, product=part, quantity=99, unit_price="123.45"
        )
        staff = self._staff(client)
        user = User.objects.get(username="staffer")
        prefix = formset_prefix(order_panel, ItemInlineTabular, order, rf, user)
        data = order_payload(reference=order.reference)
        data.update(management_form(prefix, total=1, initial=1))
        data.update(
            item_row(prefix, 0, part_pk=part.pk, quantity=99, unit_price="0.01", pk=hidden.pk)
        )
        staff.post(change_url(order), data)
        hidden.refresh_from_db()
        assert str(hidden.unit_price) == "123.45"
