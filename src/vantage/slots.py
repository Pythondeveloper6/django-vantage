"""Slots — the named places a project can put its own HTML inside a panel page.

A slot is the escape hatch that stops the next feature request from being "please add
an option for this". Overriding a whole template to add one badge means inheriting
every future change to that template by hand; a slot is a stable, named seam that
survives upgrades::

    # blog/panel.py
    from vantage import slots

    slots.add("before_list", template="blog/_import_banner.html")

    @slots.slot("object_actions", permission="blog.change_post", model=Post)
    def preview_button(context):
        return format_html('<a class="v-btn" href="{}">Preview</a>', context["object"].url)

Three properties are non-negotiable, and each is here because the alternative is a
bug report nobody can act on:

**Output is escaped unless the contributor marked it safe.** A callable returning a
plain string gets ``conditional_escape``. Injecting HTML is an explicit act —
``format_html`` or ``mark_safe`` — exactly as it is everywhere else in Django. A
template contribution is trusted, because the template engine already escaped its
variables on the way through.

**One bad contribution never takes the page down.** A slot renders third-party code in
the middle of somebody's list page. ``Exception`` is the right breadth here for the
same reason it is in ``dashboards._render_safely``: the alternative is one team's
plugin making another team's records unreachable.

**A contribution can declare a permission.** An action button rendered for a user who
cannot use it is worse than no button, and gating it in the fragment's own template
puts the check somewhere nobody audits.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.template.loader import render_to_string
from django.utils.html import conditional_escape
from django.utils.safestring import SafeString, mark_safe

__all__ = [
    "BUILTIN_SLOTS",
    "Contribution",
    "add",
    "clear",
    "contributions",
    "declare",
    "is_filled",
    "render_slot",
    "slot",
    "slot_names",
]

logger = logging.getLogger("vantage.slots")

#: Where the shipped templates call ``{% slot %}``. Registering against a name that is
#: not one of these — or not passed to :func:`declare` — raises, because a typo in a
#: slot name is otherwise invisible: the fragment simply never appears, and there is
#: nothing to notice.
BUILTIN_SLOTS: tuple[str, ...] = (
    "before_list",
    "after_list",
    "before_form",
    "after_form",
    "object_actions",
    "row_actions",
    "detail_sidebar",
    "dashboard_top",
)

_declared: set[str] = set(BUILTIN_SLOTS)
_registry: dict[str, list[Contribution]] = {}
_sequence = itertools.count()


@dataclass(frozen=True)
class Contribution:
    """One fragment registered against one slot."""

    slot: str
    template: str | None = None
    render: Callable[[dict], Any] | None = None
    #: A Django permission string, or a callable taking the request. ``None`` means
    #: anyone who can already see the page.
    permission: str | Callable[[Any], bool] | None = None
    #: Restrict to one model's pages. ``None`` means every page carrying this slot.
    model: type | None = None
    #: Contributions sort by this, then by registration order. It exists so a plugin
    #: can sit ahead of project code without the project having to import it.
    order: int = 0
    sequence: int = 0

    @property
    def source(self) -> str:
        """A name for the log line when this contribution fails."""
        if self.template:
            return self.template
        func = self.render
        module = getattr(func, "__module__", "?")
        name = getattr(func, "__qualname__", None) or repr(func)
        return f"{module}.{name}"


# ── registration ────────────────────────────────────────────────────────────


def declare(*names: str) -> None:
    """Allow ``{% slot %}`` names beyond the built-in set.

    Only needed by a project that overrode a shipped template and added a slot of its
    own; without it, :func:`add` would reject the name as a typo.
    """
    _declared.update(names)


def slot_names() -> list[str]:
    return sorted(_declared)


def add(
    name: str,
    template: str | None = None,
    render: Callable[[dict], Any] | None = None,
    *,
    permission: str | Callable[[Any], bool] | None = None,
    model: type | None = None,
    order: int = 0,
) -> Contribution:
    """Register a fragment for the slot ``name``.

    Exactly one of ``template`` or ``render``. Both would be ambiguous, neither is a
    typo, and finding out at render time — inside the try/except that swallows
    failures — is precisely where a configuration mistake should not surface.
    """
    if name not in _declared:
        raise ValueError(
            f"{name!r} is not a known Vantage slot. Known slots: "
            f"{', '.join(slot_names())}. Call vantage.slots.declare({name!r}) first if "
            f"you added it to a template of your own."
        )
    if (template is None) == (render is None):
        raise ValueError(
            "A slot contribution needs exactly one of 'template' or 'render'."
        )
    if render is not None and not callable(render):
        raise TypeError("'render' must be callable; it receives the template context.")

    contribution = Contribution(
        slot=name,
        template=template,
        render=render,
        permission=permission,
        model=model,
        order=order,
        sequence=next(_sequence),
    )
    _registry.setdefault(name, []).append(contribution)
    return contribution


def slot(
    name: str,
    *,
    permission: str | Callable[[Any], bool] | None = None,
    model: type | None = None,
    order: int = 0,
):
    """Decorator form of :func:`add` for a function returning HTML.

    The function receives the template context as a plain dict and returns a string.
    Returning ``None`` or ``""`` contributes nothing, which is how a fragment opts out
    for a particular record without needing a permission to express it.
    """

    def wrap(func: Callable[[dict], Any]) -> Callable[[dict], Any]:
        add(name, render=func, permission=permission, model=model, order=order)
        return func

    return wrap


def contributions(name: str) -> list[Contribution]:
    """Everything registered for ``name``, in the order it will render."""
    return sorted(_registry.get(name, ()), key=lambda c: (c.order, c.sequence))


def clear(name: str | None = None) -> None:
    """Drop registrations. Used by tests; harmless in production, useless there."""
    if name is None:
        _registry.clear()
    else:
        _registry.pop(name, None)


# ── rendering ───────────────────────────────────────────────────────────────


def render_slot(name: str, context: Any = None, **extra: Any) -> SafeString:
    """Render every eligible contribution to ``name``, concatenated.

    ``context`` is the template context (or any dict). ``extra`` is merged over it, so
    a per-row slot can say ``{% slot "row_actions" object=row.obj %}`` without the view
    having to put anything anywhere.
    """
    data = _as_dict(context)
    if extra:
        data = {**data, **extra}

    request = data.get("request")
    panel = data.get("panel")

    parts: list[str] = []

    # The panel's own hook goes first: it is the narrowest scope in play, and on a page
    # about one model its content is the content most likely to be about the record in
    # front of you.
    hook = getattr(panel, "slot", None)
    if callable(hook):
        rendered = _guard(
            lambda: hook(name, data), f"{type(panel).__name__}.slot({name!r})"
        )
        if rendered:
            parts.append(conditional_escape(rendered))

    for contribution in contributions(name):
        if not _in_scope(contribution, panel):
            continue
        rendered = _guard(
            lambda c=contribution: _render_one(c, data, request), contribution.source
        )
        if rendered:
            parts.append(rendered)

    return mark_safe("".join(parts))


def is_filled(name: str, context: Any = None) -> bool:
    """Whether anything *could* render here, without rendering it.

    Templates need this to decide whether to emit the wrapper — an extra empty table
    column on every row is worse than no column at all — and deciding it by rendering
    first would run every contribution twice.
    """
    data = _as_dict(context)
    request = data.get("request")
    panel = data.get("panel")

    if callable(getattr(panel, "slot", None)):
        return True
    for contribution in contributions(name):
        if not _in_scope(contribution, panel):
            continue
        if _guard(lambda c=contribution: _allowed(c, request), contribution.source):
            return True
    return False


def _in_scope(contribution: Contribution, panel) -> bool:
    """Whether a model-scoped contribution belongs on the page being rendered."""
    if contribution.model is None:
        return True
    return getattr(panel, "model", None) is contribution.model


def _render_one(contribution: Contribution, data: dict, request) -> str:
    if not _allowed(contribution, request):
        return ""

    if contribution.template:
        # Not escaped: a template escaped its own variables on the way through, and
        # escaping the result again would render the fragment's own markup as text.
        return mark_safe(render_to_string(contribution.template, data, request=request))

    result = contribution.render(data)  # type: ignore[misc]
    if result is None:
        return ""
    # Escaped unless the contributor said otherwise with mark_safe/format_html.
    # Injecting HTML from a slot is a deliberate act, the same as anywhere else.
    return conditional_escape(result)


def _allowed(contribution: Contribution, request) -> bool:
    """Whether this user may see this fragment.

    Mirrors :meth:`vantage.pages.Page.has_permission` exactly, so the two ways of
    gating something in the panel behave the same way.
    """
    permission = contribution.permission
    if permission is None:
        return True
    if request is None:
        # A gated fragment with nobody to check is not "allowed by default": failing
        # open here would leak content into any context that forgot the request.
        return False
    if callable(permission):
        return bool(permission(request))
    user = getattr(request, "user", None)
    return bool(user is not None and user.has_perm(str(permission)))


def _guard(work: Callable[[], Any], source: str):
    """Run one contribution. A failure is logged and skipped, never propagated."""
    try:
        return work()
    except Exception:
        logger.exception("Vantage slot contribution from %s failed.", source)
        return None


def _as_dict(context: Any) -> dict:
    if context is None:
        return {}
    if isinstance(context, dict):
        return context
    flatten = getattr(context, "flatten", None)
    return flatten() if callable(flatten) else dict(context)
