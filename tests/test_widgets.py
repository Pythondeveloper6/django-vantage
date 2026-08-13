"""Widget inference decides what every form control in the panel looks like.

The classification table below is the contract: one row per :class:`FieldKind`, checked
against a real Django field. Adding a kind means adding a row.
"""

import uuid
from datetime import timedelta

import pytest
from django import forms
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models

from tests.testapp.models import Author, Category, Comment, Order, Post, Tag
from vantage.panels import ModelPanel
from vantage.widgets import (
    RELATION_SEARCH_THRESHOLD,
    FieldKind,
    build_form_class,
    classify,
    infer_form_fields,
    infer_widget,
    layout,
)


class WKinds(models.Model):
    """Every field kind the fixture models do not already cover."""

    class Colour(models.TextChoices):
        RED = "red", "Red"
        BLUE = "blue", "Blue"

    uid = models.UUIDField(default=uuid.uuid4)
    address = models.GenericIPAddressField()
    span = models.DurationField()
    at = models.TimeField()
    on = models.DateField()
    attachment = models.FileField(upload_to="files/", blank=True)
    blob = models.BinaryField(editable=True, blank=True)
    color = models.CharField(max_length=7)
    body_html = models.TextField()
    picked = models.CharField(max_length=10, choices=Colour.choices)
    ratio = models.FloatField()
    maybe = models.BooleanField(null=True)
    path = models.FilePathField(path="/tmp", blank=True)

    class Meta:
        app_label = "testapp"


class WOwner(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"


class WOwned(models.Model):
    """A reverse OneToOne and a reverse FK live on ``WOwner`` because of this model."""

    owner = models.OneToOneField(WOwner, on_delete=models.CASCADE, related_name="profile")
    tagged = models.ManyToManyField(Tag, blank=True)

    class Meta:
        app_label = "testapp"


class WNote(models.Model):
    """Carries a GenericForeignKey: many_to_one is True but related_model is None."""

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    target = GenericForeignKey("content_type", "object_id")
    body = models.TextField()

    class Meta:
        app_label = "testapp"


def field_of(model, name):
    return model._meta.get_field(name)


CLASSIFICATION = [
    (Post, "title", FieldKind.TEXT),
    (Post, "body", FieldKind.LONGTEXT),
    (WKinds, "body_html", FieldKind.RICHTEXT),
    (Post, "view_count", FieldKind.NUMBER),
    (WKinds, "ratio", FieldKind.NUMBER),
    (Order, "total", FieldKind.DECIMAL),
    (Post, "featured", FieldKind.BOOLEAN),
    (Post, "status", FieldKind.CHOICE),
    (WKinds, "path", FieldKind.CHOICE),
    (WKinds, "on", FieldKind.DATE),
    (Post, "created_at", FieldKind.DATETIME),
    (WKinds, "at", FieldKind.TIME),
    (WKinds, "span", FieldKind.DURATION),
    (Author, "email", FieldKind.EMAIL),
    (Author, "website", FieldKind.URL),
    (Post, "slug", FieldKind.SLUG),
    (Post, "metadata", FieldKind.JSON),
    (WKinds, "attachment", FieldKind.FILE),
    (Author, "avatar", FieldKind.IMAGE),
    (WKinds, "color", FieldKind.COLOR),
    (Post, "author", FieldKind.RELATION_ONE),
    (Post, "tags", FieldKind.RELATION_MANY),
    (WKinds, "uid", FieldKind.UUID),
    (WKinds, "address", FieldKind.IP),
    (WKinds, "blob", FieldKind.BINARY),
]


class TestClassify:
    @pytest.mark.parametrize(("model", "name", "expected"), CLASSIFICATION)
    def test_every_kind(self, model, name, expected):
        assert classify(field_of(model, name)) is expected

    def test_every_kind_is_covered_by_the_table(self):
        """A new kind without a row here is a kind nobody has proved renders."""
        covered = {expected for _, _, expected in CLASSIFICATION}
        assert covered == set(FieldKind) - {FieldKind.UNKNOWN}

    def test_an_unrecognised_field_is_unknown_not_a_crash(self):
        class Odd(models.Field):
            pass

        assert classify(Odd(name="odd")) is FieldKind.UNKNOWN

    def test_datetime_is_not_classified_as_a_date(self):
        """DateTimeField subclasses DateField, so the isinstance order is load-bearing."""
        assert classify(field_of(Post, "created_at")) is FieldKind.DATETIME

    def test_image_is_not_classified_as_a_file(self):
        assert classify(field_of(Author, "avatar")) is FieldKind.IMAGE

    def test_choices_outrank_the_storage_type(self):
        assert classify(field_of(Post, "status")) is FieldKind.CHOICE

    def test_a_generic_foreign_key_is_a_single_relation(self):
        """many_to_one is True while related_model is None — the classic crash."""
        gfk = next(f for f in WNote._meta.private_fields if isinstance(f, GenericForeignKey))
        assert gfk.many_to_one is True
        assert gfk.related_model is None
        assert classify(gfk) is FieldKind.RELATION_ONE

    def test_a_reverse_foreign_key_is_a_multi_relation(self):
        """ManyToOneRel.one_to_many is True: the flags describe traversal, not the name."""
        rel = Post._meta.get_field("comments")
        assert rel.one_to_many is True
        assert classify(rel) is FieldKind.RELATION_MANY

    def test_a_reverse_one_to_one_is_a_single_relation(self):
        rel = WOwner._meta.get_field("profile")
        assert rel.concrete is False
        assert classify(rel) is FieldKind.RELATION_ONE

    def test_a_reverse_many_to_many_is_a_multi_relation(self):
        assert classify(Tag._meta.get_field("wowned")) is FieldKind.RELATION_MANY

    def test_a_generic_relation_is_a_multi_relation(self):
        class WArticle(models.Model):
            notes = GenericRelation(WNote)

            class Meta:
                app_label = "testapp"

        assert classify(WArticle._meta.get_field("notes")) is FieldKind.RELATION_MANY

    def test_a_long_char_field_named_color_is_still_prose(self):
        class WPalette(models.Model):
            color_notes = models.CharField(max_length=500)

            class Meta:
                app_label = "testapp"

        assert classify(field_of(WPalette, "color_notes")) is FieldKind.TEXT

    def test_a_third_party_field_class_is_recognised_by_name(self):
        class RichTextField(models.TextField):
            pass

        class ColorField(models.CharField):
            pass

        class WFancy(models.Model):
            intro = RichTextField()
            brand = ColorField(max_length=7)

            class Meta:
                app_label = "testapp"

        assert classify(field_of(WFancy, "intro")) is FieldKind.RICHTEXT
        assert classify(field_of(WFancy, "brand")) is FieldKind.COLOR


class TestWidgetAttributes:
    def test_email_gets_a_typed_input_not_a_text_box(self):
        """Django's own default here is a TextInput, which is why this module exists."""
        spec = infer_widget(field_of(Author, "email"))
        assert spec.input_type == "email"
        assert spec.placeholder == "name@example.com"

    def test_url_gets_a_typed_input(self):
        assert infer_widget(field_of(Author, "website")).input_type == "url"

    def test_numbers_get_a_number_input(self):
        assert infer_widget(field_of(Post, "view_count")).input_type == "number"

    def test_decimal_step_comes_from_decimal_places(self):
        spec = infer_widget(field_of(Order, "total"))
        assert spec.attrs["step"] == "0.01"

    def test_decimal_bounds_come_from_max_digits(self):
        spec = infer_widget(field_of(Order, "total"))
        assert spec.attrs["max"] == "99999999.99"
        assert spec.attrs["min"] == "-99999999.99"

    def test_a_scaleless_decimal_steps_by_one(self):
        class WPriced(models.Model):
            whole = models.DecimalField(max_digits=4, decimal_places=0)

            class Meta:
                app_label = "testapp"

        assert infer_widget(field_of(WPriced, "whole")).attrs["step"] == "1"

    def test_a_float_accepts_any_step(self):
        assert infer_widget(field_of(WKinds, "ratio")).attrs["step"] == "any"

    def test_a_positive_integer_cannot_go_below_zero(self):
        assert infer_widget(field_of(Post, "view_count")).attrs["min"] == "0"

    def test_explicit_validators_set_the_bounds(self):
        class WRated(models.Model):
            score = models.IntegerField(
                validators=[MinValueValidator(1), MaxValueValidator(5)]
            )

            class Meta:
                app_label = "testapp"

        attrs = infer_widget(field_of(WRated, "score")).attrs
        assert (attrs["min"], attrs["max"]) == ("1", "5")

    def test_max_length_becomes_maxlength(self):
        assert infer_widget(field_of(Post, "title")).attrs["maxlength"] == "200"

    def test_a_file_path_length_is_not_a_maxlength(self):
        """FileField.max_length bounds the stored path, not what the user uploads."""
        assert "maxlength" not in infer_widget(field_of(Author, "avatar")).attrs

    def test_images_accept_images(self):
        assert infer_widget(field_of(Author, "avatar")).attrs["accept"] == "image/*"

    def test_a_plain_file_field_accepts_anything(self):
        assert "accept" not in infer_widget(field_of(WKinds, "attachment")).attrs

    def test_an_extension_validator_narrows_accept(self):
        class WUpload(models.Model):
            doc = models.FileField(
                upload_to="d/", validators=[FileExtensionValidator(["pdf", "docx"])]
            )

            class Meta:
                app_label = "testapp"

        assert infer_widget(field_of(WUpload, "doc")).attrs["accept"] == ".pdf,.docx"

    def test_long_text_gets_rows(self):
        assert infer_widget(field_of(Post, "body")).attrs["rows"]

    def test_a_slug_gets_a_pattern(self):
        assert infer_widget(field_of(Post, "slug")).attrs["pattern"] == "[-a-zA-Z0-9_]+"

    def test_a_duration_explains_its_own_format(self):
        """Nobody guesses Django's duration syntax without being told."""
        assert "12:30:00" in infer_widget(field_of(WKinds, "span")).help_text

    def test_declared_help_text_is_never_replaced(self):
        field = models.DurationField(help_text="How long the job ran.")
        field.set_attributes_from_name("span")
        assert infer_widget(field).help_text == "How long the job ran."

    def test_a_blank_field_is_not_required(self):
        assert infer_widget(field_of(Post, "summary")).required is False
        assert infer_widget(field_of(Post, "title")).required is True

    def test_choices_travel_with_the_spec(self):
        spec = infer_widget(field_of(Post, "status"))
        assert ("draft", "Draft") in spec.choices

    def test_a_datetime_carries_the_html5_type(self):
        assert infer_widget(field_of(Post, "published_at")).input_type == "datetime-local"

    def test_a_nullable_boolean_is_not_a_checkbox(self):
        spec = infer_widget(field_of(WKinds, "maybe"))
        assert spec.nullable is True
        assert spec.input_type is None

    def test_html_attrs_include_the_kind_for_the_front_end(self):
        attrs = infer_widget(field_of(Author, "email")).html_attrs()
        assert attrs["data-vantage-kind"] == "email"
        assert attrs["type"] == "email"


class TestRelationWidgets:
    def test_a_relation_knows_its_target(self):
        assert infer_widget(field_of(Post, "author")).related_model is Author

    def test_many_to_many_is_multiple(self):
        assert infer_widget(field_of(Post, "tags")).multiple is True

    def test_unknown_cardinality_means_search(self):
        """A select over an unbounded table is the failure mode; a search box is not."""
        assert infer_widget(field_of(Post, "author")).needs_search is True

    def test_a_small_relation_renders_inline(self):
        spec = infer_widget(field_of(Post, "author"), related_count=3)
        assert spec.needs_search is False

    def test_a_large_relation_needs_search(self):
        spec = infer_widget(
            field_of(Post, "author"), related_count=RELATION_SEARCH_THRESHOLD + 1
        )
        assert spec.needs_search is True

    def test_a_generic_foreign_key_does_not_crash_on_its_missing_target(self):
        gfk = next(f for f in WNote._meta.private_fields if isinstance(f, GenericForeignKey))
        spec = infer_widget(gfk)
        assert spec.kind is FieldKind.RELATION_ONE
        assert spec.related_model is None


class TestBuiltWidgets:
    def test_a_choice_field_becomes_a_select_not_a_text_input(self):
        widget = infer_widget(field_of(Post, "status")).build_widget()
        assert isinstance(widget, forms.Select)

    def test_a_multi_relation_becomes_a_multi_select(self):
        widget = infer_widget(field_of(Post, "tags")).build_widget()
        assert isinstance(widget, forms.SelectMultiple)

    def test_a_nullable_boolean_becomes_a_tri_state_select(self):
        widget = infer_widget(field_of(WKinds, "maybe")).build_widget()
        assert isinstance(widget, forms.NullBooleanSelect)

    def test_a_boolean_becomes_a_checkbox(self):
        widget = infer_widget(field_of(Post, "featured")).build_widget()
        assert isinstance(widget, forms.CheckboxInput)

    def test_a_date_widget_formats_for_the_native_picker(self):
        """The browser picker stays blank unless the value is in ISO shape."""
        widget = infer_widget(field_of(WKinds, "on")).build_widget()
        assert widget.format == "%Y-%m-%d"

    def test_a_textarea_does_not_emit_a_bogus_type_attribute(self):
        widget = infer_widget(field_of(Post, "body")).build_widget()
        assert "type" not in widget.attrs

    def test_the_type_survives_into_the_rendered_html(self):
        widget = infer_widget(field_of(Author, "email")).build_widget()
        assert 'type="email"' in widget.render("email", "a@b.com")


class TestFormFields:
    def test_the_obvious_fields_are_offered(self):
        fields = infer_form_fields(Post)
        for name in ("title", "slug", "body", "status", "author", "tags", "featured"):
            assert name in fields

    def test_auto_now_add_is_never_editable(self):
        assert "created_at" not in infer_form_fields(Post)

    def test_auto_now_is_never_editable(self):
        assert "updated_at" not in infer_form_fields(Post)

    def test_the_auto_primary_key_is_never_offered(self):
        assert "id" not in infer_form_fields(Post)

    def test_sensitive_fields_never_appear_on_a_form(self):
        class WCredential(models.Model):
            username = models.CharField(max_length=50)
            password = models.CharField(max_length=128)
            api_key = models.CharField(max_length=64)
            reset_token = models.CharField(max_length=64)
            secret = models.CharField(max_length=64)

            class Meta:
                app_label = "testapp"

        assert infer_form_fields(WCredential) == ["username"]

    def test_a_natural_primary_key_is_still_editable(self):
        class WCountry(models.Model):
            code = models.CharField(max_length=2, primary_key=True)
            name = models.CharField(max_length=80)

            class Meta:
                app_label = "testapp"

        assert infer_form_fields(WCountry) == ["name", "code"]

    def test_a_generated_primary_key_is_not_editable(self):
        class WToken(models.Model):
            key = models.UUIDField(primary_key=True, default=uuid.uuid4)
            label = models.CharField(max_length=20)

            class Meta:
                app_label = "testapp"

        assert infer_form_fields(WToken) == ["label"]

    def test_a_many_to_many_through_an_explicit_model_is_left_to_an_inline(self):
        """`.set()` raises on a through model with extra fields — the form would fail on save."""

        class WMembership(models.Model):
            band = models.ForeignKey("testapp.WBand", on_delete=models.CASCADE)
            player = models.ForeignKey(WOwner, on_delete=models.CASCADE)
            role = models.CharField(max_length=30)

            class Meta:
                app_label = "testapp"

        class WBand(models.Model):
            name = models.CharField(max_length=50)
            players = models.ManyToManyField(WOwner, through=WMembership)

            class Meta:
                app_label = "testapp"

        assert infer_form_fields(WBand) == ["name"]

    def test_the_name_field_leads(self):
        assert infer_form_fields(Author)[0] == "name"

    def test_relations_follow_the_content_and_flags_follow_relations(self):
        fields = infer_form_fields(Post)
        assert fields.index("title") < fields.index("author")
        assert fields.index("author") < fields.index("featured")

    def test_metadata_sorts_last(self):
        fields = infer_form_fields(Post)
        assert fields[-1] == "metadata"

    def test_every_inferred_field_is_actually_usable_in_a_form(self):
        """The list has to be one modelform_factory would accept, for every model."""
        for model in (Post, Author, Order, Comment, Tag, Category, WKinds, WOwned):
            form_class = build_form_class(model)
            assert set(form_class.base_fields) == set(infer_form_fields(model)), model.__name__


class TestLayout:
    def test_sections_come_back_in_reading_order(self):
        keys = [section.key for section in layout(Post)]
        assert keys == ["main", "relations", "flags", "metadata"]

    def test_fields_land_in_the_right_section(self):
        sections = {section.key: section.fields for section in layout(Post)}
        assert "title" in sections["main"]
        assert "author" in sections["relations"]
        assert "tags" in sections["relations"]
        assert "featured" in sections["flags"]
        assert "metadata" in sections["metadata"]

    def test_empty_sections_are_dropped(self):
        assert [section.key for section in layout(Tag)] == ["main"]

    def test_an_explicit_field_list_is_respected(self):
        sections = layout(Post, fields=["title", "author"])
        assert [section.fields for section in sections] == [("title",), ("author",)]

    def test_an_unresolvable_field_stays_in_the_main_section(self):
        sections = {section.key: section.fields for section in layout(Post, fields=["captcha"])}
        assert sections["main"] == ("captcha",)

    def test_the_layout_covers_exactly_the_inferred_fields(self):
        for model in (Post, Author, Order, WKinds):
            flat = [name for section in layout(model) for name in section.fields]
            assert flat == infer_form_fields(model), model.__name__


class TestBuildFormClass:
    def test_it_is_a_real_model_form(self):
        assert issubclass(build_form_class(Post), forms.ModelForm)

    def test_the_inferred_widgets_are_wired_in(self):
        form = build_form_class(Post)()
        assert isinstance(form.fields["status"].widget, forms.Select)
        assert isinstance(form.fields["body"].widget, forms.Textarea)
        assert form.fields["rating"].widget.attrs["step"] == "0.01"
        assert form.fields["title"].widget.attrs["maxlength"] == "200"

    def test_an_email_field_renders_as_an_email_input(self):
        form = build_form_class(Author)()
        assert 'type="email"' in str(form["email"])

    def test_an_explicit_field_list_wins(self):
        form_class = build_form_class(Post, fields=["title", "body"])
        assert list(form_class.base_fields) == ["title", "body"]

    def test_a_panel_supplies_the_model(self):
        class PostPanel(ModelPanel):
            pass

        form_class = build_form_class(PostPanel(Post))
        assert form_class._meta.model is Post

    def test_a_panel_field_list_wins_but_widgets_stay_inferred(self):
        class PostPanel(ModelPanel):
            fields = ["title", "status"]

        form_class = build_form_class(PostPanel(Post))
        assert list(form_class.base_fields) == ["title", "status"]
        assert isinstance(form_class.base_fields["status"].widget, forms.Select)

    def test_a_panel_exclude_is_honoured(self):
        class PostPanel(ModelPanel):
            exclude = ["metadata"]

        assert "metadata" not in build_form_class(PostPanel(Post)).base_fields

    def test_the_form_validates_and_saves_a_real_instance(self, db):
        author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
        tag = Tag.objects.create(name="analytical-engine")

        form = build_form_class(Post)(
            data={
                "title": "On the Engine",
                "slug": "on-the-engine",
                "body": "Notes.",
                "summary": "",
                "status": Post.Status.DRAFT,
                "author": author.pk,
                "tags": [tag.pk],
                "view_count": 0,
                "rating": "4.25",
                "metadata": "{}",
                "published_at": "2026-08-13T09:30",
            }
        )
        assert form.is_valid(), form.errors
        post = form.save()

        assert post.pk
        assert post.author == author
        assert list(post.tags.all()) == [tag]
        assert str(post.rating) == "4.25"
        assert post.published_at.hour == 9
        assert post.created_at is not None  # excluded from the form, still populated

    def test_the_form_reports_model_validation_errors(self, db):
        Author.objects.create(name="Ada", email="ada@example.com")
        form = build_form_class(Author)(data={"name": "Grace", "email": "ada@example.com"})
        assert not form.is_valid()
        assert "email" in form.errors

    def test_a_duration_round_trips_through_the_form(self, db):
        form_class = build_form_class(WKinds)
        form = form_class(
            data={
                "uid": str(uuid.uuid4()),
                "address": "192.0.2.1",
                "span": "01:30:00",
                "at": "09:00",
                "on": "2026-08-13",
                "blob": "",
                "color": "#ff0000",
                "body_html": "<p>hi</p>",
                "picked": "red",
                "ratio": "1.5",
                "maybe": "unknown",
                "path": "",
            }
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["span"] == timedelta(hours=1, minutes=30)
        assert form.cleaned_data["maybe"] is None
