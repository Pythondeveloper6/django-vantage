# django-vantage demo

A working Django project that installs the panel and points it at a small wholesale
shop. Nothing here is part of the package — it exists so you can look at the thing
running, with enough data in it that the tables, filters and pagination have something
to do.

## Run it

From the **repository root**:

```bash
./example/run.sh
```

Then open <http://127.0.0.1:8765/admin/>.

Or step by step, if you would rather see what that script does:

```bash
uv run python example/manage.py migrate
uv run python example/manage.py seed
uv run python example/manage.py runserver 8765
```

Both the migration and the seed are idempotent, so re-running either is harmless. To
start completely clean, delete `example/db.sqlite3` and run them again.

## Check that it works

```bash
uv run pytest tests/test_example_project.py
```

boots the demo against a throwaway database (`VANTAGE_DEMO_DB`, `VANTAGE_DEMO_MEDIA`),
runs `check`, `migrate` and `seed`, and loads the panel as the seeded admin.

## Sign in

| Username | Password      | Role                       |
| -------- | ------------- | -------------------------- |
| `admin`  | `admin12345`  | superuser (admin@example.com) |
| `editor` | `editor12345` | staff only                 |

The panel gates on `is_staff`, so both accounts get in and see the same thing — the
second one is there for when permissions land.

## What the seed creates

Deterministic, from a fixed RNG seed, so two people looking at this demo are looking at
the same rows:

- 40 customers across six countries, four of them archived
- 25 products in 9 categories (three parents, six leaves), tagged from a pool of 8
- 200 orders spread over the last 180 days, with 500-odd order items
- 60 reviews

## What to look at

**The sidebar groups.** `Commerce` holds Orders and Catalogue because
`example/shop/panel.py` says so. `Shop` holds everything else because that is the app's
verbose name and nobody said otherwise. `Authentication and Authorization` shows up
without anyone asking — Users and Groups are administrable out of the box.

**Two configured models, five inferred ones.** `panel.py` writes out `Order` and
`Product` by hand. `Customer`, `Category`, `Tag`, `OrderItem` and `Review` have no panel
class at all. Compare them; the inferred ones are not obviously the poor relations.

**`ProductPanel` sets `list_display` and nothing else** — and still has filters
(status, featured, created, category) and search (SKU, name, description, and one hop
into `category__name`). Overriding one attribute does not cost you the rest.

**Two columns that are missing on purpose.** `Customer.api_token` never appears in the
columns, the filters or the search, because a field name matching `token` is treated as
a credential and rendering one into a table is a security bug. `Customer.deleted_at` is
also absent from the columns, having simply lost to higher-signal fields — but note it
is not offered as the date filter either: `joined_on` is, because inference prefers a
record's own date over an audit stamp.

**Search reaches one hop.** Search Orders for `Amelia` and you get that customer's
orders, matched through `customer__name`, which is what a human means by search.

**Branding.** `VANTAGE` in `config/settings.py` sets one `BRAND_COLOR`; the whole
palette, light and dark, is derived from it. The red banner across the top is
`ENVIRONMENT`. Hit the sun/moon button in the header to switch themes.

## Layout

```
example/
  config/          settings, urls (mounts the panel at /admin/), wsgi
  shop/
    models.py      the demo models
    panel.py       two explicit panels; everything else left to inference
    management/commands/seed.py
  templates/
    registration/login.html    minimal login page, reusing the panel's stylesheet
  run.sh
```

`config/urls.py` also carries a tiny `LoginView` subclass. It exists only to hand the
login template the panel's theme tokens so the two pages match; the panel itself does
not require it.
