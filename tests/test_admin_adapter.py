"""Adopting ``django.contrib.admin``: the migration path, tested end to end.

The fixture ``admin.py`` lives at the top of this module rather than in
``tests/testapp/``, because ``django.contrib.admin`` is deliberately *not* in the test
project's ``INSTALLED_APPS``. That is the more interesting configuration: it proves the
adapter is a genuinely optional dependency, and that the soft detection is real rather
than decorative. The admins are registered on a private ``AdminSite`` instance, which is
also what a project with a custom admin site has.

The important test in here is ``TestThroughTheRealViews``. A translation that produces
plausible-looking class attributes is worth nothing; what a project adopting Vantage
needs to know is that the list page renders its columns, the search box searches its
fields, the action bar runs its actions and ``has_delete_permission`` still says no.
"""

import types

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import path, reverse

from tests.testapp.models import Author, Comment, Order, Post
from vantage import ModelPanel, site as default_site
from vantage.contrib import django_admin
from vantage.contrib.django_admin import (
    Adoption,
    adopt_admin_site,
    admin_installed,
    check_admin_adapter,
    from_admin,
    panel_from_admin,
)
from vantage.sites import VantageSite

# ── the fixture admin.py ────────────────────────────────────────────────────


def make_featured(modeladmin, request, queryset):
    """A plain function action, the ``(modeladmin, request, queryset)`` shape."""
    return f"Featured {queryset.update(featured=True)} posts."


make_featured.short_description = "Feature selected posts"
make_featured.allowed_permissions = ("change",)


class CommentInline(admin.TabularInline):
    model = Comment
    extra = 1


class PostAdmin(admin.ModelAdmin):
    """A realistic admin: most of the surface, including things that cannot travel."""

    list_display = ("title", "author", "status", "word_count")
    list_display_links = ("title",)
    list_filter = ("status", "featured", "category")
    search_fields = ("title", "body")
    ordering = ("-published_at",)
    list_per_page = 25
    list_select_related = ("author",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)
    exclude = ("metadata",)
    actions = [make_featured, "mark_draft"]

    # None of these come across, and each must be reported.
    inlines = [CommentInline]
    raw_id_fields = ("author",)
    formfield_overrides = {}
    prepopulated_fields = {"slug": ("title",)}

    class Media:
        js = ("post.js",)

    def word_count(self, obj):
        return len(obj.body.split())

    def get_urls(self):
        return super().get_urls()

    @admin.action(description="Move back to draft")
    def mark_draft(self, request, queryset):
        return f"{queryset.update(status='draft')} back to draft."

    def has_delete_permission(self, request, obj=None):
        """Nobody deletes a post. Superusers included — that is the admin's rule."""
        return False


class OrderAdmin(admin.ModelAdmin):
    list_display = ("__str__", "status", "total")
    fieldsets = (
        (None, {"fields": (("number", "customer"),)}),
        ("Money", {"fields": ("total", "status"), "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(status="refunded")


class AuthorAdmin(admin.ModelAdmin):
    """Sets one thing. Everything else must stay inferred."""

    search_fields = ("name",)


@pytest.fixture
def admin_site():
    site = admin.AdminSite(name="fixture-admin")
    site.register(Post, PostAdmin)
    site.register(Order, OrderAdmin)
    site.register(Author, AuthorAdmin)
    return site


@pytest.fixture(autouse=True)
def clean_adoptions():
    django_admin.reset()
    yield
    django_admin.reset()


@pytest.fixture
def post_panel():
    """The translated ``Post`` panel, instantiated the way a site would."""
    adoption = panel_from_admin(Post, PostAdmin)
    return adoption.panel_class(Post)


# ── translation ─────────────────────────────────────────────────────────────


class TestListPageTranslation:
    def test_columns_come_across(self, post_panel):
        assert post_panel.get_list_display() == ["title", "author", "status", "word_count"]

    def test_a_display_method_still_works(self, post_panel):
        """The method belongs to the admin; the panel is a different object."""
        assert post_panel.word_count(Post(body="one two three")) == 3

    def test_the_linked_column_comes_from_list_display_links(self, post_panel):
        assert post_panel.get_list_link() == "title"

    def test_filters_come_across(self, post_panel):
        filters = post_panel.get_list_filter()
        assert {"status", "featured", "category"} <= set(filters)

    def test_search_fields_come_across(self, post_panel):
        assert post_panel.get_list_search() == ["title", "body"]

    def test_ordering_and_page_size_come_across(self, post_panel):
        assert post_panel.get_ordering() == ["-published_at"]
        assert post_panel.get_per_page() == 25

    def test_list_select_related_comes_across(self, post_panel):
        assert post_panel.list_select_related == ["author"]

    # ``True`` is deprecated in Django 6, but adopted admins still carry it.
    @pytest.mark.filterwarnings("ignore:Setting ModelAdmin.list_select_related")
    def test_select_related_true_gives_way_to_inference(self):
        class Auto(admin.ModelAdmin):
            list_select_related = True

        panel = panel_from_admin(Post, Auto).panel_class(Post)
        assert panel.list_select_related is None

    def test_date_hierarchy_becomes_a_date_filter(self, post_panel):
        """Not the same control. The capability — narrow to a period — survives."""
        assert "created_at" in post_panel.get_list_filter()

    def test_date_hierarchy_does_not_freeze_the_inferred_filters(self):
        class Dated(admin.ModelAdmin):
            date_hierarchy = "created_at"

        panel = panel_from_admin(Post, Dated).panel_class(Post)
        filters = panel.get_list_filter()
        assert filters[-1] == "created_at"
        # Everything inference would have offered is still there.
        assert len(filters) > 1

    def test_str_becomes_a_real_column(self):
        """And not one called ``order``, which is ``ModelPanel.order``."""
        panel = panel_from_admin(Order, OrderAdmin).panel_class(Order)
        assert panel.get_list_display() == ["order_display", "status", "total"]
        assert panel.order_display(Order(number="A-1")) == "A-1"
        assert panel.order == 0

    def test_a_str_column_takes_the_model_name_when_it_is_free(self):
        class Titled(admin.ModelAdmin):
            list_display = ("__str__", "status")

        panel = panel_from_admin(Post, Titled).panel_class(Post)
        # ``post`` is neither a Post field nor a ModelPanel attribute, so it is used.
        assert panel.get_list_display() == ["post", "status"]
        assert panel.post(Post(title="Hi")) == "Hi"

    def test_list_display_of_exactly_str_is_indistinguishable_from_no_setting(self):
        """``("__str__",)`` *is* the ModelAdmin default, so it reads as unset — and
        that is the better answer: inferred columns beat one column of ``str(obj)``."""

        class Titled(admin.ModelAdmin):
            list_display = ("__str__",)

        panel = panel_from_admin(Post, Titled).panel_class(Post)
        assert panel.list_display is None
        assert len(panel.get_list_display()) > 1


class TestFormTranslation:
    def test_readonly_and_exclude_come_across(self, post_panel):
        assert post_panel.readonly_fields == ["created_at"]
        assert post_panel.exclude == ["metadata"]

    def test_fields_are_flattened(self):
        class Grouped(admin.ModelAdmin):
            fields = ("title", ("slug", "status"))

        panel = panel_from_admin(Post, Grouped).panel_class(Post)
        assert panel.fields == ["title", "slug", "status"]

    def test_fieldsets_become_a_layout(self):
        from vantage.layout import Row, Section

        panel = panel_from_admin(Order, OrderAdmin).panel_class(Order)
        layout = panel.layout
        assert isinstance(layout, Section)
        first, second = layout.children
        assert isinstance(first.children[0], Row)
        assert list(first.children[0].fields()) == ["number", "customer"]
        assert second.title == "Money"
        assert second.collapsed is True

    def test_a_single_fieldset_is_a_single_section(self):
        class One(admin.ModelAdmin):
            fieldsets = ((None, {"fields": ("title", "body")}),)

        from vantage.layout import Section

        panel = panel_from_admin(Post, One).panel_class(Post)
        assert isinstance(panel.layout, Section)
        assert list(panel.layout.fields()) == ["title", "body"]


class TestInferenceSurvives:
    """Adopting an admin must not cost you the intelligence you came for."""

    def test_an_admin_that_sets_one_thing_overrides_one_thing(self):
        panel = panel_from_admin(Author, AuthorAdmin).panel_class(Author)
        assert panel.get_list_search() == ["name"]
        assert panel.list_display is None
        assert panel.list_filter is None
        assert len(panel.get_list_display()) > 1

    def test_the_default_list_display_is_not_copied(self):
        """Every ModelAdmin has ``list_display = ("__str__",)``; copying it would
        replace a table of inferred columns with one column of ``str(obj)``."""

        class Bare(admin.ModelAdmin):
            pass

        panel = panel_from_admin(Post, Bare).panel_class(Post)
        assert panel.list_display is None
        assert panel.list_per_page is None
        assert panel.get_per_page() == 50

    def test_a_bare_admin_reports_nothing_unmapped(self):
        class Bare(admin.ModelAdmin):
            pass

        assert panel_from_admin(Post, Bare).unmapped == ()


class TestActions:
    def test_actions_come_across_with_their_labels(self, post_panel):
        from vantage.actions import resolve_actions

        actions = {a.name: a for a in resolve_actions(post_panel)}
        assert set(actions) == {"make_featured", "mark_draft"}
        assert actions["make_featured"].label == "Feature selected posts"
        assert actions["mark_draft"].label == "Move back to draft"

    def test_an_action_permission_comes_from_allowed_permissions(self, post_panel):
        from vantage.actions import resolve_actions

        actions = {a.name: a for a in resolve_actions(post_panel)}
        assert actions["make_featured"].permission == "change"

    def test_a_function_action_runs(self, db, post_panel):
        from vantage.actions import resolve_actions

        author = Author.objects.create(name="Ada", email="ada@example.com")
        Post.objects.create(title="P", slug="p", body="b", author=author)
        action = next(a for a in resolve_actions(post_panel) if a.name == "make_featured")
        result = action.run(post_panel, None, Post.objects.all())
        assert result.message == "Featured 1 posts."
        assert Post.objects.filter(featured=True).count() == 1

    def test_a_method_action_runs(self, db, post_panel):
        from vantage.actions import resolve_actions

        author = Author.objects.create(name="Ada", email="ada@example.com")
        Post.objects.create(title="P", slug="p", body="b", author=author, status="published")
        action = next(a for a in resolve_actions(post_panel) if a.name == "mark_draft")
        assert action.run(post_panel, None, Post.objects.all()).message == "1 back to draft."

    def test_delete_selected_is_dropped(self):
        class WithDelete(admin.ModelAdmin):
            actions = ["delete_selected"]

        panel = panel_from_admin(Post, WithDelete).panel_class(Post)
        assert panel.actions == ModelPanel.actions

    def test_a_redirecting_action_becomes_a_redirect(self, db):
        from django.http import HttpResponseRedirect

        def go_elsewhere(modeladmin, request, queryset):
            return HttpResponseRedirect("/somewhere/")

        class Redirecting(admin.ModelAdmin):
            actions = [go_elsewhere]

        panel = panel_from_admin(Post, Redirecting).panel_class(Post)
        result = panel.actions[0].run(panel, None, Post.objects.none())
        assert result.redirect == "/somewhere/"

    def test_a_custom_action_permission_is_reported_not_guessed(self):
        def approve(modeladmin, request, queryset):
            return "ok"

        approve.allowed_permissions = ("approve",)

        class Approving(admin.ModelAdmin):
            actions = [approve]

        adoption = panel_from_admin(Post, Approving)
        assert any("'approve'" in item for item in adoption.unmapped)
        assert adoption.panel_class.actions[0].permission == "change"


class TestDelegatedHooks:
    def test_get_queryset_is_delegated(self, db):
        author = Author.objects.create(name="Ada", email="ada@example.com")
        Order.objects.create(number="A-1", customer=author, total=1, status="paid")
        Order.objects.create(number="A-2", customer=author, total=1, status="refunded")

        panel = panel_from_admin(Order, OrderAdmin).panel_class(Order)
        assert [o.number for o in panel.get_queryset(None)] == ["A-1"]

    def test_save_model_is_delegated(self):
        saved = []

        class Saving(admin.ModelAdmin):
            def save_model(self, request, obj, form, change):
                saved.append(obj)

        panel = panel_from_admin(Post, Saving).panel_class(Post)
        panel.save_model(None, "obj", None, False)
        assert saved == ["obj"]

    def test_an_unoverridden_hook_is_not_delegated(self):
        class Bare(admin.ModelAdmin):
            pass

        panel_class = panel_from_admin(Post, Bare).panel_class
        assert "save_model" not in vars(panel_class)
        assert panel_class.get_queryset is ModelPanel.get_queryset


class TestPermissionTranslation:
    def test_has_delete_permission_becomes_a_panel_hook(self, post_panel):
        assert post_panel.has_delete_permission(None) is False

    def test_only_overridden_methods_are_translated(self, post_panel):
        assert not hasattr(post_panel, "has_view_permission")

    def test_has_add_permission_is_called_without_an_object(self):
        seen = []

        class Restricted(admin.ModelAdmin):
            def has_add_permission(self, request):
                seen.append(request)
                return False

        panel = panel_from_admin(Post, Restricted).panel_class(Post)
        assert panel.has_add_permission("req") is False
        assert seen == ["req"]


# ── the honest report ───────────────────────────────────────────────────────


class TestUnmappedReport:
    def test_inlines_are_reported(self, post_panel):
        adoption = panel_from_admin(Post, PostAdmin)
        assert any(item.startswith("inlines") for item in adoption.unmapped)

    def test_every_untranslatable_setting_is_named(self):
        adoption = panel_from_admin(Post, PostAdmin)
        reported = " | ".join(adoption.unmapped)
        for expected in (
            "inlines",
            "raw_id_fields",
            "prepopulated_fields",
            "get_urls()",
            "Media",
        ):
            assert expected in reported

    def test_the_check_lists_them_per_model(self):
        panel_from_admin(Post, PostAdmin)
        messages = check_admin_adapter(None)
        assert [m.id for m in messages] == ["vantage.W040"]
        assert "PostAdmin" in messages[0].msg
        assert "testapp.Post" in messages[0].msg
        assert "inlines" in messages[0].hint

    def test_a_clean_admin_produces_no_check_output(self):
        panel_from_admin(Author, AuthorAdmin)
        assert check_admin_adapter(None) == []

    def test_the_check_filters_by_app_config(self):
        from django.apps import apps as django_apps

        panel_from_admin(Post, PostAdmin)
        other = [django_apps.get_app_config("auth")]
        assert check_admin_adapter(other) == []
        assert check_admin_adapter([django_apps.get_app_config("testapp")])

    def test_a_custom_list_filter_class_is_reported_and_the_field_kept(self):
        class Recent(admin.SimpleListFilter):
            title = "recent"
            parameter_name = "recent"

            def lookups(self, request, model_admin):
                return []

            def queryset(self, request, queryset):
                return queryset

        class Filtered(admin.ModelAdmin):
            list_filter = ("status", ("created_at", admin.DateFieldListFilter), Recent)

        adoption = panel_from_admin(Post, Filtered)
        panel = adoption.panel_class(Post)
        assert panel.list_filter == ["status", "created_at"]
        reported = " | ".join(adoption.unmapped)
        assert "DateFieldListFilter" in reported
        assert "Recent" in reported

    def test_a_short_description_on_a_column_is_reported(self):
        class Described(admin.ModelAdmin):
            list_display = ("title", "shout")

            @admin.display(description="LOUD")
            def shout(self, obj):
                return obj.title.upper()

        adoption = panel_from_admin(Post, Described)
        assert any("short_description" in item for item in adoption.unmapped)

    def test_a_computed_readonly_field_is_reported(self):
        class Computed(admin.ModelAdmin):
            readonly_fields = ("word_count",)

            def word_count(self, obj):
                return 0

        adoption = panel_from_admin(Post, Computed)
        assert any("word_count" in item for item in adoption.unmapped)


# ── registration entry points ───────────────────────────────────────────────


class TestFromAdminDecorator:
    def test_it_fills_the_panel_in(self):
        @from_admin(PostAdmin, Post)
        class PostPanel(ModelPanel):
            pass

        panel = PostPanel(Post)
        assert panel.get_list_display() == ["title", "author", "status", "word_count"]
        assert panel.get_per_page() == 25

    def test_what_the_class_declares_wins(self):
        @from_admin(PostAdmin, Post)
        class PostPanel(ModelPanel):
            list_per_page = 10
            icon = "file-text"

        assert PostPanel(Post).get_per_page() == 10
        assert PostPanel.icon == "file-text"

    def test_the_model_can_come_from_the_class(self):
        @from_admin(PostAdmin)
        class PostPanel(ModelPanel):
            model = Post

        assert PostPanel(Post).get_list_search() == ["title", "body"]

    def test_the_adoption_record_names_the_project_class(self):
        @from_admin(PostAdmin, Post)
        class MyPostPanel(ModelPanel):
            pass

        adoption = django_admin.adoptions()[0]
        assert adoption.panel_class is MyPostPanel
        assert adoption.unmapped

    def test_an_unregistered_admin_class_without_a_model_is_a_clear_error(self):
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured, match="Pass it"):

            @from_admin(PostAdmin)
            class PostPanel(ModelPanel):
                pass


class TestAdoptAdminSite:
    def test_it_registers_a_panel_per_admin(self, admin_site):
        site = VantageSite(name="adopting")
        adopted = adopt_admin_site(site, admin_site=admin_site)

        assert [a.model for a in adopted] == [Author, Order, Post]
        assert site.is_registered(Post)
        assert site.get_panel(Post).get_per_page() == 25

    def test_an_existing_vantage_panel_wins(self, admin_site):
        class Hand(ModelPanel):
            list_per_page = 7

        site = VantageSite(name="adopting")
        site.register(Post, Hand)
        adopt_admin_site(site, admin_site=admin_site)

        assert isinstance(site.get_panel(Post), Hand)
        assert site.get_panel(Post).get_per_page() == 7

    def test_replace_is_explicit(self, admin_site):
        class Hand(ModelPanel):
            list_per_page = 7

        site = VantageSite(name="adopting")
        site.register(Post, Hand)
        adopt_admin_site(site, admin_site=admin_site, replace=True)
        assert site.get_panel(Post).get_per_page() == 25

    def test_registration_order_is_stable(self, admin_site):
        site = VantageSite(name="adopting")
        first = [a.label for a in adopt_admin_site(site, admin_site=admin_site)]
        second_site = VantageSite(name="adopting-again")
        second = [a.label for a in adopt_admin_site(second_site, admin_site=admin_site)]
        assert first == second == sorted(first)


class TestOptionalDependency:
    def test_contrib_admin_is_not_installed_in_this_project(self):
        """The premise of this module: everything above ran without the app."""
        assert admin_installed() is False

    def test_adopting_the_default_site_is_a_no_op_when_it_is_not_installed(self, caplog):
        site = VantageSite(name="adopting")
        with caplog.at_level("WARNING", logger="vantage.contrib.admin"):
            assert adopt_admin_site(site) == []
        assert "not in INSTALLED_APPS" in caplog.text

    def test_the_lazy_re_export_does_not_import_contrib_admin(self):
        from vantage import contrib

        assert contrib.adopt_admin_site is adopt_admin_site

    def test_importing_the_core_does_not_import_the_adapter(self):
        import subprocess
        import sys

        code = (
            "import django, os;"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tests.settings');"
            "django.setup();"
            "import sys;"
            "print('django.contrib.admin' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "False"


# ── the part that actually matters ──────────────────────────────────────────


@pytest.fixture
def adopted(admin_site, db):
    """A mounted Vantage site built entirely from the fixture ``admin.py``.

    The urlconf is a throwaway module rather than this one, so the site — and the
    permission hooks its panels carry — cannot outlive the test through the URL
    resolver's cache. Panels are unregistered on the way out for the same reason.
    """
    site = VantageSite(name="adopted")
    adoptions = adopt_admin_site(site, admin_site=admin_site)

    urlconf = types.ModuleType("tests.adopted_urls")
    urlconf.urlpatterns = [path("admin/", site.urls)]

    with override_settings(ROOT_URLCONF=urlconf):
        yield site

    for adoption in adoptions:
        site.unregister(adoption.model)


@pytest.fixture
def client_in(client, db):
    user = User.objects.create_superuser("adopter", "a@example.com", "x")
    client.force_login(user)
    return client


@pytest.fixture
def posts(db):
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    return [
        Post.objects.create(
            title=f"Adopted {i}",
            slug=f"adopted-{i}",
            body="one two three",
            author=author,
            status="published" if i % 2 else "draft",
        )
        for i in range(1, 4)
    ]


class TestThroughTheRealViews:
    def url(self):
        return reverse("vantage:testapp-post-list")

    def test_the_list_page_renders_the_admin_columns(self, adopted, client_in, posts):
        import re

        body = client_in.get(self.url()).content.decode()
        assert "Adopted 1" in body
        # ``word_count`` is a ModelAdmin method rendering through a Vantage cell.
        assert 'data-col-name="word_count"' in body
        assert re.search(r">\s*3\s*<", body)

    def test_the_admin_filters_are_offered_and_work(self, adopted, client_in, posts):
        response = client_in.get(self.url(), {"status": "published"})
        body = response.content.decode()
        assert "Adopted 1" in body
        assert "Adopted 2" not in body

    def test_search_uses_the_admin_search_fields(self, adopted, client_in, posts):
        body = client_in.get(self.url(), {"q": "Adopted 3"}).content.decode()
        assert "Adopted 3" in body
        assert "Adopted 1" not in body

    def test_the_page_size_is_the_admin_page_size(self, adopted, client_in, posts):
        response = client_in.get(self.url())
        assert response.context["paginator"].per_page == 25

    def test_the_admin_action_is_offered_and_runs(self, adopted, client_in, posts):
        body = client_in.get(self.url()).content.decode()
        assert "Feature selected posts" in body

        response = client_in.post(
            self.url(),
            {"action": "make_featured", "selected": [str(posts[0].pk)]},
            follow=True,
        )
        assert response.status_code == 200
        posts[0].refresh_from_db()
        assert posts[0].featured is True

    def test_has_delete_permission_is_honoured_for_a_superuser(
        self, adopted, client_in, posts
    ):
        """``contrib.admin`` denies everyone, superusers included. So does this."""
        url = reverse("vantage:testapp-post-delete", args=[posts[0].pk])
        assert client_in.get(url).status_code == 403
        assert Post.objects.filter(pk=posts[0].pk).exists()

    def test_the_delete_button_is_not_rendered(self, adopted, client_in, posts):
        url = reverse("vantage:testapp-post-detail", args=[posts[0].pk])
        body = client_in.get(url).content.decode()
        assert "Adopted 1" in body
        assert f"/{posts[0].pk}/delete/" not in body

    def test_the_other_pages_still_work(self, adopted, client_in, posts):
        assert client_in.get(reverse("vantage:dashboard")).status_code == 200
        detail = reverse("vantage:testapp-post-detail", args=[posts[0].pk])
        assert client_in.get(detail).status_code == 200
        assert client_in.get(reverse("vantage:testapp-post-add")).status_code == 200

    def test_a_delegated_queryset_scopes_the_list(self, adopted, client_in, db):
        author = Author.objects.create(name="Buyer", email="buyer@example.com")
        Order.objects.create(number="A-1", customer=author, total=1, status="paid")
        Order.objects.create(number="A-2", customer=author, total=1, status="refunded")

        body = client_in.get(reverse("vantage:testapp-order-list")).content.decode()
        assert "A-1" in body
        assert "A-2" not in body


class TestPermissionHookIsolation:
    """The hook must not outlive the panel that declared it."""

    def test_deletion_works_again_once_the_adopted_panel_is_gone(
        self, client_in, posts
    ):
        url = reverse("vantage:testapp-post-delete", args=[posts[0].pk])
        assert client_in.get(url).status_code == 200

    def test_the_default_site_is_unaffected(self):
        assert not hasattr(default_site.get_panel(Post), "has_delete_permission")


def test_the_adoption_dataclass_reports_its_label():
    adoption = Adoption(model=Post, admin_class=PostAdmin, panel_class=ModelPanel)
    assert adoption.label == "testapp.Post"
