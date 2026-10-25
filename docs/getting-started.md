# Getting started

[← Index](index.md)

## Install

```bash
pip install --pre django-vantage
```

`--pre` is needed while Vantage is in alpha: pip skips pre-releases unless asked. Pin
the exact version (`django-vantage==0.1.0a1`) if you depend on it, because the API may
still move before 1.0.

Python 3.12+ and Django 5.2, 6.0 or 6.1 are required. Nothing else is.

## Mount it

Two edits to your project.

```python
# settings.py
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "vantage",
    # your apps
]
```

```python
# urls.py
from django.urls import path
from vantage import site

urlpatterns = [
    path("admin/", site.urls),
]
```

Vantage stores four things of its own — site configuration, an audit log, saved views
and per-list preferences — so run its migrations:

```bash
python manage.py migrate
```

Every one of those tables is optional in practice: if the table is missing, the layer
that uses it silently falls back to what the code says. That is deliberate, because a
settings layer that raises when its own table is absent makes the migration that would
create the table impossible to run. But you want the tables, so run `migrate`.

### Signing in

The panel redirects anonymous visitors to `settings.LOGIN_URL` and requires `is_staff`.

Vantage ships styled sign-in, sign-out and change-password views, because a panel that
hands a visitor an unstyled Django form has broken its own promise on the first screen
anyone ever sees:

```python
# urls.py
from django.urls import include, path
from vantage import site

urlpatterns = [
    path("admin/", site.urls),
    path("accounts/", include("vantage.authviews")),
]
```

```python
# settings.py
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/admin/"
```

The route names deliberately match `django.contrib.auth.urls` — `login`, `logout`,
`password_change` — and no `app_name` is declared, so it is a drop-in replacement:
`LOGIN_URL`, `redirect_to_login` and every `{% url 'login' %}` in your project keep
working unchanged. Swap the include back for `django.contrib.auth.urls` at any time.

Add [`LOGIN_IMAGE`](settings.md#branding) for a two-column sign-in page.

Vantage also provides 403, 404 and 500 handlers that render inside the panel shell,
with a bare fallback for when the shell itself cannot render.

## First look

Start the server and open `/admin/`. Without a line of configuration you have:

- a **dashboard** with a count card per model you can see, a chart of new records over
  time, and a recent-records table for each model that stamps a creation date;
- a **list page** per model, with inferred columns, a filter rail, a search box, sortable
  headers, a per-column summary bar and CSV/TSV/NDJSON export;
- **create, edit and delete** pages, with widgets inferred from the field types and a
  cascade-delete preview that counts what a delete would take with it;
- a **command palette** on <kbd>⌘K</kbd> / <kbd>Ctrl+K</kbd>, with global search across
  every model you have permission to open;
- a **schema map** at `/admin/_/schema/`, derived from the registry so it cannot go
  stale;
- a **system page** at `/admin/_/health/` for superusers;
- **light, dark and system themes**, and right-to-left layout when the active language
  is RTL.

### What gets registered

Every concrete, non-abstract model in `INSTALLED_APPS` is registered automatically,
minus framework plumbing nobody wants to browse: sessions, content types,
`auth.Permission`, `admin.LogEntry` and Vantage's own tables. `auth.User` and
`auth.Group` are deliberately kept — they are among the most useful things to
administer.

Narrow it with [`EXCLUDE` or `INCLUDE_ONLY`](settings.md#discovery), or switch
auto-registration off entirely with `AUTO_REGISTER = False` and register by hand.

## Configure one model

Create `panel.py` next to `models.py` in any app. Vantage imports it automatically at
startup, the same way `django.contrib.admin` imports `admin.py`.

```python
# blog/panel.py
from vantage import ModelPanel, register

from blog.models import Post


@register(Post)
class PostPanel(ModelPanel):
    icon = "file-text"
    group = "Content"
    list_display = ["title", "author", "status", "published_at"]
```

That overrides the columns. Filters, search fields, sort order and every form widget
are still inferred. See [Panels](panels.md) for the full attribute list and
[Inference](inference.md) for what the defaults are.

## See what was inferred

Inference is only trustworthy if you can look at it. `vantage_scaffold` renders the
engine's decisions as the Python you would have written:

```bash
python manage.py vantage_scaffold shop
```

Running that against the demo project prints, among others:

```python
@register(Order)
class OrderPanel(ModelPanel):
    list_display = [
        "number",
        "status",
        "customer",
        "shipping_city",
        "total",
        "created_at",
    ]
    list_link = "number"
    list_filter = ["status", "created_at", "customer"]
    list_search = ["number", "shipping_city", "note", "customer__name"]
    ordering = ["-created_at"]
```

Two flags:

| Flag | Effect |
|---|---|
| `--write` | Write `panel.py` into the app directory instead of printing it |
| `--force` | Overwrite an existing `panel.py` |

Once written, the file is ordinary code: readable in a diff, editable, and no longer
subject to inference changing under it on upgrade. Delete an attribute and it returns
to being inferred. Delete the whole file and the models stay registered.

## Check your configuration

Mistakes in a panel should fail at startup, not surface as a puzzling page.

```bash
python manage.py check
```

Vantage registers six system checks: one for the `VANTAGE` settings dict (unknown keys,
invalid choices), one for every registered panel (field paths that do not resolve, a
`list_link` outside `list_display`, a layout naming a field the model does not have),
one for the `NAV` declaration, one for [multi-tenancy](tenancy.md) (a model with no
path to the tenant and no exemption — fails closed, so this check exists to name every
model that would otherwise show nothing), one for plugins that failed to load, and one
for the `contrib.admin` adapter (settings an adopted `ModelAdmin` declared that could
not be translated). Panels left entirely at their inferred defaults are skipped — there
is no user-written configuration to be wrong.

A further set of checks only runs with `--deploy` — see [Security](security.md) and
[Deployment](deployment.md).

A typo in the settings dict is caught with a suggestion:

```
Unrecognised key(s) in the VANTAGE setting:
  'SITE_TTLE' is not a Vantage setting. Did you mean 'SITE_TITLE'?
```

## The demo project

The repository ships a complete demo — a small wholesale shop with seven models, two
hand-written panels and five left entirely alone, so you can compare them side by side.

```bash
./example/run.sh          # migrate, seed, and serve on :8765
```

Sign in as `admin` / `admin12345`.

`example/shop/panel.py` and `example/config/settings.py` are written to be read. They
are the shortest useful reference for most of what follows.

## Where next

- [Panels](panels.md) — every attribute you can set
- [Inference](inference.md) — what the defaults will be, and why
- [Lists](lists.md) — the list page in detail
- [Settings](settings.md) — project-wide configuration
