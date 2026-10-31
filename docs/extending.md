# Extending Vantage

Three extension points, in the order you are likely to need them.

| You want to | Use |
|---|---|
| Put your own HTML on a Vantage page | [Slots](#slots) |
| Ship an extension as an installable package | [Plugins](#plugins) |
| Keep the `admin.py` you already have | [The `contrib.admin` adapter](#the-contribadmin-adapter) |

---

## Slots

A slot is a named place in a Vantage template that you can fill from Python. It exists
so that adding one banner does not mean overriding — and then maintaining — a whole
template.

### The eight slots

| Name | Where |
|---|---|
| `before_list` / `after_list` | above / below the grid on a list page |
| `before_form` / `after_form` | above / below a create or edit form |
| `object_actions` | the button row on a detail page |
| `row_actions` | a trailing cell on every list row |
| `detail_sidebar` | beside the fields on a detail page |
| `dashboard_top` | above the dashboard cards |

### Filling one

Anywhere that runs at startup — `panel.py` is the natural home:

```python
# blog/panel.py
from django.utils.html import format_html

from vantage import slots

# A template fragment.
slots.add("before_list", template="blog/_import_banner.html")

# Or a callable. It receives the template context as a plain dict.
@slots.slot("object_actions")
def preview_button(context):
    post = context["object"]
    return format_html('<a class="v-btn v-btn-ghost" href="{}">Preview</a>', post.get_absolute_url())
```

Contributions render in registration order. Pass `order=-10` to sit ahead of everything
registered by someone else — that is how a plugin gets in front of project code without
either importing the other.

### Escaping

A callable's return value is escaped unless you marked it safe:

```python
slots.add("before_list", render=lambda ctx: "<b>hi</b>")            # renders &lt;b&gt;hi&lt;/b&gt;
slots.add("before_list", render=lambda ctx: format_html("<b>hi</b>"))  # renders <b>hi</b>
```

A `template=` contribution is not escaped again — the template engine already escaped
its own variables on the way through.

### Permissions

```python
slots.add("row_actions", template="blog/_retry.html", permission="blog.change_post")
slots.add("dashboard_top", render=beta_banner, permission=lambda request: request.user.is_staff)
```

A string goes to `request.user.has_perm`; a callable receives the request. A gated
contribution renders nothing when there is no request in context — it fails closed.

### Scoping to one model

```python
slots.add("before_list", template="blog/_post_help.html", model=Post)
```

Or, for content that belongs with the panel, implement the hook instead:

```python
@register(Post)
class PostPanel(ModelPanel):
    def slot(self, name, context):
        if name == "detail_sidebar":
            return render_to_string("blog/_seo_card.html", context)
        return None
```

The panel's own content renders first, then the registry's.

### Failure

One contribution raising does not take the page down: the exception is logged to the
`vantage.slots` logger and that fragment is skipped. Its siblings still render.

### Using a slot in your own template

If you override a Vantage template and want a slot of your own:

```python
slots.declare("my_sidebar")
```

```django
{% load vantage_slots %}
{% slot "my_sidebar" %}

{# For markup that should only exist when the slot has content: #}
{% slot_filled "row_actions" as has_actions %}
{% if has_actions %}<td>{% slot "row_actions" object=row.obj %}</td>{% endif %}
```

Undeclared names are rejected at registration, because a mistyped slot name is
otherwise invisible — the fragment simply never appears.

---

## Plugins

A plugin is a package that extends the panel by being installed.

### Writing one

```python
# vantage_celery/__init__.py
from vantage import slots
from vantage.plugins import VantagePlugin
from vantage_celery.pages import QueuePage


class CeleryPlugin(VantagePlugin):
    name = "celery"
    version = "1.2.0"

    def register(self, site):
        site.register_page(QueuePage)
        slots.add("dashboard_top", template="vantage_celery/_queue_depth.html")
```

```toml
# vantage_celery/pyproject.toml
[project.entry-points."vantage.plugins"]
celery = "vantage_celery:CeleryPlugin"
```

`pip install vantage-celery` is now the whole installation. No settings change, no
`INSTALLED_APPS` entry.

`register(site)` runs inside `AppConfig.ready()`, so it must not touch the database — a
query there breaks `migrate` on a fresh install.

An entry point may also point at an instance, or at a bare function taking the site:

```python
def setup(site):
    site.register_page(QueuePage)
```

### Registering one by hand

For a plugin you vendor, or if you would rather see your extensions written down:

```python
# project/panel.py
from vantage.plugins import register_plugin
from vantage_celery import CeleryPlugin

register_plugin(CeleryPlugin)
```

This loads immediately, and is a no-op if the same plugin was already discovered — so
doing both is harmless.

### Turning one off

```python
VANTAGE = {
    "PLUGINS": ["celery", "audit"],   # allowlist; None (the default) loads everything
    "PLUGINS_EXCLUDE": ["celery"],    # denylist, and it wins
}
```

Names match either the entry-point name or the plugin's own `name`. An excluded plugin
is never imported, which is what makes the denylist useful when the reason for
switching it off is that importing it fails.

### When a plugin is broken

Nothing is fatal. A plugin that fails to import, or whose `register` hook raises, is
skipped and reported by `manage.py check`:

```
WARNINGS:
?: (vantage.E030) The Vantage plugin 'celery' failed to load and was skipped.
    HINT: ImportError: No module named 'kombu'
    Declared at vantage_celery:CeleryPlugin.
    Fix the plugin, or add 'celery' to VANTAGE['PLUGINS_EXCLUDE'] to run without it.
```

The alternative — raising in `ready()` — means no shell, no `migrate`, and no way to
uninstall the thing that broke.

Loading is idempotent, so a second `ready()` (the autoreloader, a test that reconfigures
settings) does not give you two copies of every page.

---

## The `contrib.admin` adapter

Import an existing `ModelAdmin` and get a working panel. Your `admin.py` does not
change, and both panels keep working side by side.

### Everything at once

```python
# project/panel.py
from vantage import site
from vantage.contrib.django_admin import adopt_admin_site

adopt_admin_site(site)
```

Every model registered with `django.contrib.admin` now has a Vantage panel. Models you
have already given a hand-written `ModelPanel` are left alone; pass `replace=True` if
you want the admin to win instead.

Call it from `panel.py` so it runs before auto-registration.

### One model at a time

```python
from vantage import ModelPanel, register
from vantage.contrib.django_admin import from_admin
from blog.admin import PostAdmin
from blog.models import Post


@register(Post)
@from_admin(PostAdmin)
class PostPanel(ModelPanel):
    icon = "file-text"        # anything you declare here wins
    list_per_page = 100
```

If `PostAdmin` is not registered with `contrib.admin`, name the model:
`@from_admin(PostAdmin, Post)`.

### What translates

| `ModelAdmin` | Becomes |
|---|---|
| `list_display` | `list_display` — including methods on the admin, and `__str__` |
| `list_display_links` | `list_link` (the first entry) |
| `list_filter` | `list_filter` (field entries) |
| `search_fields` | `list_search` |
| `ordering`, `list_per_page` | `ordering`, `list_per_page` |
| `list_select_related` | `list_select_related` (`True` gives way to Vantage's own planner) |
| `list_editable` | `list_editable` |
| `fields`, `exclude`, `readonly_fields` | the same, with `fields` flattened |
| `fieldsets` | a `Section` / `Row` layout, `classes=("collapse",)` included |
| `date_hierarchy` | a date filter on that field |
| `actions` | Vantage actions, with `short_description` and `permissions` |
| `has_view/add/change/delete_permission` | the same methods on the panel |
| `get_queryset`, `save_model` | delegated to the admin — the signatures match |
| `get_readonly_fields`, `get_exclude`, `get_fields` | per-request **read-only** field rules through the panel's `get_field_access(request)`, asked with `obj=None` |

Only what the admin actually *declared* is translated. An attribute left at its
`ModelAdmin` default stays unset on the panel, so Vantage's inference still runs:
adopting an admin that sets only `search_fields` does not cost you inferred columns,
filters and widgets.

`has_*_permission` is consulted *before* the superuser bypass, matching
`contrib.admin`: an admin that denies deletion denies it to everyone.

### What does not translate

Nothing is dropped silently. Each of these is reported per model by `manage.py check`
as `vantage.W040`:

- **`inlines`** — not translated automatically; `ModelAdmin.InlineModelAdmin` classes
  do not carry across. Vantage has its own, differently-shaped mechanism —
  [`ModelPanel.inlines`](forms.md#editable-inlines) — but it is declared by hand on the
  new panel rather than inferred from the adopted `ModelAdmin`.
- **`formfield_overrides`** — Vantage infers widgets from the field.
- **`raw_id_fields`, `autocomplete_fields`, `filter_horizontal`, `filter_vertical`,
  `radio_fields`, `prepopulated_fields`** — widget choices; Vantage picks its own.
- **`get_urls()`** — register a `Page` on the Vantage site instead.
- **`Media`** — use `VANTAGE["CUSTOM_CSS"]`.
- **`form`** and the custom templates (`change_form_template` and friends).
- **The view and form hooks** — `get_form`, `get_fieldsets`, `get_list_display`,
  `get_search_results`, `get_actions`, `changelist_view`, `change_view`, `response_add`,
  `formfield_for_foreignkey`, `lookup_allowed`, and the rest: behaviour that lives in
  Python the adapter cannot read.
- **`SimpleListFilter` and list-filter classes** — the *field* stays filterable with
  Vantage's own control; the custom class does not come across.
- **`short_description` on a display method** — the column header falls back to the
  method name.
- **Computed `readonly_fields`** — excluded from the form, but not displayed.
- **A custom action permission** (`@admin.action(permissions=["approve"])`) — the action
  falls back to requiring `change`, and says so.
- **Cosmetics** — `save_as`, `save_on_top`, `view_on_site`, `empty_value_display`,
  `list_max_show_all`, `show_full_result_count`, `actions_on_top`/`_bottom`, `paginator`,
  `sortable_by`.

```
WARNINGS:
<class 'PostAdminPanel'>: (vantage.W040) PostAdmin was adopted for blog.Post, but
4 setting(s) could not be translated.
    HINT: Not carried across:
      · inlines (inline formsets — edit the related model from its own page, …)
      · raw_id_fields (raw id inputs — Vantage uses a searching select for relations)
      · get_urls() (extra URLs hung off the admin — register a Page on the Vantage site)
      · Media (extra CSS/JS — add it with VANTAGE['CUSTOM_CSS'])
```

### `contrib.admin` is optional

Nothing in the core imports it. `adopt_admin_site()` with no arguments soft-detects with
`apps.is_installed("django.contrib.admin")` and does nothing — loudly, in the log — when
it is absent. Passing an `AdminSite` explicitly skips the detection, which is what you
want for a custom admin site:

```python
from myproject.admin import staff_site

adopt_admin_site(site, admin_site=staff_site)
```

---

## Settings reference

```python
VANTAGE = {
    "PLUGINS": None,          # None = load every discovered plugin; a list = allowlist
    "PLUGINS_EXCLUDE": [],    # never loaded, whatever PLUGINS says
}
```

## Check IDs

| ID | Meaning |
|---|---|
| `vantage.E030` | A plugin failed to load and was skipped |
| `vantage.W030` | `VANTAGE["PLUGINS"]` names a plugin nothing provides |
| `vantage.W040` | A `ModelAdmin` was adopted with settings that could not be translated |
| `vantage.E041` | An adopted `ModelAdmin` restricts access with a hook the adapter cannot translate (`formfield_for_foreignkey`, `formfield_for_manytomany`, `formfield_for_choice_field`, `get_form`, `has_module_permission`). Restrict the same thing on the panel, then add the ID to `SILENCED_SYSTEM_CHECKS` |
