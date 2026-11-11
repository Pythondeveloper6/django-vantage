# Lesson projects

Two small, runnable Django projects for learning django-vantage on a coffee-shop back
office (categories, products, customers, orders, order items).

| Folder | What it is |
|---|---|
| `starter/` | Plain Django admin. You add django-vantage to it, one lesson at a time. |
| `solution/` | The finished project after the lessons, plus lesson 6 (newer features). |

## The lessons

1. **Install.** Add `vantage` to `INSTALLED_APPS`, mount `site.urls` and `vantage.authviews`,
   drop `admin.py`. Every model gets a working panel with no configuration.
2. **Model panels.** A `ModelPanel` in `shop/panel.py` sets columns, filters and search,
   plus sidebar icon and group.
3. **Stats and charts.** `list_stats` and `list_charts` put live, filter-aware numbers and
   charts above the Order list.
4. **Actions, inlines, read-only fields.** Bulk "Mark as paid/shipped" with undo, order
   items edited inline on the order page, `readonly_fields`.
5. **Permissions and dashboards.** Roles, `has_delete_permission`, and a custom
   `Dashboard` built from stat, chart, table and list cards.
6. **Newer features (solution only).** A sanitised rich-text field, Excel and PDF export,
   two-factor enrolment, and an opt-in agent identity with the JSON API and MCP.

## Run a lesson from this checkout

The commands use the package from the repository, not PyPI. From the repository root:

```bash
uv run python example/lessons/starter/manage.py migrate
uv run python example/lessons/starter/manage.py seed_shop
uv run python example/lessons/starter/manage.py createsuperuser
uv run python example/lessons/starter/manage.py runserver

uv run python example/lessons/solution/manage.py migrate
uv run python example/lessons/solution/manage.py seed_shop
uv run python example/lessons/solution/manage.py seed_users
uv run python example/lessons/solution/manage.py runserver
```

`solution` signs in as `admin` / `admin12345` or `support` / `support12345` (local demo
credentials only). Each project keeps its own `db.sqlite3`, which is git-ignored.

Outside the repository, `requirements.txt` in each folder installs Django and the
pre-release package: `pip install --pre -r requirements.txt`.
