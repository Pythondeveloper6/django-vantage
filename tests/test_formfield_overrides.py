"""``ModelPanel.formfield_overrides`` and ``ModelPanel.widgets``.

The promise: an override changes the control and nothing else. The field still goes
through ``db_field.formfield()`` (so validation is Django's), still gets the panel's
tenancy scoping and upload validator, and a sensitive field stays write-only.
"""

import pytest
from django import forms
from django.contrib.auth.models import User
from django.db import models
from django.urls import reverse

from tests.testapp.models import Author, Post, ZCredential
from vantage import site
from vantage.panels import ModelPanel
from vantage.widgets import WriteOnlyInput, build_form_class, formfield_callback_for


class Rich(forms.Textarea):
    """Stands in for a third-party editor widget."""


def panel_for(model, **attrs):
    return type("OverridePanel", (ModelPanel,), attrs)(model)


class TestClassOverrides:
    def test_a_field_class_override_replaces_the_inferred_widget(self):
        form = build_form_class(
            panel_for(Post, formfield_overrides={models.TextField: {"widget": Rich}}),
            ["title", "body"],
        )()
        assert isinstance(form.fields["body"].widget, Rich)
        assert not isinstance(form.fields["title"].widget, Rich)

    def test_matched_along_the_mro_like_contrib_admin(self):
        """``SlugField`` is a ``CharField``; an override keyed on the parent applies."""
        form = build_form_class(
            panel_for(
                Post,
                formfield_overrides={
                    models.CharField: {"widget": forms.TextInput(attrs={"data-x": "char"})}
                },
            ),
            ["slug"],
        )()
        assert form.fields["slug"].widget.attrs["data-x"] == "char"

    def test_the_most_specific_class_wins(self):
        form = build_form_class(
            panel_for(
                Post,
                formfield_overrides={
                    models.CharField: {"widget": forms.TextInput(attrs={"data-x": "char"})},
                    models.SlugField: {"widget": forms.TextInput(attrs={"data-x": "slug"})},
                },
            ),
            ["slug"],
        )()
        assert form.fields["slug"].widget.attrs["data-x"] == "slug"

    def test_non_widget_arguments_reach_the_form_field(self):
        form = build_form_class(
            panel_for(Post, formfield_overrides={models.TextField: {"help_text": "Markdown"}}),
            ["body"],
        )()
        assert form.fields["body"].help_text == "Markdown"

    def test_widget_instances_are_not_shared_between_forms(self):
        """A shared instance would share ``attrs``, and one form's edits would leak."""
        shared = forms.Textarea(attrs={"rows": 3})
        form_class = build_form_class(
            panel_for(Post, formfield_overrides={models.TextField: {"widget": shared}}), ["body"]
        )
        first, second = form_class(), form_class()
        first.fields["body"].widget.attrs["rows"] = 99
        assert second.fields["body"].widget.attrs["rows"] == 3
        assert shared.attrs["rows"] == 3

    def test_validation_is_still_djangos(self):
        form = build_form_class(
            panel_for(Post, formfield_overrides={models.CharField: {"widget": Rich}}), ["title"]
        )(data={"title": "x" * 500})
        assert not form.is_valid()
        assert "title" in form.errors


class TestPerFieldWidgets:
    def test_a_named_field_gets_its_widget(self):
        form = build_form_class(panel_for(Author, widgets={"bio": Rich}), ["name", "bio"])()
        assert isinstance(form.fields["bio"].widget, Rich)

    def test_a_per_field_widget_wins_over_a_class_override(self):
        form = build_form_class(
            panel_for(
                Author,
                formfield_overrides={models.TextField: {"widget": forms.HiddenInput}},
                widgets={"bio": Rich},
            ),
            ["bio"],
        )()
        assert isinstance(form.fields["bio"].widget, Rich)

    def test_a_sensitive_field_stays_write_only(self):
        """An override must never turn a masked secret back into a readable input."""
        form = build_form_class(
            panel_for(ZCredential, widgets={"api_token": forms.TextInput}), ["name", "api_token"]
        )()
        assert isinstance(form.fields["api_token"].widget, WriteOnlyInput)

    def test_no_overrides_shares_the_default_callback(self):
        from vantage.widgets import _formfield_callback

        assert formfield_callback_for(panel_for(Post)) is _formfield_callback


@pytest.mark.django_db
def test_the_change_page_renders_the_override(client, monkeypatch):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    author = Author.objects.create(name="Ada", email="ada@example.com", bio="Hello")
    panel = site.get_panel(Author)
    monkeypatch.setattr(
        panel, "widgets", {"bio": forms.Textarea(attrs={"data-editor": "markdown"})}
    )
    response = client.get(reverse("vantage:testapp-author-change", args=[author.pk]))
    assert response.status_code == 200
    assert b'data-editor="markdown"' in response.content


class TestChecks:
    def ids(self, panel):
        return sorted(e.id for e in panel.check())

    def test_valid_overrides_pass(self):
        assert (
            self.ids(
                panel_for(
                    Post,
                    formfield_overrides={models.TextField: {"widget": Rich}},
                    widgets={"title": forms.TextInput()},
                )
            )
            == []
        )

    @pytest.mark.parametrize(
        "value",
        [{"TextField": {"widget": Rich}}, {models.TextField: Rich}, [models.TextField], {str: {}}],
    )
    def test_a_malformed_overrides_dict_is_e120(self, value):
        assert self.ids(panel_for(Post, formfield_overrides=value)) == ["vantage.E120"]

    def test_an_unknown_field_name_is_e121(self):
        assert self.ids(panel_for(Post, widgets={"nope": Rich})) == ["vantage.E121"]

    def test_a_non_widget_value_is_e121(self):
        assert self.ids(panel_for(Post, widgets={"body": "Textarea"})) == ["vantage.E121"]
