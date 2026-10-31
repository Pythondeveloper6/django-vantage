# Migrating from django.contrib.admin

[← Index](index.md)

A project with a hundred models and years of `admin.py` should not switch in one
deploy. The path below keeps both panels running, moves one batch of models at a time,
and leaves the old admin in place until you choose to remove it. Nothing here edits
your `admin.py`.

## 1. Run both side by side

Install Vantage and mount it on a different URL prefix from the admin. Both read the
same database, the same users and the same permissions.

```python
# urls.py
urlpatterns = [
    path("admin/", admin.site.urls),        # the panel you have today
    path("panel/", vantage_site.urls),      # Vantage, alongside it
]
```

Add `"vantage"` to `INSTALLED_APPS`, run `python manage.py migrate` (Vantage keeps its
own few tables) and sign in at `/panel/`. At this point every model that is not yet
registered with Vantage is absent from it. That is expected.

## 2. Read the report

```bash
python manage.py vantage_adopt
```

Every `ModelAdmin` registered on `django.contrib.admin.site` is translated in memory
and reported, per model and per attribute, as one of:

| Status | Meaning |
|---|---|
| `translated` | Carried across, or covered by something Vantage does by default (the line says which). |
| `needs panel code` | Vantage can do it, but it is Python (a column method, an action, a queryset hook) that has to be ported by hand. |
| `unsupported` | Nothing in Vantage does this today. The line gives the reason and the nearest alternative when there is one. |

Useful options:

```bash
python manage.py vantage_adopt --app orders          # one app at a time
python manage.py vantage_adopt --format json > report.json
python manage.py vantage_adopt --admin-site myproject.admin.custom_site
python manage.py vantage_adopt --strict              # exit status 1 if anything is unsupported
```

`--strict` is meant for CI once you have decided what is acceptable: run it on the apps
you have already migrated so a new unsupported setting cannot slip in unnoticed.

Read the `unsupported` rows first. They are the decisions: drop the feature, replace it
with the Vantage equivalent shown, or keep that model in the old admin for now.

## 3. Translate in batches

Pick a batch of models, ideally a whole app that few people depend on to begin with.

**Option A: adopt at runtime.** Fastest way to see the result:

```python
# panel.py
from vantage import site
from vantage.contrib.django_admin import adopt_admin_site

adopt_admin_site(site)
```

Every registered `ModelAdmin` gets a panel, and `manage.py check` lists what could not
be translated (`vantage.W040`). A panel you have already written by hand wins over the
adopted one. See [Extending](extending.md).

**Option B: write the panels out.** Recommended for the long run, because the file is
yours and no longer reads `admin.py`:

```bash
python manage.py vantage_adopt --app orders --write
```

This writes a starter `panel.py` into the app (or into `<dir>/<app>/panel.py` with
`--output-dir`). It never overwrites an existing file unless you pass `--force`. Apps
that are installed packages are skipped unless you give `--output-dir`. Generated files
contain only attributes the adapter translates; everything it could not carry is a
`TODO(vantage_adopt)` comment at the end of the class, for example:

```python
@register(Order)
class OrderPanel(ModelPanel):
    list_display = ["number", "status"]
    list_search = ["number"]
    # TODO(vantage_adopt) action mark_shipped: port it with @action(...)
```

Work through the TODO comments, then delete them. Run `python manage.py check` after
each batch.

## 4. Permissions carry over

Vantage uses Django's own permission system: the same `auth.Permission` rows, groups and
`has_perm` calls the admin uses, so a person who could change orders in the admin can
change them in Vantage and nobody else can. `has_*_permission` methods on a
`ModelAdmin` are translated to the same hooks on the panel and are consulted before the
superuser bypass, so an admin that refuses deletion to everyone still refuses it. See
[Permissions](permissions.md).

Check these two things per batch:

- A custom permission backend (django-guardian, django-rules) works through
  `AUTHENTICATION_BACKENDS`. Guardian's row filtering needs one setting; see
  [Permissions](permissions.md#third-party-backends).
- Anything gating on `is_staff` is unchanged, since the panel gates on it too.

## 5. Cut over

When a batch behaves, tell the people who use it. Cut over by changing the URL
prefixes: point `/admin/` at Vantage and move the old admin to a prefix only developers
use, or keep it for the models you have not migrated.

```python
urlpatterns = [
    path("panel/", vantage_site.urls),          # was "admin/"
    path("legacy-admin/", admin.site.urls),
]
```

Bookmarks to `/admin/<app>/<model>/` will break when the prefix changes. If that matters,
add redirects for the models you migrated. Do the cut-over for one batch (or one team)
at a time, not all at once.

## 6. Rollback plan

Because Vantage never modifies `admin.py` or the admin's tables, going back is a URL
change:

1. Put the old admin back on its original prefix.
2. Remove the `vantage` URL include (or leave it mounted on the secondary prefix).

What does not roll back is data written through Vantage. It is ordinary rows in your own
tables, but Vantage also keeps its own records (history, saved views, roles, audit log)
in its own tables, which the old admin does not read. Leave the `vantage` app installed
until you are sure; uninstalling it later is a separate migration step. Pin the version
(`django-vantage==<version>`) while migrating, because the API may still change before
1.0.

## Checklist per batch

- [ ] `vantage_adopt --app <label>` reviewed; unsupported items decided.
- [ ] `panel.py` written or adopted; TODO comments resolved.
- [ ] `manage.py check` clean for the app.
- [ ] Each action, filter and permission rule tried by someone who uses it.
- [ ] Third-party admin plugins checked against [Plugin compatibility](plugin-compatibility.md).
- [ ] Cut over (or deferred) and the rollback prefix confirmed to work.
