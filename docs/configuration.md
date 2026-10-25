# How configuration resolves

[← Index](index.md)

This package has two ways to configure almost everything a panel does: write Python
(`panel.py` and the `VANTAGE` settings dict), or use the panel's own UI. They are not
two competing systems — they compose in one fixed order, and every page in this
documentation that has a UI layer links back to this one so the order only has to be
stated once.

> **UI override → `panel.py` / `VANTAGE` → inference.**
>
> The narrowest thing that has an opinion wins. A person using the panel can override
> what you wrote without touching a deploy; what you wrote in Python overrides what the
> package would otherwise have guessed; and the guess is always there as the floor, so
> a project that configures nothing still gets something worth looking at.

## Why it has to be this order, not some other one

Reverse the order — say, "Python always wins" — and the per-list customisation rail and
the settings screen stop being real features: a colleague hides a column at 9am, and it
reappears the moment anyone redeploys, because the code they never edited "wins". A UI
control that a redeploy can silently undo is not a control, it is a suggestion, and it
trains people to stop trusting it.

Reverse it the other way — "the UI always wins, permanently" — and `panel.py` stops
being the source of truth a developer can read to know what a page does. Six months of
UI tweaks by different people would leave a `list_display = [...]` in the repository
that nobody could relate to what actually renders, which is precisely the failure mode
a config-as-code tool exists to prevent.

So the rule is not "UI wins" or "Python wins" — it is **narrowest wins, and the
narrowest layer can always be reset back to what the code declares.** A UI override is
real and it is reversible, on purpose, by the same action: delete the override and you
are looking at `panel.py` again, not at a system nobody can fully explain.

## The same shape, wherever it appears

Every layer that stores a UI override follows the pattern
[`ListPreference`](lists.md#the-customisation-rail) established first: a database row
that sits **underneath** the getter a caller already uses, not beside it.

```python
def get_list_filter(self, request=None) -> list[str]:
    stored = listprefs.override(self, request, "list_filter")   # UI override
    if stored is not None:
        return list(stored)
    if self.list_filter is not None:                            # panel.py
        return list(self.list_filter)
    return inference.infer_list_filter(self.model)               # inference
```

Three consequences follow from writing it this way rather than as a single
all-or-nothing fallback:

- **Every existing call site picks up a UI override for free.** `check`, `scaffold`,
  the registry and a dashboard's table card all call `panel.get_list_display()` and
  none of them had to change when the UI layer was added — they simply keep asking the
  panel, and the panel now has one more place to check before it answers.
- **Resolution is per attribute, not per panel.** Overriding a list's columns from the
  UI does not switch off inference for its filters, and declaring `list_filter` in
  Python does not switch off the UI's ability to hide a column. Each getter runs its
  own three-step cascade independently — the same rule
  [Panels](panels.md#the-rule-that-governs-every-attribute) states for the Python layer
  alone, extended one layer further out.
- **The UI layer can only narrow, never widen.** A stored preference is checked against
  what the panel *currently* exposes, both when it is saved and again when it is read —
  see [the customisation rail's ceiling](lists.md#the-customisation-rail). A UI control
  that could show a column `panel.py` never declared would make the Python file a lie
  about what the page can do.

## "Why is my `panel.py` being ignored?"

One answer: **something narrower has an opinion.** In order:

1. **Is there a personal preference?** Open the surface's own UI control — the
   [customisation rail](lists.md#the-customisation-rail) on a list page, the
   [settings screen](theming.md#the-settings-screen) for branding and behaviour — and
   look for a "reset to what the code declares" action. If one is offered, something is
   currently overriding the code, by design.
2. **Is there a site-wide preference someone else saved?** Personal preferences apply
   only to the person who set them; a site-wide one applies to everyone and needs
   [`LIST_PREFS_PERMISSION`](settings.md#behaviour) or
   [`SETTINGS_PERMISSION`](settings.md#behaviour) to have been granted to set it. If you
   cannot see who changed it, that permission is too wide.
3. **Is the database table missing or unreachable?** Both layers are
   [failure-tolerant by design](faq.md#does-it-need-a-database-table) — a missing table
   resolves silently to whatever the code says, which means this is never the cause of
   a *wrong* value, only ever of a UI control quietly doing nothing. If the rail's
   changes are not sticking at all, check that `migrate` has run.
4. **Otherwise, it is inference filling a gap you thought you closed.** Confirm the
   attribute name is spelled the way `ModelPanel` expects — `manage.py check` catches a
   genuine typo, but an attribute that exists under a different name (`search_fields`
   vs. `list_search`, say) silently does nothing and inference answers instead. Run
   `manage.py vantage_scaffold <app>` to see what the panel currently resolves to and
   compare it against what you wrote.

Nothing in this list is "your Python was overruled by a bug" — every layer that can
override `panel.py` does so as its entire documented purpose, and every one of them
is reversible from the surface that set it.

## What is configurable from the UI today, and what is not

Not every knob has a UI layer yet. Sidebar navigation is Python-only for now: there is
no database-backed override for it, so `VANTAGE["NAV"]` is the only word on it, and
there is nothing to reset because nothing can be overridden. Everything else in the
table below — including the dashboard — has one.

| Surface | UI layer | Python layer | Docs |
|---|---|---|---|
| List columns, filters, search, stats, charts, summary bar | [The customisation rail](lists.md#the-customisation-rail), per list | `list_display`, `list_filter`, `list_search`, `list_stats`, `list_charts`, `list_aggregates` on `ModelPanel` | [Lists](lists.md), [Panels](panels.md) |
| Dashboard cards: which show, their order and width, and cards built entirely in the UI over any model | [`?arrange=1`](pages-and-dashboards.md#arranging-the-dashboard-from-the-ui) on the dashboard itself | `VANTAGE["DASHBOARD"]`, a `Dashboard` subclass | [Pages and dashboards](pages-and-dashboards.md#dashboards) |
| Branding: title, logo, favicon, brand colour, corner radius, theme | [The settings screen](theming.md#the-settings-screen) | `SITE_TITLE`, `LOGO`, `BRAND_COLOR`… in `VANTAGE` | [Theming](theming.md) |
| Behaviour: environment banner, rows per page, command palette, read-only mode | [The settings screen](theming.md#the-settings-screen) | `ENVIRONMENT`, `DEFAULT_PER_PAGE`, `COMMAND_PALETTE`, `READ_ONLY` in `VANTAGE` | [Settings](settings.md#behaviour) |
| Sidebar navigation: groups, order, labels, icons, hidden items, extra links | [The settings screen's Sidebar card](theming.md#the-settings-screen) | `VANTAGE["NAV"]` | [Settings](settings.md#navigation) |
| Permissions, tenancy | — Python only, deliberately: a permission boundary is not a preference | `VANTAGE["PERMISSIONS"]`, per-panel hooks, `VANTAGE["TENANT_MODEL"]` | [Permissions](permissions.md), [Multi-tenancy](tenancy.md) |

The bottom row is not an oversight to apologise for: a permission boundary that a
signed-in user could loosen from inside the panel would not be a boundary, and CSRF
tokens aside, "everyone who can reach the settings screen can also decide who can
delete records" is a sentence that should make you uneasy on sight. That one is
Python-only on purpose and will stay that way; the sidebar is Python-only simply
because a UI for it has not been built yet.

## See also

- [Panels](panels.md#the-rule-that-governs-every-attribute) — the per-attribute rule at
  the Python layer, before the UI layer is added on top
- [Lists](lists.md#the-customisation-rail) — the fullest worked example of this cascade
- [Theming](theming.md#the-settings-screen) — the other UI layer, for branding and
  behaviour
- [Settings](settings.md#where-a-value-comes-from) — the `VANTAGE` dict's own cascade
  underneath a `SiteConfiguration` row, the same shape one level up
