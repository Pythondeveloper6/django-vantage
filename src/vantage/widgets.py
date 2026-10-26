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

The one thing in this module that is *not* pure is :class:`SearchSelect`, and it is not
a function: it is the widget a relation renders through, and it settles the cardinality
question at render time against the field's own queryset. That is the only moment at
which the queryset exists, ``limit_choices_to`` and all — and rendering is already a
query, so one bounded ``LIMIT`` more is not a new cost.
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
from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.forms.models import modelform_factory
from django.utils.translation import gettext_lazy as _

from vantage.inference import DISPLAY_NAMES, TRAILING_NAMES
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
    "SearchSelect",
    "SearchSelectMultiple",
    "WidgetSpec",
    "WriteOnlyInput",
    "WriteOnlyModelForm",
    "WriteOnlyTextarea",
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


def _picker_is_unconditional() -> bool:
    """Whether a single-valued relation gets the picker regardless of how few rows.

    Read at render time rather than captured at import, so ``override_settings`` in a
    test and a settings reload in a running process both take effect. The accessor is
    lazy, cached and self-invalidating, so this is not a per-render settings parse.
    """
    from vantage.conf import vantage_settings

    return vantage_settings.RELATION_PICKER == "always"


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
DURATION_HELP = _("Days, hours, minutes and seconds, e.g. '3 12:30:00' or '01:30:00'.")


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


# ── the searchable relation control ─────────────────────────────────────────


class SearchSelect(forms.Select):
    """A relation ``<select>`` that stays a ``<select>`` however big the table is.

    Two problems, one control. The first is the render: ``ModelChoiceField`` iterates
    its whole queryset, so a foreign key to a fifty-thousand-row table puts fifty
    thousand ``<option>`` elements on the page and the browser stops responding. The
    second is the *use*: even if it rendered, nobody scrolls fifty thousand rows.

    The fix for both is to stop rendering the full list. This widget probes the
    queryset with one bounded query — ``LIMIT threshold + 1``, the same "one extra
    row" trick the autocomplete endpoint uses instead of a ``COUNT`` — and then:

    * **at or below the threshold** it behaves exactly like ``forms.Select``, options
      and all. A combobox over five options is worse than a select, so a small
      relation must not become one;
    * **above it** it renders the currently selected row plus a first page, and marks
      itself up for ``combobox.js`` to enhance into a type-to-search control.

    The count is resolved here, at render time, rather than in :func:`infer_widget`,
    because this is the only layer entitled to touch the database — and because the
    queryset it sees is the *field's*, with ``limit_choices_to`` and any panel
    narrowing already applied, which a count taken against the model would miss.

    Nothing about the form contract changes. The element that submits is still the
    real ``<select>``, still under the field's own name, still validated by
    ``ModelChoiceField`` against the full queryset — a rendered option list has never
    been what authorises a value, so shortening it cannot widen what is accepted.
    """

    template_name = "vantage/widgets/search_select.html"

    #: Above this many rows the control becomes a search box. Same number the spec
    #: uses, so ``needs_search`` and what actually renders cannot disagree.
    threshold = RELATION_SEARCH_THRESHOLD

    #: Set on every render by :meth:`optgroups`, read by :meth:`get_context`.
    #: ``searchable`` asks whether to enhance the control; ``truncated`` asks whether
    #: the option list is only a first page. They are not the same question.
    searchable = False
    truncated = False

    # ── options ─────────────────────────────────────────────────────────────

    def optgroups(self, name, value, attrs=None):
        """The options to render: all of them, or the selection plus a first page."""
        iterator = self.choices
        queryset = getattr(iterator, "queryset", None)
        if queryset is None:
            # Not a model choice field at all — a panel handed us a plain choice list.
            self.searchable = False
            return super().optgroups(name, value, attrs)

        from vantage.autocomplete import PAGE_SIZE

        rows = list(queryset[: self.threshold + 1])
        self.truncated = len(rows) > self.threshold
        # Two separate questions, and conflating them is what left an eight-row tag
        # field as a bare <select multiple>.
        #
        # Whether to *truncate* is about size: a list too long to render.
        #
        # Whether to *enhance* is about the control. A native <select> over a short
        # list is genuinely good — it is keyboard-navigable, type-ahead works, and a
        # combobox over eight options is worse. A native <select multiple> is not
        # good at any size: the selection is hidden behind ctrl-click, there is no
        # visible record of what is chosen, and one ordinary click silently discards
        # every other selection. So a multi-valued relation always gets the chip
        # picker, however few rows it has, while a single-valued one waits for the
        # threshold.
        self.searchable = (
            self.truncated or self.allow_multiple_selected or _picker_is_unconditional()
        )
        if self.truncated:
            rows = rows[:PAGE_SIZE]

        pairs = []
        empty_label = getattr(getattr(iterator, "field", None), "empty_label", None)
        if empty_label is not None:
            pairs.append(("", empty_label))
        if self.truncated:
            rows = self._with_selection(queryset, rows, value)
        pairs.extend(iterator.choice(obj) for obj in rows)

        groups = []
        has_selected = False
        for index, (option_value, option_label) in enumerate(pairs):
            selected = (not has_selected or self.allow_multiple_selected) and str(
                option_value
            ) in value
            has_selected |= selected
            option = self.create_option(
                name, option_value, option_label, selected, index, attrs=attrs
            )
            groups.append((None, [option], index))
        return groups

    def _with_selection(self, queryset, rows, value):
        """The selected rows first, then the first page, each row appearing once.

        Fetching the selection by primary key is what makes the control show its
        current label on load without a round trip — and it is not optional: the
        selected row is very unlikely to be in the first page of an alphabetical
        list, and an option that is not rendered is a value the browser drops on
        the next submit.

        Only reached when the list is being truncated. Below the threshold the
        options keep the queryset's own ordering, because there the selection is
        already in the list and hoisting it would reorder a control that used to
        render alphabetically.
        """
        chosen = [item for item in value if item not in ("", None)]
        selected_rows = []
        if chosen:
            try:
                selected_rows = list(queryset.filter(pk__in=chosen))
            except (ValueError, TypeError, ValidationError):
                # A bound form re-rendering after a failed POST can carry a value
                # that is not a primary key at all. It is invalid, not fatal.
                selected_rows = []

        seen = set()
        ordered = []
        for obj in [*selected_rows, *rows]:
            key = str(obj.pk)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(obj)
        return ordered

    # ── the wrapper the browser enhances ────────────────────────────────────

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        widget = context["widget"]
        widget["searchable"] = self.searchable
        if not self.searchable:
            # The flag was a prediction made without a row count; the count is in
            # now and says otherwise. Leaving it on would tell the front end to
            # enhance a control that has its whole option list already.
            widget["attrs"].pop("data-vantage-search", None)
            return context

        model = getattr(getattr(self.choices, "queryset", None), "model", None)
        widget["search_url"] = self._search_url()
        widget["search_model"] = model._meta.label if model is not None else ""
        widget["search_placeholder"] = self._placeholder(model)
        # Only say the list is partial when it actually is. A short many-to-many now
        # renders through this template too, and its option list is complete — the
        # note would be telling the reader to enable JavaScript to reach rows that
        # are already on the page.
        widget["truncated"] = self.truncated
        widget["search_note"] = (
            _("Showing the first %(count)s of many. Turn on JavaScript to search the rest.")
            % {"count": self._option_count(widget)}
            if self.truncated
            else ""
        )
        return context

    @staticmethod
    def _option_count(widget) -> int:
        """Rendered options, not counting the empty "no choice" one."""
        return sum(
            1
            for _group, options, _index in widget["optgroups"]
            for option in options
            if str(option["value"]) != ""
        )

    @staticmethod
    def _placeholder(model) -> str:
        if model is None:
            return str(_("Type to search…"))
        return str(_("Search %(name)s…") % {"name": model._meta.verbose_name_plural})

    @staticmethod
    def _search_url() -> str:
        """The autocomplete endpoint, or "" when the panel is not mounted.

        Reversed at render rather than stored on the widget so a widget built by
        :func:`infer_widget` — which knows nothing about URLs, sites or requests —
        still finds the endpoint of whichever site is serving the page.
        """
        from django.urls import NoReverseMatch, reverse

        try:
            return reverse("vantage:autocomplete")
        except NoReverseMatch:  # pragma: no cover - only off a mounted panel
            return ""


class SearchSelectMultiple(SearchSelect, forms.SelectMultiple):
    """:class:`SearchSelect` for a many-to-many. Same truncation, several values."""


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
        if kind is FieldKind.RELATION_ONE:
            # SearchSelect *is* a Select and renders as one below the threshold, so
            # the searchable variant is safe whenever the count is unknown — which,
            # since counting is a query and this layer does not make queries, is the
            # normal case. A caller that has counted and said "small" gets the plain
            # control and no probe query at all.
            return SearchSelect(attrs=attrs) if self.needs_search else forms.Select(attrs=attrs)
        if kind is FieldKind.CHOICE:
            return forms.Select(attrs=attrs)
        if kind is FieldKind.RELATION_MANY:
            if self.needs_search:
                return SearchSelectMultiple(attrs=attrs)
            return forms.SelectMultiple(attrs=attrs)
        if kind in (FieldKind.FILE, FieldKind.IMAGE):
            # Imported here, not at module scope: vantage.media reaches for Pillow and
            # for storage, and this module is imported while the app registry loads.
            from vantage.media import file_widget

            return file_widget(kind, attrs)
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
        # Picked up by vantage-editors.js: a plain <textarea> is what every richtext
        # kind renders as (Vantage integrates no CKEditor/TinyMCE/Quill of its own,
        # whichever package's class name triggered the classification), so a markdown
        # toolbar is the one enhancement that suits all of them.
        attrs["data-markdown"] = "1"
    if kind is FieldKind.JSON:
        attrs["rows"] = str(JSON_ROWS)
    if kind in (FieldKind.FILE, FieldKind.IMAGE):
        accept = _accept(field, kind)
        if accept:
            attrs["accept"] = accept

    choices = getattr(field, "choices", None)
    related = getattr(field, "related_model", None)
    is_relation = kind in (FieldKind.RELATION_ONE, FieldKind.RELATION_MANY)
    # A many-to-many always gets the picker; see SearchSelect.optgroups for why a row
    # count is the wrong question to ask about a <select multiple>. For a single-valued
    # relation it is a genuine trade-off, so RELATION_PICKER decides: "always" for one
    # consistent control, "threshold" to keep a native select while the list is short.
    needs_search = is_relation and (
        kind is FieldKind.RELATION_MANY
        or _picker_is_unconditional()
        or related_count is None
        or related_count > RELATION_SEARCH_THRESHOLD
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
    ("main", _("Details")),
    ("relations", _("Relationships")),
    ("flags", _("Options")),
    ("metadata", _("Metadata")),
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
    identity fields, hashed secrets, and many-to-many relations through an explicit
    model. That last one is not a taste call: ``.set()`` raises on a through model with
    extra fields, so a multi-select there would render fine and fail on save. Those
    belong in an inline.

    Other sensitive fields *are* offered, write-only: :func:`build_form_class` renders
    them empty, and an empty submission keeps the stored value. See
    :class:`WriteOnlyModelForm`.

    Ordering follows the same sections as :func:`layout`, so the flat field list and the
    grouped one never disagree, with declaration order breaking ties inside a section so
    the result follows the shape of the model as written.
    """
    candidates = [
        *editable_fields(model),
        *(f for f in m2m_fields(model) if f.editable),
    ]

    from vantage.sensitive import field_is_sensitive, is_hashed

    ordered: list[tuple[int, int, int, str]] = []
    for index, field in enumerate(candidates):
        if field_is_sensitive(field) and is_hashed(model, field.name):
            # A hash typed into a text box is a broken credential, not a new one. The
            # detail page offers "Set password" for the one hash Django knows how to set.
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
    # Every relation control in the package is built here, so this is the one place a
    # dropdown can be stopped from offering another tenant's records. A no-op unless
    # VANTAGE["TENANT_MODEL"] is set. See vantage.tenancy.scope_choices.
    from vantage.tenancy import scope_choices

    formfield = scope_choices(db_field.formfield(**kwargs))

    # A related record's __str__ is the option label, and some __str__s print a token.
    if isinstance(formfield, forms.ModelChoiceField):
        from vantage.sensitive import safe_str

        formfield.label_from_instance = safe_str

    # An upload is the one input a panel accepts that the browser did not type. The
    # extension is a claim, the Content-Type is a claim, and both are chosen by whoever
    # supplied the file; vantage.media checks the bytes instead. Appended rather than
    # assigned so the field's own validators keep running.
    if formfield is not None and isinstance(db_field, models.FileField):
        from vantage.media import upload_validator

        formfield.validators = [*formfield.validators, upload_validator(db_field)]

    return formfield


# ── write-only secrets ──────────────────────────────────────────────────────

#: The suffix of the "clear this value" checkbox a nullable secret gets on edit.
CLEAR_SUFFIX = "__clear"


class _WriteOnly:
    """Never put a value into the markup, and offer a clear box when asked to.

    The value passed to ``render`` is discarded — including on a re-render after a
    validation error, where Django would otherwise echo back whatever was typed.
    """

    clear_label: str | None = None

    def render(self, name, value, attrs=None, renderer=None):
        from django.utils.html import format_html

        html = super().render(name, None, attrs, renderer)
        if not self.clear_label:
            return html
        control_id = f"{(attrs or {}).get('id') or 'id_' + name}{CLEAR_SUFFIX}"
        return format_html(
            '{}<label class="v-writeonly-clear" for="{}">'
            '<input type="checkbox" class="v-checkbox" name="{}" id="{}" value="1"> {}</label>',
            html,
            control_id,
            f"{name}{CLEAR_SUFFIX}",
            control_id,
            self.clear_label,
        )

    def format_value(self, value):
        return None


class WriteOnlyInput(_WriteOnly, forms.PasswordInput):
    """A one-line secret: a password box that never renders what is stored."""

    def __init__(self, attrs=None):
        super().__init__(attrs, render_value=False)


class WriteOnlyTextarea(_WriteOnly, forms.Textarea):
    """A multi-line secret — a PEM key, a JSON credential — rendered empty."""


class WriteOnlyModelForm(forms.ModelForm):
    """A ``ModelForm`` whose sensitive fields can be written but never read back.

    * The control renders empty, with a placeholder saying whether a value is stored.
    * An empty submission on an existing record keeps the stored value, so editing a
      customer's name does not wipe their API token.
    * A nullable (or blank-able) secret gets a "clear" checkbox, the only way to empty
      it on purpose.
    * ``form.initial`` never holds the stored value either, so nothing that reads the
      form — a template, a diff, a third-party widget — can find it there.
    """

    #: Names rendered write-only. Set by :func:`build_form_class`.
    vantage_write_only: tuple[str, ...] = ()
    #: Every sensitive name on the model, for the audit diff. Set by build_form_class.
    vantage_sensitive_fields: frozenset[str] = frozenset()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._vantage_originals: dict[str, Any] = {}
        self._vantage_cleared: set[str] = set()
        editing = self.instance.pk is not None and not self.instance._state.adding
        for name in self.vantage_write_only:
            field = self.fields.get(name)
            if field is None:
                continue
            model_field = _model_field_or_none(self._meta.model, name)
            stored = getattr(self.instance, getattr(model_field, "attname", name), None)
            has_value = editing and stored not in (None, "", b"")
            self._vantage_originals[name] = stored
            self.initial[name] = None

            attrs = {k: v for k, v in field.widget.attrs.items() if k != "value"}
            attrs.update(
                {
                    "autocomplete": "new-password",
                    "spellcheck": "false",
                    "autocapitalize": "off",
                    "data-vantage-writeonly": "",
                    "placeholder": str(
                        _("Unchanged — type to replace")
                        if has_value
                        else (_("Not set") if editing else "")
                    ),
                }
            )
            widget_class = (
                WriteOnlyTextarea if isinstance(field.widget, forms.Textarea) else WriteOnlyInput
            )
            widget = widget_class(attrs=attrs)
            nullable = bool(
                getattr(model_field, "null", False) or getattr(model_field, "blank", False)
            )
            if has_value and nullable:
                widget.clear_label = str(_("Clear %(field)s") % {"field": field.label or name})
            field.widget = widget
            if has_value:
                field.required = False

    @property
    def _editing(self) -> bool:
        return self.instance.pk is not None and not self.instance._state.adding

    def _clear_requested(self, name: str) -> bool:
        if not self.is_bound:
            return False
        return str(self.data.get(self.add_prefix(name) + CLEAR_SUFFIX, "")).lower() in (
            "1",
            "on",
            "true",
        )

    def clean(self):
        cleaned = super().clean()
        for name in self.vantage_write_only:
            if name not in self.fields:
                continue
            original = self._vantage_originals.get(name)
            if self._editing and self._clear_requested(name) and original not in (None, ""):
                model_field = _model_field_or_none(self._meta.model, name)
                cleaned[name] = None if getattr(model_field, "null", False) else ""
                self._vantage_cleared.add(name)
                self.errors.pop(name, None)
            elif (
                self._editing
                and name not in self.errors
                and cleaned.get(name) in self.fields[name].empty_values
            ):
                # Empty means "leave it alone". The stored value goes back in so
                # construct_instance writes what was there rather than nothing.
                cleaned[name] = original
        return cleaned

    @property
    def changed_data(self) -> list[str]:
        changed = [
            name
            for name in forms.BaseForm.changed_data.func(self)
            if name not in self.vantage_write_only or self._received_value(name)
        ]
        return changed + sorted(self._vantage_cleared - set(changed))

    def _received_value(self, name: str) -> bool:
        """Whether a write-only field received a value, as opposed to "keep it"."""
        data = self.data.get(self.add_prefix(name)) if self.is_bound else None
        return data not in (None, "")


def _model_field_or_none(model, name: str):
    try:
        return model._meta.get_field(name)
    except Exception:
        return None


def build_form_class(panel_or_model: Any, fields: list[str] | None = None) -> type[forms.ModelForm]:
    """Build a ``ModelForm`` for a model or a :class:`~vantage.panels.ModelPanel`.

    ``modelform_factory`` does the work. Vantage does not reimplement ``ModelForm``:
    validation, ``_post_clean``, ``construct_instance`` and unique checks are subtle,
    well tested, and identical to what we would need. Accepting a panel keeps the
    per-attribute override rule — a panel that sets ``fields`` overrides the field list
    and nothing else; widgets stay inferred.
    """
    from vantage.sensitive import is_hashed, sensitive_names

    model = getattr(panel_or_model, "model", panel_or_model)
    if fields is None:
        fields = getattr(panel_or_model, "fields", None) or infer_form_fields(model)
    excluded = set(getattr(panel_or_model, "exclude", None) or ())
    secret = sensitive_names(panel_or_model)
    # A hash is never a form field, even when a panel lists it: whatever was typed
    # would be stored as the "hash", and the account behind it would stop working.
    names = [
        name
        for name in fields
        if name not in excluded and not (name in secret and is_hashed(model, name))
    ]
    form_class = modelform_factory(
        model, form=WriteOnlyModelForm, fields=names, formfield_callback=_formfield_callback
    )
    form_class.vantage_write_only = tuple(name for name in names if name in secret)
    form_class.vantage_sensitive_fields = secret
    return form_class
