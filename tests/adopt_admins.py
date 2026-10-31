"""A private ``AdminSite`` with a spread of admins, for the ``vantage_adopt`` tests.

A module rather than a fixture because the command takes a dotted path
(``--admin-site tests.adopt_admins.site``), which is also how a project with a custom
admin site would use it.
"""

from django import forms
from django.contrib import admin
from django.db import models

from tests.testapp.models import Author, Comment, Order, Post, Tag


class FancyMixin:
    """A third-party mixin the report does not know by name."""

    __module__ = "thirdparty.mixins"


class SimpleHistoryAdmin(admin.ModelAdmin):
    """Same name as django-simple-history's class; the report keys on the name."""


class CommentInline(admin.TabularInline):
    model = Comment


@admin.action(description="Feature")
def feature(modeladmin, request, queryset):
    return queryset.update(featured=True)


class PostAdmin(FancyMixin, admin.ModelAdmin):
    list_display = ("title", "status", "word_count")
    list_display_links = ("title",)
    list_filter = ("status", "featured")
    search_fields = ("title",)
    ordering = ("-created_at",)
    list_per_page = 20
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)
    raw_id_fields = ("author",)
    inlines = [CommentInline]
    actions = [feature]
    change_list_template = "x.html"
    formfield_overrides = {models.TextField: {"widget": forms.Textarea(attrs={"rows": 3})}}

    def word_count(self, obj):
        return len(obj.body.split())

    def get_queryset(self, request):
        return super().get_queryset(request)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return False


class OrderAdmin(admin.ModelAdmin):
    list_display = ("__str__", "status")
    fieldsets = (
        (None, {"fields": (("number", "customer"),)}),
        ("Money", {"fields": ("total", "status"), "classes": ("collapse",)}),
    )


class AuthorAdmin(SimpleHistoryAdmin):
    search_fields = ("name",)


class TagAdmin(admin.ModelAdmin):
    """Declares nothing."""


site = admin.AdminSite(name="adopt-fixture")
site.register(Post, PostAdmin)
site.register(Order, OrderAdmin)
site.register(Author, AuthorAdmin)
site.register(Tag, TagAdmin)
