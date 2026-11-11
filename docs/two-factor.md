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

Turning it off, replacing the recovery codes, or adding or removing an authenticator asks
for a current code (or a recovery code) or the password first.

## Several authenticators

A person can have up to five authenticators at once — a phone and a spare, or a phone and
a tablet — each with a name they choose ("Phone"; blank means "Authenticator"). The
account page lists them with when each was added and last used.

- **Adding one**: type a name, confirm it is you, then scan the new QR code and type the
  code it shows, exactly as for the first. Nothing changes until that code is right. The
  recovery codes stay as they are; no new set is shown.
- **Signing in**: a code from *any* of the person's devices is accepted, and so is a
  recovery code. The session remembers which device passed.
- **Removing one**: use **Remove** on its row of "Your devices" and confirm it is you.
  Each section of the page (remove, add, new recovery codes, switch off) asks for that
  proof itself. A session that removes the device it signed in with, proving it with a code
  from another device or a recovery code, stays signed in; with only the password it is
  asked for a code again. Sessions that signed in with that device
  stop being trusted and are asked for a code again; sessions that used another device
  carry on. Removing the *last* one switches two-factor sign-in off, as before (a role
  that requires it sends the person to set it up again), and deletes the recovery codes.
  **Switch off** removes every device at once.
- **Recovery codes are the person's, not a device's.** They survive removing any one
  device, and a recovery code works whichever devices are left.

Every device keeps its own sealed secret, its own "already used" step (so one device's
code being spent does not spend another's) and its own lockout counters.

Existing accounts need nothing: the migration keeps each person's authenticator, names it
"Authenticator", and attaches their recovery codes to the person.

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

**Whoever enrols first owns it.** Setting it up needs only a signed-in session, so for an
account that has not enrolled yet, the password is still enough to enrol — and whoever
does it first holds the account's second factor from then on, its owner included. When
you switch a requirement on, an attacker who already has someone's password can race the
owner to the set-up page. To keep that visible and undoable:

- Every step is in the [audit log](permissions.md#the-audit-log) against the account:
  `two_factor.started`, `two_factor.enabled`, `two_factor.disabled`, `two_factor.codes`,
  `two_factor.recovery_used` and `two_factor.reset`, and for extra devices
  `two_factor.device_started`, `two_factor.device_added` and `two_factor.device_removed`,
  with the address and browser of the request that did it. Device names are recorded;
  secrets and codes never are.
- **Two-factor sign-in for everyone** shows when each person's authenticator was last
  set up. If someone does not recognise theirs, or cannot sign in because "their"
  authenticator is not on their phone, a superuser resets it and they set it up again.
- Ask people to enrol *before* you require it, and confirm on that screen that everyone
  you expect is there.

The requirement covers the panel. If `django.contrib.admin` is mounted too, see
[Django's admin](#djangos-admin) — its own sign-in asks for the password only.

| Key | Default | Effect |
|---|---|---|
| `TWO_FACTOR` | `True` | `False` removes the feature entirely, including for people who had set it up |
| `TWO_FACTOR_REQUIRED_SUPERUSERS` | `False` | Require it of superusers |
| `TWO_FACTOR_REQUIRED_ROLES` | `[]` | Require it of members of these roles (`auth.Group` names) |
| `TWO_FACTOR_ISSUER` | `None` | The name in the authenticator app. `None` means `SITE_TITLE` |
| `TWO_FACTOR_MAX_ATTEMPTS` | `5` | Wrong codes in a row before the second step locks |
| `TWO_FACTOR_LOCKOUT_SECONDS` | `300` | How long the first lock lasts; each lock in a row after it lasts twice as long, up to a day |

None of these is editable from the settings screen.

## Django's admin

`django.contrib.admin` has its own sign-in page and its own pages, and neither is the
panel's. Someone who signs in at `/admin/login/` (or wherever your admin is mounted)
gets a fully signed-in session that has never answered a code. The panel notices and
sends that session to its second step before showing anything — but Django's admin does
not ask, and lets it in.

To hold the admin to the same requirement, add the middleware after
`AuthenticationMiddleware`:

```python
MIDDLEWARE = [
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "vantage.twofactor.AdminTwoFactorMiddleware",
    # ...
]
```

Every admin page then asks what the panel asks, of the same person. A session that owes
a code is sent to the panel's second step (`<panel>/_/account/two-factor/verify/`), and
one that owes an enrolment to the panel's set-up page, each with the admin page as
`next`; form posts get a 403, and the admin's autocomplete a JSON 403. The admin's
sign-in and sign-out pages stay open. Admin pages are recognised by the marks
`AdminSite` and `ModelAdmin` put on their views, so a renamed or second admin site is
covered wherever it is mounted.

It is opt-in because it changes a page this package does not own. `manage.py check
--deploy` warns (`vantage.W095`) when two-factor sign-in is required of anyone, the
panel is mounted, Django's admin is mounted in the root URLconf, and the middleware is
not installed.

Other login views of your own — a custom one, an API token endpoint — are yours to
gate: a session they sign in is sent to the second step by the panel, and by nothing else.

## When someone loses their phone

- **They have another device**: they sign in with it, remove the lost one on their own
  page, and add the new phone.
- **They have a recovery code**: they sign in with it, add the new phone as a device (and
  remove the lost one), or switch two-factor off and set it up again.
- **They do not**: a superuser opens **Your account → Two-factor sign-in for everyone**
  (`<panel>/_/account/two-factor/people/`) and resets them. The screen lists every
  device. **Reset** on a device removes just that one and leaves the person's other
  devices (and recovery codes) working; **Reset all**, and the `manage.py` command below,
  remove every device and the recovery codes, which switches their two-factor sign-in
  off. Resetting the last remaining device is the same as resetting all. The success
  message says how many devices were removed, and the reset is recorded in the
  [audit log](permissions.md#the-audit-log) under the superuser's name.
- **The only superuser has lost theirs**: on the server,

  ```bash
  python manage.py vantage_two_factor_reset <username>
  ```

  which removes all of that account's devices and is audited with no actor — the log's
  way of saying "from the command line".

Only superusers can reset someone else, not whoever may manage roles: switching off a
superuser's second factor is a step towards becoming one. A superuser can reset another
superuser's two-factor sign-in without that person's password; that is by design, and
it is audited under the acting superuser's name. Nobody can reset their own from
that screen; their own page asks for proof.

## Upgrading

Migrations `0024` and `0025` move recovery codes from the device to the person. Reversing
them restores the column and points each code at its owner's oldest device; codes of a
person who has no device by then cannot be attached to one and are deleted. Back up
before migrating backwards.

## Safety properties

**Half signed in is not signed in.** After a right password the panel's sign-in does
*not* call Django's `login()`. The session stays anonymous, holding a five-minute note of
who got the password right, so nothing in the project — not only the panel — treats
them as signed in. The note is void if the password changes, the account is deactivated
or the authenticator is removed in the meantime.

**Every panel route is behind the gate.** It wraps every URL a site builds — routes
grouped under an `include()` as well, all the way down — so new pages, plugin pages,
dashboards and the API are covered without opting in. A session signed in through a
*different* login view, such as `django.contrib.admin`'s, is sent to a second-step page
before the *panel* shows it anything; that other view's own pages are not covered unless
you install [`AdminTwoFactorMiddleware`](#djangos-admin) (for Django's admin) or gate them
yourself. Pages are redirected; form posts get a 403; the API and autocomplete get a JSON
403 with `"two_factor": "verify"` or `"enrol"`.

**The session key rotates** when the password is accepted and again when the code is,
so a session id planted beforehand never ends up signed in.

**A code works once.** The 30-second step it was accepted for is stored, and the next
code must be for a later one. Two requests racing with the same code cannot both win: the
check and the update are one conditional `UPDATE`. Codes from one step either side of now
are accepted, for phone clocks that drift; all three candidates are compared in constant
time.

**Wrong codes count against the account, not the session.** The count is kept per device,
but a guess is not free on the others: every wrong code or password is counted against
*each* of the person's devices that is not locked, before it is compared, and counts for
as many attempts as there are such devices. A code is compared against every one of their
secrets, so with `n` devices the second step locks after `ceil(TWO_FACTOR_MAX_ATTEMPTS / n)`
wrong answers (at least one); the chance of guessing a code within one lockout is then no
higher than with a single device. A device that is locked is not compared against at all;
the others still answer, and a right answer starts the counts again on all of them. The
second step as a whole is locked when every device is. Five in a row (by default) lock the second step for five minutes — the right code and recovery codes included — and
throw away the half-signed-in note, so carrying on means typing the password again.
Opening fresh sessions buys an attacker nothing, and neither does sending guesses in
parallel: each attempt is counted, by one conditional `UPDATE`, *before* its answer is
compared, so however the requests interleave no more than five answers are looked at per
lockout. App codes, recovery codes and the password on the "switch it off" form all go
through that same count.

**Each lock in a row lasts twice as long.** Five minutes, then ten, twenty and so on, up
to a day, until a right answer starts the count again. A fixed lock only sets a steady
guessing rate — five tries every five minutes is over a thousand a day, which a six-digit
code does not survive for long — while doubling holds an attacker who has the password
to a handful of guesses a day. A person locked out for longer than they can wait asks a
superuser for a [reset](#when-someone-loses-their-phone). The password step keeps its own
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

## The `manage.py check` messages

| Id | Meaning |
|---|---|
| `vantage.E095` | `TWO_FACTOR_REQUIRED_SUPERUSERS` is not a bool, or `TWO_FACTOR_REQUIRED_ROLES` is not a list of role names |
| `vantage.E096` | `TWO_FACTOR_ISSUER` is empty, not a string, or contains a colon |
| `vantage.E097` | Two-factor sign-in is on and the panel is mounted, but no `SessionMiddleware` is installed |
| `vantage.W095` | `--deploy` only. Two-factor sign-in is required of someone, and `django.contrib.admin` is mounted without [`AdminTwoFactorMiddleware`](#djangos-admin) |

## Limitations

- **Five authenticators per person.** A cap, because each one is another place a code can
  be guessed against.
- **A new role membership reaches a live session within a minute.** Whether someone has
  an authenticator, and whether a role requires one, is kept in their session and
  re-read at least once a minute — and at once when the requirement settings change.
  Removing a device, a reset and switching two-factor off do not wait for that: each
  request checks, with one indexed query, that the device its session passed with still
  exists, so they take effect on the next request.
- **A device you started but did not confirm expires after ten minutes.** Its secret is
  discarded and the next visit to the page starts a new one.
- **Only the panel is gated, unless you say otherwise.** A session signed in through
  another login view can still use the rest of your project before it answers — Django's
  admin included, until [`AdminTwoFactorMiddleware`](#djangos-admin) is installed. The
  panel's own sign-in, by contrast, signs nobody in until the code is in.
- **TOTP only.** No SMS (deliberately: it is phishable and SIM-swappable), no WebAuthn
  security keys yet.

## See also

- [Security](security.md) — the sign-in throttle and the rest of the hardening
- [Roles](roles.md) — the groups `TWO_FACTOR_REQUIRED_ROLES` names
- [Settings](settings.md#two-factor-sign-in) — every key in one table
