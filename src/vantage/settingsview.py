"""The settings screen: branding, appearance and behaviour, editable without a deploy.

The screen is deliberately not a generic model form. Three things it has to do that a
generic form cannot:

**Show what "empty" means.** Every control displays the value it falls back to when it
is cleared, because "clear this field" is only a safe instruction if the user can see
where it lands. That value comes from :func:`vantage.siteconfig.code_defaults`, which
is the cascade with the database taken out of it.

**Preview the brand colour honestly.** One hex value expands into a whole palette, and
for some hues the derivation has to move the lightness to keep text readable. The
preview is rendered by the same :mod:`vantage.color` code that will render the panel —
fetched from this view rather than reimplemented in JavaScript, because two
implementations of a colour space is two answers to the same question.

**Say when the colour was adjusted.** Silently shipping a different colour from the one
that was typed is the kind of thing that gets blamed on the browser for a week.
"""

from __future__ import annotations

from dataclasses import dataclass

from django import forms
from django.conf import settings as django_settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect, JsonResponse

from vantage.color import (
    BLACK,
    LIGHT_TARGET,
    WCAG_AA,
    WHITE,
    accessible_pair,
    clamp_to_gamut,
    contrast_ratio,
    derive_palette,
    hex_to_oklch,
)
from vantage.conf import DEFAULTS, vantage_settings
from vantage.models import MAX_UPLOAD_BYTES, SiteConfiguration
from vantage.siteconfig import UI_EDITABLE, code_defaults
from vantage.views import SiteView

__all__ = ["SECTIONS", "SiteSettingsForm", "SiteSettingsView", "brand_report"]

#: The screen's shape: key, heading, blurb, and the columns it owns. Also what the
#: per-section reset acts on, so the button can never disagree with the card above it.
SECTIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "branding",
        "Branding",
        "The name and marks that identify this panel.",
        ("site_title", "site_acronym", "logo", "logo_dark", "favicon", "login_image"),
    ),
    (
        "appearance",
        "Appearance",
        "One colour drives the whole palette. Everything else is derived from it.",
        ("brand_color", "brand_radius", "theme", "theme_toggle"),
    ),
    (
        "behaviour",
        "Behaviour",
        "How the panel acts, for everyone who uses it.",
        ("environment", "default_per_page", "command_palette", "read_only"),
    ),
)

#: Columns whose control wants the full width of the grid.
_WIDE = frozenset({"environment", "login_image"})

_LABELS = {
    "site_title": "Site title",
    "site_acronym": "Short mark",
    "logo": "Logo",
    "logo_dark": "Logo (dark theme)",
    "favicon": "Favicon",
    "login_image": "Login page image",
    "brand_color": "Brand colour",
    "brand_radius": "Corner radius",
    "theme": "Default theme",
    "theme_toggle": "Theme toggle",
    "environment": "Environment banner",
    "default_per_page": "Rows per page",
    "command_palette": "Command palette",
    "read_only": "Read-only mode",
}

_HELP = {
    "site_acronym": "Shown in the collapsed sidebar. Derived from the title when empty.",
    "logo_dark": "Falls back to the light logo when empty.",
    "brand_color": "A hex colour. Every button, link and focus ring is derived from it.",
    "brand_radius": "A CSS length: 0.5rem, 8px, or 0 for square corners.",
    "theme_toggle": "Lets each person choose light or dark for themselves.",
    "environment": "A banner across the top of every page. Empty hides it.",
    "read_only": "Hides every create, edit and delete control for everyone.",
}

_BOOLEAN_LABELS = {
    "theme_toggle": ("On", "Off"),
    "command_palette": ("On", "Off"),
    "read_only": ("On — nothing can be created, changed or deleted", "Off"),
}


# ── the brand-colour preview ────────────────────────────────────────────────

#: The tokens worth showing as swatches. The full palette is larger; these are the ones
#: a person can actually recognise on the page they are about to change.
PREVIEW_TOKENS = ("primary", "primary-hover", "primary-foreground", "ring", "accent")


@dataclass(frozen=True)
class BrandReport:
    """What one hex colour turns into, and whether it survived the accessibility pass."""

    color: str
    light: list[dict[str, str]]
    dark: list[dict[str, str]]
    ratio: float
    requested_ratio: float
    adjusted: bool
    warning: str | None

    def as_json(self) -> dict:
        return {
            "ok": True,
            "color": self.color,
            "light": self.light,
            "dark": self.dark,
            "ratio": round(self.ratio, 2),
            "requested_ratio": round(self.requested_ratio, 2),
            "adjusted": self.adjusted,
            "warning": self.warning,
        }


def brand_report(color: str) -> BrandReport:
    """Derive the palette for ``color`` and report what the derivation had to do.

    Raises ``ValueError`` for anything that is not a hex colour, same as the renderer.
    """
    seed = clamp_to_gamut(hex_to_oklch(color))
    requested = clamp_to_gamut(seed.with_l(LIGHT_TARGET))
    primary, foreground = accessible_pair(seed, LIGHT_TARGET)

    ratio = contrast_ratio(primary, foreground)
    # The best any text colour could manage on the colour as asked for, which is the
    # number that explains why the derivation moved.
    requested_ratio = max(contrast_ratio(requested, WHITE), contrast_ratio(requested, BLACK))
    adjusted = abs(primary.l - requested.l) > 0.005

    warning = None
    if adjusted:
        warning = (
            f"{color} cannot carry readable text at the lightness the panel uses for "
            f"buttons — the best contrast available there is {requested_ratio:.1f}:1, and "
            f"WCAG AA needs {WCAG_AA}:1. The derived button colour was "
            f"{'darkened' if primary.l < requested.l else 'lightened'} to reach "
            f"{ratio:.1f}:1. Your colour is stored exactly as you entered it."
        )

    return BrandReport(
        color=color,
        light=_swatches(color, dark=False),
        dark=_swatches(color, dark=True),
        ratio=ratio,
        requested_ratio=requested_ratio,
        adjusted=adjusted,
        warning=warning,
    )


def _swatches(color: str, *, dark: bool) -> list[dict[str, str]]:
    palette = derive_palette(color, dark=dark)
    return [{"name": name, "value": palette[name]} for name in PREVIEW_TOKENS]


# ── the form ────────────────────────────────────────────────────────────────


class SiteSettingsForm(forms.ModelForm):
    """Only the allowlisted columns, and an empty control always means "inherit".

    ``CharField(null=True)`` gives Django's ``empty_value=None`` for free, so clearing a
    text box writes NULL rather than the empty string — which is what makes the cascade
    fall back through to ``settings.py`` instead of overriding it with nothing.
    """

    class Meta:
        model = SiteConfiguration
        fields = [name for _, _, _, names in SECTIONS for name in names]
        labels = _LABELS
        help_texts = _HELP

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        inherited = code_defaults()

        for name, field in self.fields.items():
            key = _setting_key(name)
            fallback = inherited.get(key)

            if isinstance(field, forms.NullBooleanField):
                on, off = _BOOLEAN_LABELS[name]
                field.widget = forms.Select(
                    choices=[
                        ("", f"Use the code default ({'on' if fallback else 'off'})"),
                        ("true", on),
                        ("false", off),
                    ],
                    attrs={"class": "v-select"},
                )
                continue

            if name == "theme":
                field.choices = [("", f"Use the code default ({fallback})")] + [
                    choice for choice in field.choices if choice[0]
                ]
                field.widget.attrs["class"] = "v-select"
                continue

            widget = field.widget
            if isinstance(widget, forms.ClearableFileInput):
                widget.attrs["class"] = "v-file"
                widget.attrs["accept"] = ".svg,.png,.jpg,.jpeg,.webp"
                continue

            widget.attrs["class"] = "v-input"
            if fallback not in (None, ""):
                # The placeholder is the inherited value, so an empty box reads as
                # "this one" rather than as "nothing".
                widget.attrs["placeholder"] = str(fallback)

        self.fields["brand_color"].widget.attrs.update(
            {"data-vantage-brand": "", "spellcheck": "false", "autocomplete": "off"}
        )


def _setting_key(field_name: str) -> str:
    for key, column in UI_EDITABLE.items():
        if column == field_name:
            return key
    raise KeyError(field_name)  # pragma: no cover - guarded by test_siteconfig


# ── the view ────────────────────────────────────────────────────────────────


def may_edit_settings(request) -> bool:
    """Superusers, unless ``VANTAGE["SETTINGS_PERMISSION"]`` says otherwise.

    Superuser-only is the honest default: this screen can rebrand the site, switch the
    panel to read-only, and upload a file that every other user's browser will load.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False

    rule = vantage_settings.SETTINGS_PERMISSION
    if rule is None:
        return bool(user.is_superuser)
    if callable(rule):
        return bool(rule(request))
    return user.has_perm(str(rule))


class SiteSettingsView(SiteView):
    """Read and write the stored overrides. One row, one form, three sections."""

    template_name = "vantage/settings.html"
    page_title = "Settings"
    page_description = "Change the panel's branding and behaviour without a deploy."

    def check_permission(self, request):
        if may_edit_settings(request):
            return None
        raise PermissionDenied("You do not have permission to change the site settings.")

    def get_breadcrumbs(self):
        return [("Settings", None)]

    # ── read ────────────────────────────────────────────────────────────────

    def get(self, request, *args, **kwargs):
        preview = request.GET.get("preview")
        if preview is not None:
            return self.preview(preview)
        return super().get(request, *args, **kwargs)

    def preview(self, color: str) -> JsonResponse:
        """The palette for a colour the user has typed but not yet saved."""
        try:
            report = brand_report(color)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse(report.as_json())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = kwargs.get("config") or SiteConfiguration.load()
        form = kwargs.get("form") or SiteSettingsForm(instance=config)
        inherited = code_defaults()

        context.update(
            form=form,
            config=config,
            sections=[_section(form, config, inherited, spec) for spec in SECTIONS],
            brand=_current_report(form, inherited),
            code_only=sorted(set(DEFAULTS) - set(UI_EDITABLE)),
            max_upload_kb=MAX_UPLOAD_BYTES // 1024,
            # Uploads land in MEDIA_ROOT and are served from MEDIA_URL. Neither has a
            # useful default, and the failure without them is a silently broken image.
            media_configured=bool(django_settings.MEDIA_ROOT and django_settings.MEDIA_URL),
        )
        return context

    # ── write ───────────────────────────────────────────────────────────────

    def post(self, request, *args, **kwargs):
        config = SiteConfiguration.load()

        section = request.POST.get("reset")
        if section:
            return self.reset(request, config, section)

        form = SiteSettingsForm(request.POST, request.FILES, instance=config)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form, config=config))

        config = form.save(commit=False)
        config.updated_by = request.user
        config.save()

        messages.success(request, "Settings saved.")
        self.warn_about_contrast(request, config)
        return HttpResponseRedirect(request.path)

    def reset(self, request, config, section: str):
        """Clear one section back to whatever the code says."""
        names = {key: names for key, _, _, names in SECTIONS}.get(section)
        if names is None:
            # Only reachable from a hand-crafted POST. Nothing to do and nothing to say.
            return HttpResponseRedirect(request.path)

        if config.pk:
            for name in names:
                if _is_file(config, name):
                    stored = getattr(config, name)
                    if stored:
                        # Drop the bytes too: an unreferenced upload is still served to
                        # anyone who kept the URL.
                        stored.delete(save=False)
                    setattr(config, name, "")
                else:
                    setattr(config, name, None)
            config.updated_by = request.user
            config.save()

        messages.success(request, f"{section.capitalize()} reset to the code defaults.")
        return HttpResponseRedirect(request.path)

    def warn_about_contrast(self, request, config) -> None:
        """Accept the colour, but never change it behind the user's back in silence."""
        color = config.brand_color or code_defaults()["BRAND_COLOR"]
        try:
            report = brand_report(color)
        except ValueError:  # pragma: no cover - the field validator rejects these first
            return
        if report.warning:
            messages.warning(request, report.warning)


def _current_report(form, inherited) -> BrandReport | None:
    """The palette the page should show: what is in the form, else what is inherited."""
    value = form["brand_color"].value() or inherited["BRAND_COLOR"]
    try:
        return brand_report(str(value))
    except ValueError:
        return None


def _section(form, config, inherited, spec) -> dict:
    key, title, blurb, names = spec
    return {
        "key": key,
        "title": title,
        "description": blurb,
        "fields": [_field(form, config, inherited, name) for name in names],
    }


def _is_file(config, name: str) -> bool:
    """Asked of the model field, never of the value.

    ``hasattr(field_file, "url")`` looks like the obvious test and is a trap: the
    property raises ``ValueError`` when no file is stored, and ``hasattr`` only swallows
    ``AttributeError``.
    """
    from django.db.models import FileField

    return isinstance(config._meta.get_field(name), FileField)


def _field(form, config, inherited, name: str) -> dict:
    setting = _setting_key(name)
    current = getattr(config, name, None)
    is_file = _is_file(config, name)
    return {
        "field": form[name],
        "setting": setting,
        "inherited": inherited.get(setting),
        "overridden": bool(current) if is_file else current is not None,
        "is_file": is_file,
        "file": current if is_file and current else None,
        # Resolved here rather than in the template: FieldFile.url raises when the
        # project has no MEDIA_URL, and a raised ValueError inside template variable
        # resolution is a 500, not a blank.
        "file_url": _file_url(current) if is_file and current else None,
        "wide": name in _WIDE,
    }


def _file_url(stored) -> str | None:
    try:
        return stored.url
    except ValueError:
        return None
