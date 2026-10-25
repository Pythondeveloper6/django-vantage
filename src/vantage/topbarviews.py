"""The two screens behind the avatar: your account, and arranging the top bar.

**The account page is not a nicety.** It is what the avatar button *is* when there is
no JavaScript — a plain link to a page that lists every option the menu would have
shown, sign-out included. A menu that only exists once a script has run is a menu that
takes sign-out away from anyone whose script did not, and losing sign-out is not a
degraded experience, it is a security one. So the page comes first and the dropdown is
the enhancement over it, rather than the other way round.

**The arrangement screen** is the top bar's half of §4.12: reorder the controls, switch
them off, and add links of your own, without editing Python. It writes the same row the
sidebar editor writes, and it is reachable from the account menu by anyone who may
already change the site settings.

Every reordering control here is a submit button, not a drag handle. Dragging is added
on top by :file:`vantage-topbar.js`, and the buttons stay: a keyboard user reordering a
list with drag-and-drop is the accessibility failure this whole feature is supposed to
avoid repeating.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from vantage.settingsview import may_edit_settings
from vantage.templatetags.vantage import ICONS
from vantage.topbar import (
    BUILTIN_KEYS,
    ITEM_KINDS,
    REQUIRED_KEYS,
    build_topbar,
    clean_layout,
    clean_url,
    default_layout,
    editor_slots,
    reset_layout,
    save_layout,
    stored_layout,
)
from vantage.views import SiteView

__all__ = ["AccountView", "TopbarSettingsView", "build_urls"]


class AccountView(SiteView):
    """Who you are, and everything the account menu offers, as ordinary links.

    Also a real destination in its own right: it is where the avatar goes on a middle
    click, and it is the only one of these controls that can be bookmarked.
    """

    template_name = "vantage/account.html"
    page_title = _("Your account")
    page_description = _("Your details, and everything the account menu offers.")

    def get_breadcrumbs(self):
        return [(_("Your account"), None)]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["may_arrange_topbar"] = may_edit_settings(self.request)
        return context


class TopbarSettingsView(SiteView):
    """Read and write the stored top-bar arrangement."""

    template_name = "vantage/topbar.html"
    page_title = _("Top bar")
    page_description = _(
        "Arrange the controls along the top of every page, and add links of your own."
    )

    def check_permission(self, request):
        if may_edit_settings(request):
            return None
        raise PermissionDenied(_("You do not have permission to change the top bar."))

    def get_breadcrumbs(self):
        return [(_("Top bar"), None)]

    # ── read ────────────────────────────────────────────────────────────────

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        layout = kwargs.get("layout")
        if layout is None:
            stored = stored_layout()
            layout = clean_layout(stored) if stored is not None else default_layout()

        context.update(
            layout=layout,
            rows=editor_slots(self.site, self.request, layout),
            # The bar as it will actually render, from the same builder the shell uses
            # rather than a second one that could disagree with it.
            preview=build_topbar(self.site, self.request, layout),
            is_stored=stored_layout() is not None,
            kinds=ITEM_KINDS,
            icon_names=sorted(ICONS),
            add=kwargs.get("add") or {},
            errors=kwargs.get("errors") or {},
        )
        return context

    # ── write ───────────────────────────────────────────────────────────────

    def post(self, request, *args, **kwargs):
        if request.POST.get("reset"):
            reset_layout()
            messages.success(request, _("The top bar was reset to what the code says."))
            return HttpResponseRedirect(request.path)

        layout = self._layout_from_post(request)

        removed = request.POST.get("remove")
        if removed:
            layout = _without(layout, removed)

        moved = request.POST.get("move")
        if moved:
            layout = _moved(layout, moved)

        if request.POST.get("add"):
            item, errors = self._new_item(request)
            if errors:
                # Re-rendered rather than redirected, so the half-filled form and the
                # unsaved reordering above it both survive the mistake.
                return self.render_to_response(
                    self.get_context_data(layout=layout, errors=errors, add=request.POST.dict())
                )
            layout = _with(layout, item)

        save_layout(layout, request.user)
        messages.success(request, _("The top bar was saved."))
        return HttpResponseRedirect(request.path)

    # ── parsing ─────────────────────────────────────────────────────────────

    def _layout_from_post(self, request) -> dict:
        """The arrangement the form is describing, before any button is applied."""
        current = clean_layout(stored_layout() or default_layout())
        by_id = {item["id"]: dict(item) for item in current["items"]}

        for identifier, item in by_id.items():
            if f"label:{identifier}" in request.POST:
                item["label"] = request.POST.get(f"label:{identifier}", "").strip()
            if f"icon:{identifier}" in request.POST:
                item["icon"] = request.POST.get(f"icon:{identifier}", "").strip() or None

        # The order is one field, written by the drag script and by the move buttons
        # alike, so there is one thing to read here whether or not a script ran.
        posted = request.POST.get("order", "").split()
        order = posted or current["order"]

        visible = set(request.POST.getlist("visible"))
        known = set(BUILTIN_KEYS) | {f"item:{identifier}" for identifier in by_id}
        hidden = [
            key for key in order if key in known and key not in visible and key not in REQUIRED_KEYS
        ]

        return clean_layout(
            {
                "order": order,
                "hidden": hidden,
                "banner": bool(request.POST.get("banner")),
                "items": list(by_id.values()),
            }
        )

    def _new_item(self, request) -> tuple[dict, dict]:
        """A custom item from the add form, plus whatever was wrong with it."""
        kind = request.POST.get("kind", "").strip()
        target = request.POST.get("target", "").strip()
        label = request.POST.get("label", "").strip()
        icon = request.POST.get("icon", "").strip()
        errors: dict[str, str] = {}

        if kind not in {key for key, _label in ITEM_KINDS}:
            errors["kind"] = str(_("Choose what this item points at."))
        if not target:
            errors["target"] = str(_("This cannot be empty."))
        elif kind == "route":
            try:
                reverse(target)
            except NoReverseMatch:
                errors["target"] = str(
                    _("No route is named %(name)s in this project.") % {"name": target}
                )
        elif kind == "page":
            slugs = {page.get_slug() for page in self.site.pages()}
            if target not in slugs:
                errors["target"] = str(
                    _("No custom page has the slug %(slug)s.") % {"slug": target}
                )
        elif kind == "url" and clean_url(target) is None:
            errors["target"] = str(
                _("Use a full https:// address, or a path beginning with a slash.")
            )

        if not label:
            errors["label"] = str(_("Give the item a name; it is what the bar shows."))

        return (
            {"kind": kind, "target": target, "label": label, "icon": icon or None},
            errors,
        )


# ── list surgery ────────────────────────────────────────────────────────────


def _with(layout: dict, item: dict) -> dict:
    """Add one custom item, just before the avatar.

    At the end of the bar it would sit *after* the account menu, which is the one
    control every admin surface keeps last. The id is not chosen here: ``clean_layout``
    mints one and guarantees it is unique, so this reads it back rather than guessing.
    """
    resolved = clean_layout({**layout, "items": [*layout["items"], item]})
    key = f"item:{resolved['items'][-1]['id']}"
    order = [entry for entry in resolved["order"] if entry != key]
    order.insert(order.index("user") if "user" in order else len(order), key)
    return clean_layout({**resolved, "order": order})


def _without(layout: dict, key: str) -> dict:
    """Drop one custom item. Built-ins are hidden, never removed."""
    if not key.startswith("item:"):
        return layout
    identifier = key[5:]
    return clean_layout(
        {
            **layout,
            "order": [entry for entry in layout["order"] if entry != key],
            "hidden": [entry for entry in layout["hidden"] if entry != key],
            "items": [item for item in layout["items"] if item["id"] != identifier],
        }
    )


def _moved(layout: dict, instruction: str) -> dict:
    """Apply one ``"<key>:up"`` / ``"<key>:down"`` button press."""
    key, _sep, direction = instruction.rpartition(":")
    order = list(layout["order"])
    if key not in order or direction not in ("up", "down"):
        return layout

    index = order.index(key)
    target = index - 1 if direction == "up" else index + 1
    if not 0 <= target < len(order):
        return layout

    order[index], order[target] = order[target], order[index]
    return clean_layout({**layout, "order": order})


# ── routes ──────────────────────────────────────────────────────────────────


def build_urls(site) -> list:
    from django.urls import path

    return [
        path("_/account/", AccountView.as_view(site=site), name="account"),
        path("_/topbar/", TopbarSettingsView.as_view(site=site), name="topbar"),
    ]
