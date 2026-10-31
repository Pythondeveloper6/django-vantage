# API stability and deprecations

Vantage is pre-1.0, but "pre-1.0" is not a licence to rename things under you. This
page is the contract: what is frozen, what may still move, how a change is announced,
and the command that finds every affected line in your project before you upgrade.

## What is public

The public API is:

- every name in `vantage.__all__` (`ModelPanel`, `register`, `site`, `VantageSite`,
  `Stat`, `Chart`, `Dashboard`, `Page`, the layout nodes, the inlines and `action`);
- every **declarative `ModelPanel` attribute** and every `get_*` / `has_*_permission`
  hook documented in [Panels](panels.md);
- every key of the **`VANTAGE` setting** documented in the
  [Settings reference](settings.md);
- the management commands, their options and their exit codes;
- the JSON API's URLs, request and response shapes ([The JSON API](api.md));
- the template slot names ([Extending](extending.md)) and the `vantage.plugins`
  entry-point group.

Anything else — a module-level function, a private `_name`, a template's internal
markup, a CSS class — is an implementation detail and can change in any release.

## Stable and provisional

Every setting and every declarative panel attribute is classified, in code, in
`vantage.deprecation`:

- **Stable** — shipped in a release. Frozen: it is not renamed, removed or given a
  different meaning without going through the deprecation cycle below.
- **Provisional** — added since the last release. It may still change before the
  *next* release; the release after the one that first ships it makes it stable.

The test suite fails when a setting or attribute is added without being classified, so
the public surface cannot grow by accident. `vantage_upgrade_check` reports every
provisional name a project uses (`U008`, informational).

Provisional today: the two-factor (`TWO_FACTOR*`), webhook (`WEBHOOKS*`) and scheduled
report (`REPORTS*`) settings, and the panel attributes `field_permissions`, `history`,
`requires_approval`, `formfield_overrides` and `widgets`.

## The deprecation cycle

1. **Deprecated.** The release that decides to change a stable name keeps the old one
   working, adds it to `vantage.deprecation.DEPRECATED_SETTINGS` or
   `DEPRECATED_PANEL_ATTRIBUTES`, and emits `VantageDeprecationWarning` (a
   `DeprecationWarning` subclass) where it is used. A renamed setting or attribute is
   translated to its new name for the whole period, so nothing breaks.
2. **Announced.** The change is listed under **Deprecated** in the
   [changelog](https://github.com/Pythondeveloper6/django-vantage/blob/main/CHANGELOG.md)
   with its replacement and the version it will be removed in.
3. **Removed** no earlier than **two minor releases later** (before 1.0: `0.3` deprecates,
   `0.5` removes at the earliest; after 1.0: never within a major version). A removed
   panel attribute fails `manage.py check` (`vantage.E020`) rather than being ignored.

Exceptions: a security fix may change behaviour in any release, and says so under
**Security** in the changelog. Database migrations are additive within a minor version.

To make deprecations fail your test suite instead of scrolling past:

```ini
# pytest.ini / pyproject.toml [tool.pytest.ini_options]
filterwarnings =
    error::vantage.deprecation.VantageDeprecationWarning
```

## Checking a project before an upgrade

```console
$ python manage.py vantage_upgrade_check --from 0.2.0a2
django-vantage 0.2.0a2 upgrade check
[error] U001 VANTAGE['SITE_TITEL'] is not a setting this version recognises; the site will refuse to start.
    Did you mean 'SITE_TITLE'?
[info] U008 VANTAGE['WEBHOOKS'] is provisional: it may still change before the next release.
    See docs/api-stability.md.

Upgrading to next:
  - Run `manage.py migrate vantage` (migrations 0016 onwards: ...)
```

Run it after bumping the version in a branch, and in CI on every build. It exits `1`
when it finds anything at or above `--fail-level` (`error` by default).

| Option | Meaning |
|---|---|
| `--from VERSION` | Print the upgrade notes for every release since `VERSION` |
| `--format text\|json` | Human-readable, or one JSON document for tooling |
| `--fail-level info\|warning\|error` | The lowest level that makes the exit status `1` |
| `--database ALIAS` | Whose migration state to check (default `default`) |
| `--skip-database` | Configuration checks only; never connects |

| Code | Level | Meaning |
|---|---|---|
| `U001` | error | A `VANTAGE` key this version does not know — the site would refuse to start |
| `U002` | warning | A deprecated setting, with its replacement and removal version |
| `U003` | warning | A deprecated panel attribute |
| `U004` | error | A removed panel attribute is still set |
| `U005` | error | Vantage migrations not applied to the database |
| `U006` | error | The installed Django is outside the tested range |
| `U007` | error/warning | A panel configuration problem `manage.py check` would report |
| `U008` | info | A provisional setting or attribute is in use |
