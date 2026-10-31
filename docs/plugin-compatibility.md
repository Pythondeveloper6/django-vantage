# Plugin compatibility

[← Index](index.md)

`django.contrib.admin` has a large ecosystem of packages that subclass `ModelAdmin`,
inject templates or register widgets. Vantage does not run `ModelAdmin` code, so a
package that works by being the base class of an admin does not carry over by itself.
This page says what to do for the common ones. `python manage.py vantage_adopt` flags
the ones it recognises by name.

Status values:

- **Works as-is**: it does not depend on the admin, so it works in your project and
  with Vantage.
- **Vantage equivalent**: Vantage has its own feature that covers the same job.
- **Not supported**: no equivalent today.

| Package | Status | What to do |
|---|---|---|
| django-import-export | Vantage equivalent | List export (CSV, TSV, JSON) and a mapping/preview import wizard are built in: [Export](lists.md#export), [Import](lists.md#import). Resource classes with custom `before_import_row`/`dehydrate_*` logic are not read; port that logic or do the cleanup in the model. `ImportExportModelAdmin` is mapped by `vantage_adopt`. |
| django-simple-history | Vantage equivalent | Every change made through the panel is recorded with a diff and can be restored: [History](history.md). Changes made outside the panel are not seen. Existing `HistoricalXxx` tables stay in your database and stay readable, but Vantage does not import them into its own history. |
| django-reversion | Vantage equivalent (partial) | [History](history.md) and [Undo](undo.md) cover change history and reverting. Reversion's "recover deleted" view and its `VersionAdmin` mixin have no direct counterpart. Reversion itself keeps working in your code. |
| django-filter (admin integration) | Vantage equivalent | Filters are inferred from the model and configured with `list_filter` ([Lists](lists.md#filtering)). A custom `FilterSet` is not read. django-filter used in your own API or views is unaffected. |
| django-ckeditor, django-tinymce | Not supported (rich-text editor) | Fields using these still render, as a plain textarea with a small markdown toolbar ([Forms](forms.md)). Vantage does not load a widget's JavaScript/CSS, so the editor itself does not appear. Content is stored unchanged. |
| django-object-actions | Vantage equivalent | Bulk actions use `@action` on the panel ([Lists](lists.md#bulk-actions)); buttons on a detail page go in the `object_actions` slot ([Extending](extending.md)). Port the action bodies by hand. |
| django-admin-sortable2 | Not supported | No drag-to-reorder. Sort by the order column; edit it inline with `list_editable`. |
| django-mptt, django-treebeard | Works as-is (display) | Models detected as trees render parent before child, with indentation ([Lists](lists.md#grouping-trees-and-trash)). Drag-to-move in `DraggableMPTTAdmin` has no equivalent. |
| django-guardian | Works as-is | Object permissions are consulted through `AUTHENTICATION_BACKENDS`. For row filtering, set `VANTAGE["PERMISSIONS"] = "vantage.permissions.GuardianPermissions"`: [Permissions](permissions.md#django-guardian). The `GuardedModelAdmin` object-permission pages are not reproduced. |
| django-rules | Works as-is | Add the backend; predicates apply at the model and object level. Row scoping needs your own `filter_queryset`: [Permissions](permissions.md#django-rules). |
| django-otp, django-two-factor-auth | Vantage equivalent | Built-in TOTP sign-in with recovery codes: [Two-factor sign-in](two-factor.md). These packages protect their own login view; Vantage's second step is separate, and the two are not linked. Do not run both on the same login. Hardware keys and SMS codes are not supported. |
| django-allauth | Works as-is | Allauth owns your user-facing sign-in. Set `LOGIN_URL` to it if you want people to sign in there, or use Vantage's login pages ([Getting started](getting-started.md#signing-in)). Allauth's admin classes for email addresses and social accounts are not translated; register panels for those models if you need to see them. |
| django-unfold, django-jazzmin, other admin themes | Not applicable | These style `django.contrib.admin`. Vantage has its own interface and theme settings ([Theming](theming.md)). Unfold-specific `ModelAdmin` options (sections, conditional fields, custom dashboard callbacks) are not read; see [Comparison](comparison.md). |
| Anything else that subclasses `ModelAdmin` | Not read | `vantage_adopt` reports unknown base classes as "needs panel code". Check what the base class adds (extra views, templates, URLs) and port it as a panel method or a `Page`. |

## How to check your own project

```bash
python manage.py vantage_adopt --format json | jq '.models[].attributes[] | select(.attribute | startswith("mixin"))'
```

Every third-party base class on your admins appears as a `mixin ...` row with its status.

If a package here is marked "Works as-is" but something behaves differently, treat it as
a bug in this page and report it; the table is based on what the code does, not on
testing every version of every package.
