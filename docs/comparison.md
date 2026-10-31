# Vantage vs Unfold (and other contrib.admin themes)

[← Index](index.md)

This is a comparison of approach, written from the Vantage side, so read it with that in
mind. It lists what is different and when each is the better choice. It contains no
benchmark numbers: see [Benchmarks](benchmarks.md) for how to measure on your own data.

## The core difference

[Unfold](https://github.com/unfoldadmin/django-unfold) and similar packages (Jazzmin,
Grappelli, django-admin-interface) **restyle and extend `django.contrib.admin`**. Your
`ModelAdmin` classes, `admin.site`, third-party admin mixins and the admin's templates
stay; you gain a new look and extra options.

Vantage **replaces** the admin. Panels are configured with `ModelPanel`, pages are built
from Vantage's own views and templates, and `ModelAdmin` code is not run (the
[adapter](extending.md) translates the common attributes, and `vantage_adopt` reports
the rest).

## Feature comparison

| | Vantage | Unfold / admin themes |
|---|---|---|
| Built on | Its own views and templates | `django.contrib.admin` |
| Existing `admin.py` | Translated, not run; see [Migrating](migrating-from-django-admin.md) | Used unchanged |
| Third-party admin plugins | Not run; equivalents for several ([Plugin compatibility](plugin-compatibility.md)) | Mostly work, since the admin is still there (Unfold ships integrations for some) |
| Columns, filters, search | Inferred from the model; overriding one keeps the rest inferred | Declared in `ModelAdmin`, as in the stock admin |
| Query optimisation | `select_related`/`prefetch_related` derived from the columns shown | As in the stock admin: declare `list_select_related` yourself |
| Dashboard and charts | Built in, declared in Python ([Pages and dashboards](pages-and-dashboards.md), [Analytics](analytics.md)) | Dashboard components are provided; you write the callback |
| List views | Table, plus kanban, calendar, map and gallery when the model fits ([List views](list-views.md)) | Table |
| Saved views, column preferences | Built in, per user and shared | Not part of the base theme |
| Import and export | Built in | Via django-import-export (Unfold has a styled integration) |
| Change history, undo, approvals | Built in ([History](history.md), [Undo](undo.md), [Approvals](approvals.md)) | Via third-party packages, or the stock `LogEntry` |
| Field-level permissions, roles | Built in ([Field permissions](field-permissions.md), [Roles](roles.md)) | Stock Django permissions; plugins for more |
| Two-factor sign-in | Built in (TOTP) | Via django-otp or similar |
| JSON API | Built in, same permission path as the UI ([API](api.md)) | None |
| Inline formsets, nested editing | `ModelPanel.inlines`; hand-declared | Stock inlines, restyled; extra options such as tabbed inlines |
| Rich-text and widget packages | Plain textarea with a markdown toolbar; widget JavaScript is not loaded | Works with admin widgets |
| Theming | Brand colour, radius, light/dark, presets ([Theming](theming.md)) | Extensive theming and configuration |
| Maturity | Alpha; the API may change before 1.0 ([FAQ](faq.md)) | Established project with a larger user base |
| Python / Django | Python 3.12+, Django 5.2 to 6.1 | See that project's documentation |

Entries in the right-hand column describe how the stock admin and typical themes work;
check the Unfold documentation for its current feature set, which changes.

## When to choose which

**Choose a theme such as Unfold when:**

- you rely on third-party admin packages and do not want to replace them;
- you have a large `admin.py` and the goal is a better look, not a different tool;
- you need a stable, widely used dependency today.

**Choose Vantage when:**

- you want columns, filters, search and query optimisation inferred, with little
  configuration per model;
- you want history, undo, approvals, roles, saved views, import/export and a JSON API
  without assembling packages;
- you are starting a new internal tool, or are willing to port the admin classes you
  actually use (start with `manage.py vantage_adopt`);
- you accept an alpha release, a smaller ecosystem and pinning an exact version.

They are not mutually exclusive during a migration: the stock admin (themed or not) can
stay mounted on another prefix while Vantage takes over model by model.
