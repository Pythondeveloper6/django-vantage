"""Sensitive fields: inferred with no configuration, masked everywhere, revealed one at a time.

An admin panel is read by more people than the records it describes, and most of them
need to know *that* a customer has an API token, not what it is. So a field that looks
like a secret is treated as one before anybody says so, and every surface that can carry
a value — a table cell, the detail page, an export, the JSON API, global search, the
audit log — asks this module first.

**One decision, asked everywhere.** Which fields are sensitive is answered by
:meth:`vantage.panels.ModelPanel.get_sensitive_fields`, which reads
``sensitive_fields`` when the panel sets it and :func:`infer_sensitive_fields` when it
does not. Everything else in the package calls :func:`is_sensitive_path` or
:func:`sensitive_names` rather than restating the rule, so a field cannot be masked on
the list page and printed in the CSV because two modules disagreed about it.

**Matching is by whole word, and by type.** ``token`` matches ``api_token`` and
``token`` but not ``tokenizer_name``; ``secret`` does not match ``is_secretary``;
nothing in the list matches ``passport_photo_url`` — a passport *photo* is personal data
with its own access story, not a credential, and masking every column that mentions a
passport would teach people to switch the feature off. A name is also only a secret if
the column can hold one: ``password_changed_at`` is a date, ``otp_enabled`` a boolean,
``token_type`` a choice and ``pin_attempts`` a counter, and masking any of them hides
nothing worth hiding while making the page harder to use. Encrypted field classes
(``EncryptedCharField`` and friends, from any package) are sensitive whatever they are
called, because encrypting a column is the developer saying so.

**Masking never encodes the value.** The mask is the same eight bullets for a three
character PIN and a forty character key, so its width is not a length oracle. Card,
IBAN and account numbers keep their last four characters — that is how people tell two
cards apart — but only when the value is long enough that four characters are a small
part of it.

**A hash is never revealed.** Django's ``User.password``, and any field whose name says
it holds the hash of another secret (``pin_hash``, ``api_key_digest``), are masked and
not revealable: a password hash is an offline cracking target, and a hashed PIN can be
brute-forced in milliseconds. The user model's password field is masked even when a
panel's ``sensitive_fields`` leaves it out — there is no screen on which printing it is
the right answer.

What this module does *not* protect is listed in ``docs/sensitive-fields.md``: the
database, the Django shell, ``DEBUG`` tracebacks and anything a panel author renders
with their own code are all outside the panel's reach.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from django.db import models
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.utils.translation import gettext as _

if TYPE_CHECKING:  # pragma: no cover
    from vantage.panels import ModelPanel

__all__ = [
    "DEFAULT_PATTERNS",
    "HASH_WORDS",
    "LAST4_PATTERNS",
    "MASK",
    "METADATA_SUFFIXES",
    "REVEAL_PERMISSION",
    "REVEAL_SECONDS",
    "can_reveal",
    "field_is_sensitive",
    "infer_sensitive_fields",
    "is_auth_password",
    "is_encrypted_field",
    "is_hashed",
    "is_sensitive_path",
    "mask",
    "masked_html",
    "may_reveal_anything",
    "name_is_sensitive",
    "patterns",
    "reveal_value",
    "safe_str",
    "sensitive_names",
    "set_password_url",
    "strip_sensitive",
    "uses_last4",
]

#: What a masked value reads as. Fixed width, whatever the value's length.
MASK = "•" * 8

#: The built-in vocabulary. Each entry is matched as a run of whole ``_``-separated
#: words, so ``api_key`` matches ``stripe_api_key`` but not ``api_keyboard``.
DEFAULT_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "client_secret",
    "ssn",
    "social_security",
    "tax_id",
    "iban",
    "account_number",
    "card_number",
    "cc_number",
    "credit_card",
    "cvv",
    "cvc",
    "pin",
    "otp",
    "salt",
    "hash",
    "signature",
)

#: Values people identify by their tail. Shown as ``•••• 4242`` rather than fully hidden.
LAST4_PATTERNS: tuple[str, ...] = (
    "iban",
    "account_number",
    "card_number",
    "cc_number",
    "credit_card",
)

#: A last-four mask needs at least this many characters, or four of them is most of it.
LAST4_MIN_LENGTH = 10

#: Words that mean "this column holds a digest of something", which is never revealed
#: when the name also mentions what was digested (``pin_hash``, ``password_digest``).
HASH_WORDS: tuple[str, ...] = ("hash", "hashed", "digest")

#: A trailing word that turns a secret's name into a fact *about* the secret:
#: ``token_type``, ``pin_attempts``, ``password_hint``, ``otp_enabled``. Columns named
#: like this are metadata, and masking them hides nothing worth hiding.
METADATA_SUFFIXES: frozenset[str] = frozenset(
    {
        "algorithm",
        "algo",
        "attempt",
        "attempts",
        "count",
        "enabled",
        "expires",
        "expiry",
        "expiration",
        "hint",
        "kind",
        "len",
        "length",
        "method",
        "policy",
        "required",
        "status",
        "type",
        "version",
    }
)

#: The Django permission that lets a non-superuser reveal a value. Created on
#: ``migrate`` by :func:`ensure_reveal_permission` rather than by a migration, so the
#: feature costs a project no schema change.
REVEAL_PERMISSION = "vantage.reveal_sensitive"
REVEAL_CODENAME = "reveal_sensitive"

#: How long a revealed value stays on screen before the page masks it again.
REVEAL_SECONDS = 30

#: Column types that can actually hold a secret. Everything else — booleans, dates,
#: relations, files, choices — is a fact about a secret at most.
_VALUE_TYPES = (
    models.CharField,
    models.TextField,
    models.BinaryField,
    models.JSONField,
    models.UUIDField,
    models.IntegerField,
    models.DecimalField,
    models.FloatField,
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


# ── the vocabulary ──────────────────────────────────────────────────────────


def patterns() -> tuple[str, ...]:
    """The active vocabulary: the built-in list, extended or replaced by settings.

    ``VANTAGE["SENSITIVE_PATTERNS"]`` replaces the list. Including ``"..."`` (or
    Python's ``...``) anywhere in it keeps the built-in entries too, so extending reads
    ``["...", "nhs_number"]`` rather than requiring a project to copy the defaults and
    then miss the next one added upstream.
    """
    configured = _configured()
    if configured is None:
        return DEFAULT_PATTERNS
    return _resolve_vocabulary(tuple(configured))


def _configured():
    """The raw setting, read past the settings layer on purpose.

    ``vantage_settings`` consults the ``SiteConfiguration`` row, which would put a
    query behind the first mask on every request — and the vocabulary must not be
    editable from inside the panel anyway: a screen that can shorten the list is a
    screen that can unmask a column. Deployment settings belong in the file the
    deployment owns, the same call :mod:`vantage.security` makes for rate limits.
    """
    from django.conf import settings as django_settings

    from vantage.conf import _validate_sensitive_patterns

    value = (getattr(django_settings, "VANTAGE", None) or {}).get("SENSITIVE_PATTERNS")
    _validate_sensitive_patterns(value)
    return value


@lru_cache(maxsize=16)
def _resolve_vocabulary(configured: tuple) -> tuple[str, ...]:
    resolved: list[str] = []
    for entry in configured:
        if entry is Ellipsis or entry == "...":
            resolved.extend(DEFAULT_PATTERNS)
        else:
            resolved.append(str(entry))
    return tuple(dict.fromkeys(resolved))


def _words(name: str) -> tuple[str, ...]:
    """``"stripeApiKey"`` and ``"stripe_api_key"`` both read as ``("stripe", "api", "key")``."""
    return tuple(word for word in _CAMEL.sub("_", name).lower().split("_") if word)


@lru_cache(maxsize=64)
def _compiled(vocabulary: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(words for entry in vocabulary if (words := _words(entry)))


def _contains(words: tuple[str, ...], run: tuple[str, ...]) -> bool:
    size = len(run)
    return any(words[i : i + size] == run for i in range(len(words) - size + 1))


def name_is_sensitive(name: str, vocabulary: tuple[str, ...] | None = None) -> bool:
    """Whether a name reads like a secret, by whole words and nothing else.

    A trailing metadata word (``_type``, ``_count``, ``_hint``…) cancels the match:
    ``token_type`` is not a token.
    """
    words = _words(name)
    if not words:
        return False
    if len(words) > 1 and words[-1] in METADATA_SUFFIXES:
        return False
    return any(_contains(words, run) for run in _compiled(vocabulary or patterns()))


def uses_last4(name: str) -> bool:
    """Whether a masked value for ``name`` keeps its last four characters."""
    words = _words(name)
    return any(_contains(words, run) for run in _compiled(LAST4_PATTERNS))


# ── fields ──────────────────────────────────────────────────────────────────


def is_encrypted_field(field: Any) -> bool:
    """A field class from any encryption package, recognised by name.

    ``django-fernet-fields``, ``django-encrypted-model-fields``, ``django-cryptography``
    and ``django-pgcrypto-fields`` all spell their classes ``Encrypted<Something>Field``
    (the last one builds them at runtime). Reading the MRO by name rather than importing
    keeps every one of them optional.
    """
    return any(
        cls.__name__.startswith("Encrypted") and cls.__name__.endswith("Field")
        for cls in type(field).__mro__
    )


def is_auth_password(field: Any) -> bool:
    """Django's own ``password`` column on a user model: always a hash."""
    model = getattr(field, "model", None)
    if model is None or getattr(field, "name", None) != "password":
        return False
    from django.contrib.auth.base_user import AbstractBaseUser

    return isinstance(model, type) and issubclass(model, AbstractBaseUser)


def field_is_sensitive(field: Any) -> bool:
    """The inference rule for one model field. Pure apart from the vocabulary setting."""
    if is_encrypted_field(field) or is_auth_password(field):
        return True
    if getattr(field, "primary_key", False) or getattr(field, "is_relation", False):
        return False
    if isinstance(field, models.AutoField | models.BooleanField):
        return False
    if not isinstance(field, _VALUE_TYPES) or getattr(field, "choices", None):
        return False
    return name_is_sensitive(getattr(field, "name", ""))


def infer_sensitive_fields(model: type[models.Model]) -> list[str]:
    """Every field of ``model`` that inference treats as sensitive, in declaration order."""
    return [
        field.name
        for field in model._meta.get_fields()
        if getattr(field, "concrete", False) and field_is_sensitive(field)
    ]


def _floor(model: type[models.Model]) -> set[str]:
    """Fields masked whatever a panel says. Today: a user model's password hash."""
    try:
        field = model._meta.get_field("password")
    except Exception:
        return set()
    return {"password"} if is_auth_password(field) else set()


def panel_sensitive_fields(panel: ModelPanel) -> frozenset[str]:
    """The resolved set for a panel: its declaration or inference, plus the floor."""
    declared = getattr(panel, "sensitive_fields", None)
    names = set(declared) if declared is not None else set(infer_sensitive_fields(panel.model))
    return frozenset(names | _floor(panel.model))


def _panel_for(model: type[models.Model], site=None):
    """The panel a model is registered with, on ``site`` or the default site."""
    candidates = [site] if site is not None else []
    try:
        from vantage.sites import site as default_site

        candidates.append(default_site)
    except Exception:  # pragma: no cover - only during app loading
        pass
    for candidate in candidates:
        try:
            if candidate.is_registered(model):
                return candidate.get_panel(model)
        except Exception:  # pragma: no cover - a half-built registry
            continue
    return None


def sensitive_names(target: Any, site=None) -> frozenset[str]:
    """The sensitive field names for a panel, a model, or a model instance.

    A panel answers for itself. A bare model is looked up in the registry — on ``site``
    when given, then the default site — so a ``sensitive_fields`` declaration is honoured
    by code that only has the model in hand, such as the audit log. An unregistered
    model falls back to inference.
    """
    if hasattr(target, "get_sensitive_fields"):
        return frozenset(target.get_sensitive_fields())
    model = target if isinstance(target, type) else type(target)
    panel = _panel_for(model, site)
    if panel is not None:
        return frozenset(panel.get_sensitive_fields())
    return _inferred(model, patterns())


@lru_cache(maxsize=512)
def _inferred(model: type[models.Model], vocabulary: tuple[str, ...]) -> frozenset[str]:
    return frozenset(set(infer_sensitive_fields(model)) | _floor(model))


def is_sensitive_path(target: Any, path: str, site=None) -> bool:
    """Whether a column, search path, filter or sort key reaches a sensitive value.

    ``target`` is a panel or a model. Lookup prefixes (``-``, ``^``, ``=``, ``@``) and a
    trailing lookup (``__icontains``) are ignored, and every hop is checked against
    *its own* model's answer — so ``customer__api_token`` is sensitive because
    ``api_token`` is sensitive on the customer, whichever panel asks.

    A name that is not a field at all (a model property, say) is judged by name: a
    property called ``secret`` that decrypts a column is exactly as sensitive as the
    column.
    """
    clean = str(path).lstrip("-^=@")
    memo = None
    if hasattr(target, "get_sensitive_fields"):
        target.get_sensitive_fields()  # refreshes the memo if the vocabulary moved
        memo = getattr(target, "_sensitive_memo", (None, None, None))[2]
        if memo is not None and clean in memo:
            return memo[clean]
    answer = _is_sensitive_path(target, clean, site)
    if memo is not None:
        memo[clean] = answer
    return answer


def _is_sensitive_path(target: Any, clean: str, site=None) -> bool:
    from vantage.introspection import resolve_path

    model = getattr(target, "model", target)
    site = site if site is not None else getattr(target, "site", None)
    hops = resolve_path(model, clean)
    if not hops:
        declared = getattr(target, "sensitive_fields", None)
        if declared is not None:
            return clean in declared
        return name_is_sensitive(clean.split("__")[0])
    for hop in hops:
        owner = target if hop.model is model and hasattr(target, "get_sensitive_fields") else None
        names = sensitive_names(owner or hop.model, site)
        if hop.name in names:
            return True
    return False


def strip_sensitive(target: Any, paths, site=None) -> list[str]:
    """``paths`` minus anything that reaches a sensitive value, order kept."""
    return [path for path in paths if not is_sensitive_path(target, path, site)]


def is_hashed(model: type[models.Model], name: str) -> bool:
    """Whether a sensitive field holds a hash and must never be revealed.

    The user model's ``password``, or a name with a hash word *and* another secret word
    in it — ``pin_hash``, ``api_key_digest``, ``password_hash``. A bare ``content_hash``
    is a checksum of public content and stays revealable.
    """
    try:
        field = model._meta.get_field(name)
    except Exception:
        field = None
    if field is not None and is_auth_password(field):
        return True
    words = _words(name)
    if not any(word in HASH_WORDS for word in words):
        return False
    rest = tuple(word for word in words if word not in HASH_WORDS)
    return bool(rest) and any(
        _contains(rest, run) for run in _compiled(patterns()) if run[0] not in HASH_WORDS
    )


def looks_like_password_hash(value: Any) -> bool:
    """Whether a value is in Django's ``algorithm$...`` hash format. Belt and braces."""
    if not isinstance(value, str) or "$" not in value:
        return False
    from django.contrib.auth.hashers import identify_hasher

    try:
        identify_hasher(value)
    except Exception:
        return False
    return True


# ── masking ─────────────────────────────────────────────────────────────────


def mask(value: Any, name: str = "") -> str:
    """The masked stand-in for a value. Never longer or shorter because the value was."""
    if name and uses_last4(name):
        text = "".join(ch for ch in str(value) if ch.isalnum())
        if len(text) >= LAST4_MIN_LENGTH:
            return f"{MASK[:4]} {text[-4:]}"
    return MASK


def reveal_url(panel: ModelPanel, obj: Any, name: str) -> str | None:
    """The POST endpoint that reveals one value, or ``None`` when it may never be shown."""
    from django.urls import NoReverseMatch, reverse

    if obj is None or getattr(obj, "pk", None) is None:
        return None
    try:
        field = panel.model._meta.get_field(name)
    except Exception:
        return None
    if not getattr(field, "concrete", False) or is_hashed(panel.model, name):
        return None
    try:
        return reverse(f"vantage:{panel.slug}-reveal", args=[obj.pk, name])
    except NoReverseMatch:
        return None


def masked_html(panel: ModelPanel | None, obj: Any, name: str, value: Any, label: str = ""):
    """The masked cell: bullets, plus what the reveal control needs to find its endpoint.

    The value itself is never in the markup — not in an attribute, not in a ``data-``
    blob, not in a ``title``. The reveal endpoint is a POST so that it is never
    prefetched, cached, or followed by a crawler holding a session.
    """
    from vantage.render import EMPTY

    if value is None or value == "" or value == b"":
        return EMPTY
    url = reveal_url(panel, obj, name) if panel is not None else None
    text = mask(value, name)
    if url is None:
        return format_html(
            '<span class="v-masked" data-vantage-masked>'
            '<span class="v-masked-value" aria-label="{}">{}</span></span>',
            _("Hidden value"),
            text,
        )
    return format_html(
        '<span class="v-masked" data-vantage-masked data-reveal-url="{}" data-reveal-label="{}">'
        '<span class="v-masked-value" aria-label="{}">{}</span></span>',
        url,
        label or name.replace("_", " "),
        _("Hidden value"),
        text,
    )


def set_password_url(panel: ModelPanel, obj: Any, name: str) -> str | None:
    """Where to set a new password instead of revealing the hash, when that applies."""
    from django.urls import NoReverseMatch, reverse

    try:
        field = panel.model._meta.get_field(name)
    except Exception:
        return None
    if not is_auth_password(field) or not hasattr(panel.model, "set_password"):
        return None
    if obj is None or getattr(obj, "pk", None) is None:
        return None
    try:
        return reverse(f"vantage:{panel.slug}-set-password", args=[obj.pk])
    except NoReverseMatch:
        return None


def reveal_value(value: Any) -> str:
    """The plain-text form of a revealed value, for the JSON response."""
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)


# ── __str__ ─────────────────────────────────────────────────────────────────

#: A sensitive value shorter than this is not looked for inside ``__str__``: a
#: three-digit CVV "appearing" in "Order 1234" is a coincidence, not a leak.
MIN_LEAK_LENGTH = 4


def safe_str(obj: Any, site=None) -> str:
    """``str(obj)``, unless that string contains one of the record's sensitive values.

    A ``__str__`` that returns the token — DRF's ``Token`` model does exactly that — would
    otherwise put the secret in the page title, the breadcrumbs, every foreign-key chip
    that points at the record, the search palette and the audit log. The fallback is
    ``"<Verbose name> #<pk>"``, which identifies the record without describing it.

    Only values already loaded on the instance are compared, so this never costs a
    query: a ``__str__`` that read a deferred field has loaded it by the time this looks.
    """
    if obj is None:
        return ""
    text = str(obj)
    meta = getattr(obj, "_meta", None)
    if not text or meta is None:
        return text
    loaded = getattr(obj, "__dict__", {})
    for name in sensitive_names(obj, site):
        try:
            field = meta.get_field(name)
        except Exception:
            continue
        value = loaded.get(getattr(field, "attname", name))
        if value is None:
            continue
        needle = str(value)
        if len(needle) >= MIN_LEAK_LENGTH and needle in text:
            return f"{str(meta.verbose_name).capitalize()} #{obj.pk}"
    return text


# ── reveal ──────────────────────────────────────────────────────────────────


def can_reveal(request, panel: ModelPanel, obj: Any, name: str) -> bool:
    """Whether this request may see this one value in full.

    The panel's ``can_reveal(request, obj, field)`` hook has the last word; its default
    allows superusers and holders of ``vantage.reveal_sensitive``. A hashed field is
    refused before the hook is asked, so no hook can make a password hash revealable.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_staff:
        return False
    if name not in panel.get_sensitive_fields() or is_hashed(panel.model, name):
        return False
    return bool(panel.can_reveal(request, obj, name))


def may_reveal_anything(request, site=None) -> bool:
    """Whether to offer reveal controls on this page at all. A UI hint, never a gate.

    True for superusers and holders of the permission, and for everyone when any panel
    overrides ``can_reveal`` — that hook can grant per record, which cannot be known
    page-wide without asking it once per cell. The endpoint asks the real question.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    if user.is_superuser or user.has_perm(REVEAL_PERMISSION):
        return True
    if site is None:
        return False
    from vantage.panels import ModelPanel

    return any(type(panel).can_reveal is not ModelPanel.can_reveal for panel in site.panels())


def ensure_reveal_permission(sender=None, using: str = "default", **kwargs) -> None:
    """Create ``vantage.reveal_sensitive`` after ``migrate``, without a migration.

    Hung off the log-entry model's content type because the permission is about the
    panel, not about any one model, and that content type is guaranteed to exist
    wherever Vantage is installed.
    """
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType

    from vantage.models import VantageLogEntry

    try:
        content_type = ContentType.objects.db_manager(using).get_for_model(VantageLogEntry)
        Permission.objects.db_manager(using).get_or_create(
            codename=REVEAL_CODENAME,
            content_type=content_type,
            defaults={"name": "Can reveal sensitive field values"},
        )
    except Exception:  # pragma: no cover - a database without the auth tables
        return


def mask_safe(value: Any, name: str = "") -> SafeString:
    """``mask`` as markup, for templates that render a value they did not build."""
    return format_html('<span class="v-masked">{}</span>', mask(value, name))
