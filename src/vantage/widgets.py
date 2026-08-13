"""Field → widget inference, and the form class built from it.

Django's ``Field.formfield()`` defaults date from 2005: an ``EmailField`` renders as a
plain ``TextInput``, a ``DecimalField`` gets no ``step``, an ``ImageField`` accepts
``.exe``. Browsers have shipped typed inputs, constraint attributes and native pickers
for a decade. This module is the one part of the form stack Vantage does not inherit:
we keep Django's *machinery* — validation, ``_post_clean``, ``construct_instance``,
``BoundField`` — and replace only the field→widget mapping, which is exactly the split
recommended in the platform decisions (B5).

Inference is a two-step: :func:`classify` reduces a field to a :class:`FieldKind` — a
rendering category, not a Python type — and :func:`infer_widget` turns that kind plus
the field's own constraints into a :class:`WidgetSpec`. Splitting them means a custom
field class from a third-party package only has to answer "which kind?" to get the full
treatment, and it means the front end has a small closed vocabulary to write CSS and JS
against instead of a hundred Django field classes.

Every function here is pure: field or model in, configuration out. No database access,
no request, no settings. Relation cardinality — the one fact that decides between a
plain ``<select>`` and an async search box, and the one fact that requires a query — is
passed in by the caller rather than looked up here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from django import forms
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.forms.models import modelform_factory

from vantage.inference import DISPLAY_NAMES, SENSITIVE, TRAILING_NAMES
from vantage.introspection import (
    editable_fields,
    is_auto_created_through,
    m2m_fields,
    resolve_path,
    through_model,
)

__all__ = [
    "FieldKind",
    "FormSection",
    "WidgetSpec",
    "build_form_class",
    "classify",
    "infer_form_fields",
    "infer_widget",
    "layout",
]


class FieldKind(StrEnum):
    """What a field *looks like* to a user, as opposed to what it is to the ORM.

    A ``StrEnum`` because these values travel all the way to the template and into
    ``data-`` attributes; comparing against a plain string has to keep working there.
    """

    TEXT = "text"
    LONGTEXT = "longtext"
    RICHTEXT = "richtext"
    NUMBER = "number"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    CHOICE = "choice"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    DURATION = "duration"
    EMAIL = "email"
    URL = "url"
    SLUG = "slug"
    JSON = "json"
    FILE = "file"
    IMAGE = "image"
    COLOR = "color"
    RELATION_ONE = "relation_one"
    RELATION_MANY = "relation_many"
    UUID = "uuid"
    IP = "ip"
    BINARY = "binary"
    UNKNOWN = "unknown"


#: Above this many related rows a ``<select>`` stops being usable — and, long before
#: that, stops being cheap to render. Callers that know the count say so; callers that
#: do not get the safe answer (see :func:`infer_widget`).
RELATION_SEARCH_THRESHOLD = 50

#: Textarea heights per kind. Prose gets more room than a JSON blob nobody reads.
TEXTAREA_ROWS = 6
RICHTEXT_ROWS = 12
JSON_ROWS = 8

#: Field *class* names that mean rich text. Every rich-text package invents its own
#: field class, and none of them share a base we could import without depending on it,
#: so the class name is the only portable signal.
RICHTEXT_CLASS_HINTS = (
    "richtext",
    "html",
    "ckeditor",
    "tinymce",
    "quill",
    "prosemirror",
    "wysiwyg",
    "markdown",
)

#: Names and class names that mean "this string is a colour".
COLOR_NAMES = re.compile(r"(^|_)(color|colour)($|_)", re.IGNORECASE)
COLOR_MAX_LENGTH = 32

#: Sample values that show the expected shape without pretending to be data.
PLACEHOLDERS: dict[FieldKind, str] = {
    FieldKind.EMAIL: "name@example.com",
    FieldKind.URL: "https://example.com",
    FieldKind.DURATION: "HH:MM:SS",
    FieldKind.IP: "192.0.2.1",
    FieldKind.UUID: "00000000-0000-0000-0000-000000000000",
    FieldKind.JSON: "{}",
}

#: The HTML ``type`` for kinds rendered as ``<input>``. Kinds absent from this map are
#: not inputs at all — ``<select>`` for the choice and relation kinds, ``<textarea>``
#: for the long ones — and carry ``input_type = None`` so nothing emits ``type="select"``.
INPUT_TYPES: dict[FieldKind, str] = {
    FieldKind.TEXT: "text",
    FieldKind.NUMBER: "number",
    FieldKind.DECIMAL: "number",
    FieldKind.BOOLEAN: "checkbox",
    FieldKind.DATE: "date",
    FieldKind.DATETIME: "datetime-local",
    FieldKind.TIME: "time",
    FieldKind.DURATION: "text",
    FieldKind.EMAIL: "email",
    FieldKind.URL: "url",
    FieldKind.SLUG: "text",
    FieldKind.FILE: "file",
    FieldKind.IMAGE: "file",
    FieldKind.COLOR: "color",
    FieldKind.UUID: "text",
    FieldKind.IP: "text",
    FieldKind.UNKNOWN: "text",
}

#: Kinds where ``max_length`` is a limit on what the *user types*. Deliberately not
#: ``FILE``/``IMAGE``: their ``max_length`` bounds the stored path, not the upload.
LENGTH_LIMITED = frozenset(
    {
        FieldKind.TEXT,
        FieldKind.LONGTEXT,
        FieldKind.RICHTEXT,
        FieldKind.EMAIL,
        FieldKind.URL,
        FieldKind.SLUG,
        FieldKind.COLOR,
        FieldKind.IP,
    }
)

#: ``datetime-local`` and friends only populate from a value in exactly this shape;
#: the locale-dependent default formats produce a blank picker on an existing record.
HTML5_FORMATS: dict[FieldKind, str] = {
    FieldKind.DATE: "%Y-%m-%d",
    FieldKind.DATETIME: "%Y-%m-%dT%H:%M",
    FieldKind.TIME: "%H:%M",
}

#: Django's duration parsing accepts a format no user would guess unaided.
DURATION_HELP = "Days, hours, minutes and seconds, e.g. '3 12:30:00' or '01:30:00'."


# ── classification ──────────────────────────────────────────────────────────


def classify(field: Any) -> FieldKind:
    """Reduce any model field — forward, reverse, generic — to a rendering kind.

    Relations are settled first and by *flag*, never by class name, because the flags
    describe traversal direction: ``ManyToOneRel`` (a reverse FK) has ``one_to_many``
    set, not ``many_to_one``. A ``GenericForeignKey`` is checked before anything else
    since it reports ``many_to_one`` with ``related_model is None``, and every branch
    below that would otherwise be entitled to assume a target model exists.
    """
    if isinstance(field, GenericForeignKey):
        return FieldKind.RELATION_ONE

    if getattr(field, "is_relation", False):
        if field.many_to_many or field.one_to_many:
            return FieldKind.RELATION_MANY
        return FieldKind.RELATION_ONE

    if getattr(field, "choices", None):
        # An explicit choice list outranks the storage type: a CharField with choices
        # is a picker, and rendering it as free text loses the whole constraint.
        return FieldKind.CHOICE
    if isinstance(field, models.FilePathField):
        # Its choices are enumerated from disk at form-build time, not declared.
        return FieldKind.CHOICE

    # Order matters throughout: ImageField subclasses FileField, DateTimeField
    # subclasses DateField, and Email/URL/Slug all subclass CharField.
    if isinstance(field, models.ImageField):
        return FieldKind.IMAGE
    if isinstance(field, models.FileField):
        return FieldKind.FILE
    if isinstance(field, models.UUIDField):
        return FieldKind.UUID
    if isinstance(field, models.GenericIPAddressField):
        return FieldKind.IP
    if isinstance(field, models.DurationField):
        return FieldKind.DURATION
    if isinstance(field, models.DateTimeField):
        return FieldKind.DATETIME
    if isinstance(field, models.DateField):
        return FieldKind.DATE
    if isinstance(field, models.TimeField):
        return FieldKind.TIME
    if isinstance(field, models.BooleanField):
        return FieldKind.BOOLEAN
    if isinstance(field, models.DecimalField):
        return FieldKind.DECIMAL
    if isinstance(field, models.IntegerField | models.FloatField):
        return FieldKind.NUMBER
    if isinstance(field, models.JSONField):
        return FieldKind.JSON
    if isinstance(field, models.BinaryField):
        return FieldKind.BINARY
    if isinstance(field, models.EmailField):
        return FieldKind.EMAIL
    if isinstance(field, models.URLField):
        return FieldKind.URL
    if isinstance(field, models.SlugField):
        return FieldKind.SLUG
    if isinstance(field, models.TextField):
        return FieldKind.RICHTEXT if _is_richtext(field) else FieldKind.LONGTEXT
    if isinstance(field, models.CharField):
        return FieldKind.COLOR if _is_color(field) else FieldKind.TEXT
    return FieldKind.UNKNOWN


def _is_richtext(field: models.Field) -> bool:
    class_name = type(field).__name__.lower()
    return any(hint in class_name for hint in RICHTEXT_CLASS_HINTS) or field.name.endswith("_html")


def _is_color(field: models.Field) -> bool:
    """A colour is a short string named like one, or stored in a field class named so.

    The length bound matters: a ``CharField(max_length=500)`` called ``color_notes`` is
    prose about colour, and a native colour picker would truncate it to seven characters.
    """
    if "color" in type(field).__name__.lower() or "colour" in type(field).__name__.lower():
        return True
    return bool(COLOR_NAMES.search(field.name)) and (field.max_length or 0) <= COLOR_MAX_LENGTH


# ── the spec ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WidgetSpec:
    """Everything the renderer needs to draw one field, and nothing about how.

    Deliberately not a ``forms.Widget``: the spec is data, so it can be asserted in a
    table test, serialised to a front end, or handed to a template that renders the
    control itself. :meth:`build_widget` is the adapter back to Django, used when the
    form is a real ``ModelForm``.
    """

    kind: FieldKind
    #: The HTML ``type`` attribute, or ``None`` for controls that are not ``<input>``.
    input_type: str | None = None
    #: Constraint attributes: ``min``, ``max``, ``step``, ``maxlength``, ``rows``,
    #: ``accept``, ``pattern``. Values are strings because that is what HTML takes.
    attrs: dict[str, str] = dataclass_field(default_factory=dict)
    choices: tuple[tuple[Any, Any], ...] | None = None
    #: True for a control that accepts several values at once (forward or reverse M2M).
    multiple: bool = False
    #: True when the options must come from an async search endpoint rather than being
    #: rendered inline.
    needs_search: bool = False
    related_model: type[models.Model] | None = None
    placeholder: str | None = None
    help_text: str = ""
    required: bool = True
    #: True for a boolean that also has a third, unset state.
    nullable: bool = False
    label: str = ""

    def html_attrs(self) -> dict[str, str]:
        """The attributes to put on the rendered control.

        ``type`` is included here rather than being applied separately because
        ``forms.Input.__init__`` pops it out of ``attrs`` and uses it as the input type —
        the documented way to override it — so one dict covers both paths.
        """
        attrs: dict[str, str] = {"data-vantage-kind": str(self.kind)}
        if self.input_type:
            attrs["type"] = self.input_type
        if self.placeholder:
            attrs["placeholder"] = self.placeholder
        if self.needs_search:
            attrs["data-vantage-search"] = "1"
        attrs.update(self.attrs)
        return attrs

    def build_widget(self) -> forms.Widget:
        """The Django widget realising this spec."""
        attrs = self.html_attrs()
        kind = self.kind

        if kind in HTML5_FORMATS:
            widget_class = {
                FieldKind.DATE: forms.DateInput,
                FieldKind.DATETIME: forms.DateTimeInput,
                FieldKind.TIME: forms.TimeInput,
            }[kind]
            return widget_class(attrs=attrs, format=HTML5_FORMATS[kind])
        if kind is FieldKind.BOOLEAN:
            # A nullable boolean has three states; a checkbox can only report two, and
            # would silently rewrite every unset value to False on first save.
            if self.nullable:
                return forms.NullBooleanSelect(attrs=attrs)
            return forms.CheckboxInput(attrs=attrs)
        if kind in (FieldKind.CHOICE, FieldKind.RELATION_ONE):
            return forms.Select(attrs=attrs)
        if kind is FieldKind.RELATION_MANY:
            return forms.SelectMultiple(attrs=attrs)
        if kind in (FieldKind.FILE, FieldKind.IMAGE):
            return forms.ClearableFileInput(attrs=attrs)
        if kind in (FieldKind.LONGTEXT, FieldKind.RICHTEXT, FieldKind.JSON, FieldKind.BINARY):
            return forms.Textarea(attrs=attrs)
        if kind in (FieldKind.NUMBER, FieldKind.DECIMAL):
            return forms.NumberInput(attrs=attrs)
        if kind is FieldKind.EMAIL:
            return forms.EmailInput(attrs=attrs)
        if kind is FieldKind.URL:
            return forms.URLInput(attrs=attrs)
        return forms.TextInput(attrs=attrs)


def infer_widget(field: Any, *, related_count: int | None = None) -> WidgetSpec:
    """Describe how to render one field.

    ``related_count`` is the caller's cardinality hint for a relation. It is a parameter
    rather than a lookup because counting is a query and this module does not touch the
    database. When it is omitted the answer is "use search": a search box over five rows
    is a mild inconvenience, while a ``<select>`` over fifty thousand rows is an outage.
    """
    kind = classify(field)
    attrs: dict[str, str] = {}

    max_length = getattr(field, "max_length", None)
    if max_length and kind in LENGTH_LIMITED:
        attrs["maxlength"] = str(max_length)
    if kind in (FieldKind.NUMBER, FieldKind.DECIMAL):
        attrs.update(_numeric_attrs(field))
    if kind is FieldKind.SLUG:
        attrs["pattern"] = "[-a-zA-Z0-9_]+"
    if kind is FieldKind.LONGTEXT:
        attrs["rows"] = str(TEXTAREA_ROWS)
    if kind is FieldKind.RICHTEXT:
        attrs["rows"] = str(RICHTEXT_ROWS)
    if kind is FieldKind.JSON:
        attrs["rows"] = str(JSON_ROWS)
    if kind in (FieldKind.FILE, FieldKind.IMAGE):
        accept = _accept(field, kind)
        if accept:
            attrs["accept"] = accept

    choices = getattr(field, "choices", None)
    related = getattr(field, "related_model", None)
    is_relation = kind in (FieldKind.RELATION_ONE, FieldKind.RELATION_MANY)
    needs_search = is_relation and (
        related_count is None or related_count > RELATION_SEARCH_THRESHOLD
    )

    help_text = str(getattr(field, "help_text", "") or "")
    if kind is FieldKind.DURATION and not help_text:
        help_text = DURATION_HELP

    # A tri-state boolean is a <select>, not a checkbox, so it has no input type.
    nullable = kind is FieldKind.BOOLEAN and bool(getattr(field, "null", False))

    return WidgetSpec(
        kind=kind,
        input_type=None if nullable else INPUT_TYPES.get(kind),
        attrs=attrs,
        choices=_freeze_choices(choices) if choices and kind is FieldKind.CHOICE else None,
        multiple=kind is FieldKind.RELATION_MANY,
        needs_search=needs_search,
        related_model=related,
        placeholder=PLACEHOLDERS.get(kind),
        help_text=help_text,
        required=not getattr(field, "blank", False),
        nullable=nullable,
        label=_label(field),
    )


def _label(field: Any) -> str:
    verbose = getattr(field, "verbose_name", None)
    if verbose:
        return str(verbose).capitalize()
    return str(getattr(field, "name", "")).replace("_", " ").capitalize()


def _freeze_choices(choices: Any) -> tuple[tuple[Any, Any], ...]:
    """Make the choice list hashable-ish, keeping optgroups intact."""
    return tuple(
        (value, tuple(label) if isinstance(label, list) else label) for value, label in choices
    )


def _numeric_attrs(field: models.Field) -> dict[str, str]:
    """``step``, ``min`` and ``max`` for a numeric input.

    Without a ``step`` the browser rejects "12.50" in a ``number`` input as invalid, so
    a ``DecimalField`` has to state its scale. The bounds come from ``max_digits``
    because that is the constraint the database will actually enforce; explicit
    validators are applied afterwards and win, including the backend-derived integer
    range that ``PositiveIntegerField`` carries.
    """
    attrs: dict[str, str] = {}
    if isinstance(field, models.DecimalField):
        places = field.decimal_places or 0
        attrs["step"] = "1" if places == 0 else f"0.{'0' * (places - 1)}1"
        if field.max_digits:
            bound = Decimal(10) ** (field.max_digits - places) - Decimal(10) ** -places
            attrs["max"] = str(bound)
            attrs["min"] = str(-bound)
    elif isinstance(field, models.FloatField):
        attrs["step"] = "any"

    for validator in field.validators:
        limit = getattr(validator, "limit_value", None)
        if callable(limit):
            continue  # a deferred limit is not knowable at render time
        if isinstance(validator, MinValueValidator):
            attrs["min"] = str(limit)
        elif isinstance(validator, MaxValueValidator):
            attrs["max"] = str(limit)
    return attrs


def _accept(field: models.Field, kind: FieldKind) -> str | None:
    """The ``accept`` attribute, from the field's own extension validator if it has one.

    An explicit ``FileExtensionValidator`` is the developer's statement of intent and is
    more specific than ``image/*``, so it wins even on an ``ImageField``.
    """
    for validator in field.validators:
        if isinstance(validator, FileExtensionValidator) and validator.allowed_extensions:
            return ",".join(f".{ext}" for ext in validator.allowed_extensions)
    return "image/*" if kind is FieldKind.IMAGE else None


# ── which fields belong on a form ───────────────────────────────────────────

#: Sections, in the order they are laid out. The identity of the record comes first
#: because it is what the editor came to change; audit metadata comes last because it
#: is what they came to read.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("main", "Details"),
    ("relations", "Relationships"),
    ("flags", "Options"),
    ("metadata", "Metadata"),
)

_SECTION_ORDER = {key: index for index, (key, _) in enumerate(SECTIONS)}


def _section(field: Any) -> str:
    """Which section a field belongs to.

    JSON counts as metadata rather than content: a raw blob is a technical detail, and
    putting it between the title and the body pushes the fields people actually edit
    below the fold.
    """
    name = getattr(field, "name", "")
    if name in TRAILING_NAMES or name == "deleted_at":
        return "metadata"
    kind = classify(field)
    if kind is FieldKind.JSON:
        return "metadata"
    if kind in (FieldKind.RELATION_ONE, FieldKind.RELATION_MANY):
        return "relations"
    if kind is FieldKind.BOOLEAN:
        return "flags"
    return "main"


def infer_form_fields(model: type[models.Model]) -> list[str]:
    """The fields a create/edit form should offer, in the order they should appear.

    Excluded: anything Django will not let a form write — auto primary keys,
    ``auto_now``/``auto_now_add`` timestamps, ``editable=False`` — plus generated
    identity fields, sensitive names, and many-to-many relations through an explicit
    model. That last one is not a taste call: ``.set()`` raises on a through model with
    extra fields, so a multi-select there would render fine and fail on save. Those
    belong in an inline.

    Ordering follows the same sections as :func:`layout`, so the flat field list and the
    grouped one never disagree, with declaration order breaking ties inside a section so
    the result follows the shape of the model as written.
    """
    candidates = [
        *editable_fields(model),
        *(f for f in m2m_fields(model) if f.editable),
    ]

    ordered: list[tuple[int, int, int, str]] = []
    for index, field in enumerate(candidates):
        if SENSITIVE.search(field.name):
            continue
        if field.primary_key and field.has_default():
            # A generated identity is not something to type over by accident.
            continue
        if field.many_to_many and not is_auto_created_through(through_model(field)):
            continue
        # The record's own name leads its section; everything else keeps its place.
        rank = 0 if field.name in DISPLAY_NAMES else 1
        ordered.append((_SECTION_ORDER[_section(field)], rank, index, field.name))

    ordered.sort()
    return [name for *_, name in ordered]


@dataclass(frozen=True)
class FormSection:
    """One group of fields on the form. ``key`` is stable; ``title`` is for humans."""

    key: str
    title: str
    fields: tuple[str, ...]


def layout(model: type[models.Model], fields: list[str] | None = None) -> list[FormSection]:
    """Group form fields into sections, dropping the ones that end up empty.

    The grouping is by role rather than by type: what an editor scans for is "the thing
    itself", then "what it is attached to", then "its switches", then "when it happened".
    A field whose name does not resolve — a form-only field a panel added — stays in the
    main section, where an unexplained input is least surprising.
    """
    names = infer_form_fields(model) if fields is None else list(fields)
    grouped: dict[str, list[str]] = {key: [] for key, _ in SECTIONS}

    for name in names:
        hops = resolve_path(model, name)
        grouped["main" if not hops else _section(hops[-1].field)].append(name)

    return [
        FormSection(key=key, title=title, fields=tuple(grouped[key]))
        for key, title in SECTIONS
        if grouped[key]
    ]


# ── the form class ──────────────────────────────────────────────────────────


def _formfield_callback(db_field: models.Field, **kwargs: Any) -> forms.Field | None:
    """Give every field the inferred widget, then let Django build the form field.

    Going through ``db_field.formfield()`` rather than constructing form fields directly
    is the whole point: it is what keeps ``ModelChoiceField`` querysets, ``limit_choices_to``,
    choice coercion, validators and ``max_length`` checks working exactly as Django
    intends. Only the widget is ours.
    """
    kwargs.setdefault("widget", infer_widget(db_field).build_widget())
    return db_field.formfield(**kwargs)


def build_form_class(
    panel_or_model: Any, fields: list[str] | None = None
) -> type[forms.ModelForm]:
    """Build a ``ModelForm`` for a model or a :class:`~vantage.panels.ModelPanel`.

    ``modelform_factory`` does the work. Vantage does not reimplement ``ModelForm``:
    validation, ``_post_clean``, ``construct_instance`` and unique checks are subtle,
    well tested, and identical to what we would need. Accepting a panel keeps the
    per-attribute override rule — a panel that sets ``fields`` overrides the field list
    and nothing else; widgets stay inferred.
    """
    model = getattr(panel_or_model, "model", panel_or_model)
    if fields is None:
        fields = getattr(panel_or_model, "fields", None) or infer_form_fields(model)
    excluded = set(getattr(panel_or_model, "exclude", None) or ())
    names = [name for name in fields if name not in excluded]
    return modelform_factory(model, fields=names, formfield_callback=_formfield_callback)
