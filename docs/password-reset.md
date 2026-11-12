# Password reset

An opt-in "Forgot password?" flow on the sign-in page. It uses Django's own
`PasswordResetTokenGenerator` and your project's email backend: no new tables, no
second token scheme. It is off by default; until you enable it the routes are 404 and
the sign-in page shows no link.

## Enabling it

```python
VANTAGE = {
    "PASSWORD_RESET": True,
    "PASSWORD_RESET_TIMEOUT_MINUTES": 60,                 # default; minimum 5
    "PASSWORD_RESET_BASE_URL": "https://panel.example.com",  # recommended
}

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"  # your mail setup
DEFAULT_FROM_EMAIL = "panel@example.com"
```

The routes live under the panel's own prefix, for a panel mounted at `admin/`:
`admin/_/password-reset/` (request), `.../done/`, `.../confirm/<uidb64>/<token>/` and
`.../complete/`. Reverse them as `vantage:password_reset`, `vantage:password_reset_done`,
`vantage:password_reset_confirm` and `vantage:password_reset_complete`. In development,
use `django.core.mail.backends.console.EmailBackend` and read the link from the terminal.

## Who gets an email

Only accounts that can sign in to the panel with a password: active, `is_staff`, a usable
password and an email address. Everyone else gets the same on-screen answer and no
email. **Accounts that sign in only through an identity provider** (see
[Single sign-on](single-sign-on.md)) have no usable password and are not reset here;
their password belongs to the provider.

## Customising the email

Override `vantage/password_reset_email.txt` (body) and `vantage/password_reset_subject.txt`
(subject, collapsed to one line) in your templates. Context: `user`, `username`, `link`,
`site_title`, `valid_minutes`. The sender is `DEFAULT_FROM_EMAIL`; the backend is yours.
The pages are `password_reset.html`, `password_reset_done.html`,
`password_reset_confirm.html` and `password_reset_complete.html` under `vantage/`.

## Security properties

* **No user enumeration.** The request form redirects to the same page whether or not
  the address has an account, is ineligible, or was rate limited. The email is sent on a
  background thread, so a slow mail server does not make a real address slower to answer.
* **Rate limits** through the existing `RATE_LIMITS` machinery (and so `RATE_LIMIT`,
  `RATE_LIMIT_CACHE`): `password_reset` (20 per hour per caller address),
  `password_reset_address` (3 per hour per target address) and `password_reset_confirm`
  (30 per hour per caller, answered with `429`). Over a request limit nothing is sent and
  the answer does not change. Override like any other surface; set one to `None` to turn it off.
* **Single use, short lived.** The token hashes the password and `last_login`, so using
  it, changing the password or signing in voids it. Tokens use their own salt (Django's
  own reset tokens are not accepted) and Django's constant-time comparison. Lifetime is
  `PASSWORD_RESET_TIMEOUT_MINUTES`, independent of `PASSWORD_RESET_TIMEOUT`.
* **The token leaves the address bar.** The first visit stores it in the session and
  redirects to `.../confirm/<uid>/set-password/`. That redirect sends
  `Referrer-Policy: no-referrer`; every page here sends `same-origin` (a browser posts
  `Origin: null` under `no-referrer`, which CSRF protection refuses, so the forms cannot
  use it) and `Cache-Control: no-store`. The form pages carry no token in their URL.
* **Links point at your host.** With `PASSWORD_RESET_BASE_URL` set, links use it.
  Otherwise they use the request host as validated against `ALLOWED_HOSTS`
  (`X-Forwarded-Host` only if you set `USE_X_FORWARDED_HOST`); the host is checked
  before any lookup, so a bad `Host` fails the same for every address. A wildcard
  `ALLOWED_HOSTS` without a base URL raises `vantage.W117` under `check --deploy`.
* **Password rules apply.** The new password is validated with `AUTH_PASSWORD_VALIDATORS`.
* **Sessions end.** Setting the password changes the session hash, so every other
  session of that account is signed out. The reset itself signs nobody in.
* **Two-factor is not bypassed.** The reset changes the password and nothing else. The
  authenticator stays enrolled and the next sign-in still asks for a code; a half-finished
  sign-in is dropped.
* **Audit.** "Password reset requested" and "Password reset completed" entries name the
  account and the caller's address (`REMOTE_ADDR`). Tokens and passwords are never recorded.

## Read-only mode and tenancy

Pages work under `READ_ONLY`: a reset writes the account's own password, not a record in
a panel model, and a locked-out person must still be able to recover. Accounts are
not tenant-scoped, so tenancy does not apply. Impersonation does not apply: the pages
are for signed-out visitors.

## Not covered

Translations: the strings are marked for translation, but the Arabic catalogue has not
been refreshed for this feature yet, so they appear in English until it is.
