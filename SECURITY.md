# Security policy

`django-vantage` is an admin panel. Every page it serves is behind authentication and
most of them read or write data the rest of the application is careful about, so a
defect here is a defect in someone's access control. Reports are taken seriously and
answered.

## Reporting a vulnerability

**Do not open a public issue.**

Report privately by either route:

- **Preferred —** [open a private advisory on GitHub](https://github.com/Pythondeveloper6/django-vantage/security/advisories/new).
  It creates a private thread with the maintainers and a place to draft the eventual
  disclosure and CVE.
- **Email —** <pythondeveloper6@gmail.com>, with `django-vantage security` in the
  subject line.

Please include, as far as you have it:

- the version of `django-vantage`, Django and Python;
- what an attacker gains — the specific data read, written or destroyed, and by whom;
- the smallest reproduction you can manage. A failing test against `tests/testapp` is
  ideal; a panel definition plus a request is fine;
- whether the deployment needs anything unusual to be vulnerable (a particular setting,
  an optional integration such as django-guardian, a specific permission backend).

## What to expect

| | |
| --- | --- |
| Acknowledgement | within **3 working days** |
| Initial assessment (is it a vulnerability, and how severe) | within **7 days** |
| Fix or a dated plan | within **30 days** for high and critical, best effort otherwise |
| Public disclosure | after a fix ships, coordinated with you |

This is a small project maintained by volunteers, not a vendor with an on-call rota. If
you have not heard back in a week, please chase — it means the message was missed, not
ignored.

You will be credited in the advisory and the changelog by whatever name you prefer, or
not at all if you would rather. There is no bug bounty.

## Supported versions

Security fixes land on `main` and are released as a new patch of the most recent minor
version. Older minors are not backported.

| Version | Supported |
| --- | --- |
| 0.1.x (pre-release) | ✅ current development line |
| < 0.1 | ❌ never released |

**The package has not had a stable release yet.** While the version is below `1.0` there
is exactly one supported line — the latest release — and no long-term support branch.
That policy will be replaced with a real one at 1.0; until then, run the newest version.

### Supported dependencies

A report is in scope if it reproduces on any combination the project claims to support:

| | Supported |
| --- | --- |
| Python | 3.12, 3.13, 3.14 |
| Django | 5.2 LTS, 6.0, 6.1 |

Django versions that upstream no longer supports are out of scope here too; see the
[Django supported versions](https://www.djangoproject.com/download/#supported-versions)
page. Vulnerabilities in Django itself go to <security@djangoproject.com>, not here.

## Scope

**In scope** — anything where the panel does something the permission layer should have
prevented:

- a user seeing, editing, deleting or exporting a record they hold no permission for;
- a bulk action, saved view, autocomplete, import or export that escapes its permission
  or row-level scoping;
- a value that reaches the ORM, `order_by`, a template or a redirect without validation
  — SQL injection, XSS, template injection, open redirect;
- sensitive field values (password, token, secret, api_key…) appearing in a column,
  filter, search index, export, audit diff or error page;
- CSRF on a state-changing endpoint, or a missing `Cache-Control: no-store` on a
  permission-scoped response;
- impersonation, read-only mode or the audit log being bypassable or forgeable;
- a denial of service reachable by an ordinary authenticated user — an unbounded query,
  an unbounded import, an unpaginated relation.

**Out of scope:**

- anything that requires superuser access to exploit. A superuser can already edit the
  site configuration, impersonate, and read the health page; that is the role, not a
  vulnerability. The exception is a *lateral* superuser issue, such as impersonating
  another superuser, which is explicitly prevented and is in scope if it isn't.
- a project that has deliberately granted a permission and is surprised by what it
  allows;
- running with `DEBUG = True`, which leaks by design and is Django's behaviour, not
  this package's;
- missing security headers a host project is expected to set (HSTS, CSP) — though a
  report that the panel *cannot function* under a strict CSP is a real bug and welcome
  as an ordinary issue;
- findings from an automated scanner with no demonstrated impact.

## Hardening notes

Two settings are worth knowing about before you need them:

- `VANTAGE["READ_ONLY"]` is enforced in the permission layer rather than in the views,
  so it denies every current and future write path at once. A string value doubles as
  the banner text explaining why.
- Impersonation is superusers only, never onto another superuser, refused for inactive
  accounts, and logged at warning level on both entry and exit.
