# Two-factor sign-in

After the password, a six-digit code from an authenticator app on the person's phone —
Google Authenticator, Microsoft Authenticator, 1Password, Bitwarden, or any other app
that does time-based codes (TOTP, [RFC 6238](https://www.rfc-editor.org/rfc/rfc6238)).
A leaked or guessed password is then not enough on its own.

It needs no configuration and no extra package. Everyone with panel access can switch it
on for themselves from their account page; you can require it of superusers, or of
chosen [roles](roles.md), with one setting.

```bash
python manage.py migrate   # two small tables: the authenticators, and the recovery codes
```

## Setting it up

From **Your account → Set up two-factor sign-in** (`<panel>/_/account/two-factor/`):

1. **Set it up** shows a QR code, the same key written out for typing in by hand, and an
   `otpauth://` link for an authenticator app on the same device.
2. Scan it, then type the code the app shows. Nothing is switched on until a code proves
   the app really has the key — a QR code that was never scanned cannot lock anyone out.
3. Ten **recovery codes** are shown, once. Each signs in once without the phone. Keep them
   in a password manager.

From then on, signing in through the panel's own sign-in page (`vantage.authviews`)
asks for a code after the password. One box takes either an app code or a recovery code.

Turning it off, or replacing the recovery codes, asks for a current code (or a recovery
code) or the password first.

The QR code is drawn by the package itself (`vantage.qr`), as inline SVG, black on white
in both themes — a phone camera reads that reliably, and the inverted version unreliably.

## Requiring it

```python
VANTAGE = {
    "TWO_FACTOR_REQUIRED_SUPERUSERS": True,
    "TWO_FACTOR_REQUIRED_ROLES": ["Finance", "Support leads"],
}
```

Someone it applies to who has not set it up is sent to the set-up page before anything
else in the panel: every other page, the JSON API and autocomplete refuse until they have.
The requirement takes effect on sessions that are already signed in, not only at the
next sign-in.

| Key | Default | Effect |
|---|---|---|
| `TWO_FACTOR` | `True` | `False` removes the feature entirely, including for people who had set it up |
| `TWO_FACTOR_REQUIRED_SUPERUSERS` | `False` | Require it of superusers |
| `TWO_FACTOR_REQUIRED_ROLES` | `[]` | Require it of members of these roles (`auth.Group` names) |
| `TWO_FACTOR_ISSUER` | `None` | The name in the authenticator app. `None` means `SITE_TITLE` |
| `TWO_FACTOR_MAX_ATTEMPTS` | `5` | Wrong codes in a row before the second step locks |
| `TWO_FACTOR_LOCKOUT_SECONDS` | `300` | How long the lock lasts |

None of these is editable from the settings screen.

## When someone loses their phone

- **They have a recovery code**: they sign in with it, switch two-factor off on their own
  page, and set it up again on the new phone.
- **They do not**: a superuser opens **Your account → Two-factor sign-in for everyone**
  (`<panel>/_/account/two-factor/people/`) and resets them. The reset is recorded in the
  [audit log](permissions.md#the-audit-log) under the superuser's name.
- **The only superuser has lost theirs**: on the server,

  ```bash
  python manage.py vantage_two_factor_reset <username>
  ```

  which is audited with no actor — the log's way of saying "from the command line".

Only superusers can reset someone else, not whoever may manage roles: switching off a
superuser's second factor is a step towards becoming one. Nobody can reset their own from
that screen; their own page asks for proof.

## Safety properties

**Half signed in is not signed in.** After a right password the panel's sign-in does
*not* call Django's `login()`. The session stays anonymous, holding a five-minute note of
who got the password right, so nothing in the project — not only the panel — treats
them as signed in. The note is void if the password changes, the account is deactivated
or the authenticator is removed in the meantime.

**Every panel route is behind the gate.** It wraps every URL a site builds, so new pages,
plugin pages, dashboards and the API are covered without opting in. That also catches a
session signed in through a *different* login view, such as `django.contrib.admin`'s:
it is sent to a second-step page before the panel shows it anything. Pages are redirected;
form posts get a 403; the API and autocomplete get a JSON 403 with `"two_factor": "verify"`
or `"enrol"`.

**The session key rotates** when the password is accepted and again when the code is,
so a session id planted beforehand never ends up signed in.

**A code works once.** The 30-second step it was accepted for is stored, and the next
code must be for a later one. Two requests racing with the same code cannot both win: the
check and the update are one conditional `UPDATE`. Codes from one step either side of now
are accepted, for phone clocks that drift; all three candidates are compared in constant
time.

**Wrong codes count against the account, not the session.** Five in a row (by default)
lock the second step for five minutes — the right code and recovery codes included — and
throw away the half-signed-in note, so carrying on means typing the password again.
Opening fresh sessions buys an attacker nothing, and neither does sending guesses in
parallel: each attempt is counted, by one conditional `UPDATE`, *before* its answer is
compared, so however the requests interleave no more than five answers are looked at per
lockout. App codes, recovery codes and the password on the "switch it off" form all go
through that same count. The password step keeps its own
[throttle](security.md#loginthrottlemiddleware).

**The secret is sealed and shown only once.** It is encrypted at rest with a key derived
from `SECRET_KEY` (HMAC-SHA256 in counter mode, then a separate HMAC-SHA256 tag, so a
tampered row is refused rather than decrypted to something else). The tag also covers
whose secret it is and which enrolment, so someone who can write to the table but not
read `SECRET_KEY` cannot copy their own sealed secret over a colleague's and answer for
them with their own phone. A copy of the database alone does not reproduce anyone's
codes. After a code has confirmed it, nothing renders it again. Rotate `SECRET_KEY` the
[documented way](https://docs.djangoproject.com/en/stable/ref/settings/#secret-key-fallbacks)
— with the old key in `SECRET_KEY_FALLBACKS` — or every enrolled person will need a
recovery code or a reset. Each person's secret is sealed again under the new key the
next time they sign in with a code, so once everyone has, the fallback can go; anyone
who has not by then needs a recovery code or a reset. Values written before the binding
existed (`v1$…`) are still read, and are rewritten the same way. Recovery codes are unaffected by the key: they are 80 random
bits each, stored only as their SHA-256.

**Neither table is ever a panel model.** `TwoFactorDevice` and `TwoFactorRecoveryCode` are
in the registry's built-in exclusions, so no generic list, form, export or API exposes
them.

**CSRF everywhere.** Every view here is `csrf_protect`-ed and `never_cache`-d, so it holds
even in a project that has removed `CsrfViewMiddleware`. Pages that show a secret or
recovery codes are sent with `Cache-Control: no-store`.

**Impersonation does not touch it.** The gate asks about the person at the keyboard, not
the account being impersonated, and a superuser's proof survives starting and stopping
impersonation. The set-up and reset screens refuse while impersonating.

## The `manage.py check` errors

| Id | Meaning |
|---|---|
| `vantage.E095` | `TWO_FACTOR_REQUIRED_SUPERUSERS` is not a bool, or `TWO_FACTOR_REQUIRED_ROLES` is not a list of role names |
| `vantage.E096` | `TWO_FACTOR_ISSUER` is empty, not a string, or contains a colon |
| `vantage.E097` | Two-factor sign-in is on and the panel is mounted, but no `SessionMiddleware` is installed |

## Limitations

- **One authenticator per person.** To move to a new phone, switch it off and set it up
  again, or add the same key to both apps while setting up.
- **The cached answer is up to a minute old.** Whether someone has an authenticator, and
  whether a role requires one, is kept in their session and re-checked at least once a
  minute — and at once when the requirement settings change — so the gate costs no
  queries on an ordinary request. A reset or a new role membership reaches a live session
  within that minute.
- **Only the panel is gated.** A session signed in through another login view can still
  use the rest of your project before it answers; the panel's own sign-in, by contrast,
  signs nobody in until the code is in.
- **TOTP only.** No SMS (deliberately: it is phishable and SIM-swappable), no WebAuthn
  security keys yet.

## See also

- [Security](security.md) — the sign-in throttle and the rest of the hardening
- [Roles](roles.md) — the groups `TWO_FACTOR_REQUIRED_ROLES` names
- [Settings](settings.md#two-factor-sign-in) — every key in one table
