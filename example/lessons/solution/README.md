# shopdemo (solution)

The finished project after all lessons: the starter plus django-vantage, with lesson 6
covering the newer features.

## Run it

From the repository root, using the package from the checkout:

```bash
uv run python example/lessons/solution/manage.py migrate
uv run python example/lessons/solution/manage.py seed_shop
uv run python example/lessons/solution/manage.py seed_users
uv run python example/lessons/solution/manage.py runserver
```

Outside the repository, in a virtual environment of your own:

```bash
pip install --pre -r requirements.txt
python manage.py migrate && python manage.py seed_shop && python manage.py seed_users
python manage.py runserver
```

Open http://127.0.0.1:8000/admin/ and sign in as `admin` / `admin12345`
(superuser) or `support` / `support12345` (the limited "Support" role).
These are demo credentials for a local database only.

## What was added, by lesson

| Lesson | Files |
|---|---|
| 1. Install | `shopdemo/settings.py`, `shopdemo/urls.py`, `shop/admin.py` removed |
| 2. Model panels | `shop/panel.py`, `VANTAGE` in `shopdemo/settings.py` |
| 3. Stats and charts | `OrderPanel.list_stats` and `list_charts` in `shop/panel.py` |
| 4. Actions, inlines, read-only fields | `OrderItemInline`, `mark_paid`, `mark_shipped`, `readonly_fields` in `shop/panel.py` |
| 5. Permissions and dashboard | `has_delete_permission` in `shop/panel.py`, `shop/dashboard.py`, `shop/management/commands/seed_users.py` |
| 6. Rich text, export, 2FA, agents | `Product.details_html`, `agent_access` and `agent_hidden_fields` in `shop/panel.py`, `VANTAGE_LESSON_AGENTS` in settings |

## Lesson 6: newer features on the same shop

**Rich text.** `Product.details_html` is a plain `TextField`. Because its name ends in
`_html`, the panel edits it in the sanitised WYSIWYG editor with no configuration. Open a
product, put `<script>alert(1)</script>` in *Details* and save: the script is stripped.

**Export.** On any list, the export menu (or `?export=csv|tsv|json|xlsx|pdf`) downloads
the filtered rows. Try `/admin/shop/order/?status=paid&export=xlsx`.

**Two-factor sign-in.** Sign in as `admin`, open the account page and enrol an
authenticator app. Enrolment is per user and optional here.

**An opt-in AI agent.** Agent identities are off unless you set `VANTAGE_LESSON_AGENTS=1`.

```bash
export VANTAGE_LESSON_AGENTS=1
M="uv run python example/lessons/solution/manage.py"
$M seed_users
$M vantage_agent create --name catalogue-bot --user svc-catalogue --owner admin \
    --access write --models shop.product shop.customer
```

The command prints the token once (`vta_...`). The service user needs a role: add
`svc-catalogue` to the "Catalogue agent" group (created by `seed_users`) in the Users
page. `Product` panel has `agent_access = "write"`; `Customer` has
`agent_hidden_fields = ["email"]`, so the email never appears for an agent.

Call the API with the token. A dry run validates and describes the change and saves
nothing:

```bash
curl -H "Authorization: Bearer $VANTAGE_TOKEN" http://127.0.0.1:8000/admin/_/api/shop/customer/
curl -X PATCH "http://127.0.0.1:8000/admin/_/api/shop/product/1/?dry_run=1" \
    -H "Authorization: Bearer $VANTAGE_TOKEN" -H "Content-Type: application/json" \
    -d '{"fields": {"stock": 5}}'
```

Connect an MCP client over HTTP (`/admin/_/mcp/`) with the same token:

```json
{
  "servers": {
    "shop": {
      "type": "http",
      "url": "http://127.0.0.1:8000/admin/_/mcp/",
      "headers": {"Authorization": "Bearer vta_..."}
    }
  }
}
```

or over stdio, with no token, as the named identity:

```json
{
  "servers": {
    "shop": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "example/lessons/solution/manage.py", "vantage_mcp", "--agent", "catalogue-bot"],
      "env": {"VANTAGE_LESSON_AGENTS": "1"}
    }
  }
}
```

Keep tokens out of version control. See `docs/agents.md`, `docs/mcp.md` and
`docs/agent-writes.md`.
