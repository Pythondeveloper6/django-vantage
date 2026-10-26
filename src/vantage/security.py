"""Hardening for a panel that is actually exposed.

Everything in this module exists because the package is pointed at production data by
definition. Five concerns live here. They are unrelated to each other except in that
every one of them is something a project cannot add from the outside.

**Content-Security-Policy.** The panel's shell inlines two things it cannot move out:
a ``<style>`` block of theme tokens derived from settings, and a blocking ``<script>``
that stamps the saved theme onto ``<html>`` before first paint. Both are correct
decisions — a tokens stylesheet served as a file would be wrong for any project running
more than one brand, and a deferred theme script paints light and then flips — and both
force ``unsafe-inline`` on any project with a strict policy. That is a real deployment
blocker, so this module supplies the nonce plumbing that removes it, and
:func:`scan_panel_templates` reports the handful of places a nonce cannot reach.

**Response headers.** Clickjacking, referrer leakage and MIME sniffing are decided per
response, and the panel's answers are not the answers a content site wants. They are
set here rather than left to ``SecurityMiddleware`` because the two disagree: a site
that legitimately frames its marketing pages still must not frame this, and a site
happy with ``strict-origin-when-cross-origin`` still must not put a row's primary key
in a ``Referer`` sent to a third party.

**Login throttling.** There was none. The design constraint is the whole feature:

* keyed on IP alone locks out an office the moment one person fumbles their password,
  and every office is one IP;
* keyed on username alone hands anybody who knows an administrator's username a
  denial of service against that account, from anywhere, for free;
* so it is keyed on **both**, and a lockout is scoped to one person at one place.

**Rate limiting the panel's own surfaces.** Login is not the only thing worth a budget.
Autocomplete is a read API over arbitrary models, export walks whole tables, and import
parses attacker-supplied files — each is a small request that costs the server a lot,
which is the shape of every amplification. These are keyed on the *account*, not on the
address: past the login form there is an authenticated user to blame, and keying on IP
would again punish an office.

**Uploads.** A file field on a panel-registered model accepts bytes from a person and
hands them back out under the panel's own origin. Extension is a claim, not evidence,
so content is sniffed; the size ceiling is enforced because the ceiling the browser
respects is the one an attacker removes first.

Both counters are cache-backed because the alternative is a database write per hostile
request, which is an amplifier rather than a defence. Everything here is applied by
middleware rather than by subclassing views, so a project opts in by installing
middleware and a project that does not install it is byte-for-byte unchanged.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

__all__ = [
    "CSP_DIRECTIVES",
    "CSP_EXEMPT_TEMPLATES",
    "DEFAULTS",
    "IMAGE_SUFFIXES",
    "PANEL_HEADERS",
    "RATE_LIMITS",
    "SIGNATURES",
    "SNIFF_BYTES",
    "SVG_HAZARDS",
    "InlineFinding",
    "LoginThrottle",
    "RateLimit",
    "ThrottleState",
    "actor_for",
    "apply_security_headers",
    "client_ip",
    "csp_context",
    "csp_nonce",
    "inspect_upload",
    "nonce_attr",
    "panel_templates",
    "rate_limit_for",
    "scan_inline_html",
    "scan_panel_templates",
    "sniff",
    "throttle_from_settings",
]

logger = logging.getLogger("vantage.security")


#: Settings this module reads, with the default each falls back to.
#:
#: Each is registered in :data:`vantage.conf.DEFAULTS` as ``None`` — that registration
#: is what makes it settable, because the settings layer rejects unknown keys on
#: purpose — while the value and the reasoning for it stay here, so the two files
#: cannot drift into disagreeing about a default. See ``docs/security.md``.
DEFAULTS: dict[str, object] = {
    # Off unless the middleware is installed; this is the switch for turning it back
    # off again without unwiring middleware, which is what you want in a hurry.
    "LOGIN_THROTTLE": True,
    # Failures allowed per (username, IP) inside the window. Ten is deliberately
    # generous: a human who has forgotten which of their three passwords it is should
    # never meet this, and an attacker running a dictionary meets it immediately.
    "LOGIN_THROTTLE_LIMIT": 10,
    # Seconds. Both the counting window and the lockout — a fixed window keeps the
    # implementation to one ``incr`` and makes "locked out until" a number you can say
    # out loud to the person on the phone.
    "LOGIN_THROTTLE_WINDOW": 300,
    # Which configured cache to count in. A separate alias is worth having: these keys
    # are tiny, hot, and must not be the first thing evicted when a page cache fills.
    "LOGIN_THROTTLE_CACHE": "default",
    # A header to read the client address from, e.g. "HTTP_X_FORWARDED_FOR". None means
    # REMOTE_ADDR only. Read the warning on client_ip() before setting this.
    "LOGIN_THROTTLE_IP_HEADER": None,
    # Off unless PanelSecurityMiddleware is installed, for the same reason as above:
    # one switch that stops the counting without unwiring anything.
    "RATE_LIMIT": True,
    "RATE_LIMIT_CACHE": "default",
    # surface -> (requests, seconds). Overriding the dict replaces only the surfaces
    # named in it; see rate_limit_for().
    "RATE_LIMITS": None,
    # The per-file ceiling the panel enforces on anything arriving in request.FILES.
    # Generous, because a panel is where the 8-megapixel product photo gets uploaded;
    # the point is that there *is* one, not that it is tight.
    "UPLOAD_MAX_BYTES": 10 * 1024 * 1024,
}

#: Default budget per surface: ``(requests, seconds)``, per account.
#:
#: The numbers are chosen so that no human working normally can reach one and every
#: script reaches its own immediately.
#:
#: * ``autocomplete`` — two per second sustained. A fast typist with a debounced picker
#:   makes perhaps one request per word; a loop paging a table makes thousands.
#: * ``export`` — an export walks a whole table with a database connection held open
#:   for the length of the download, so this is the surface where a dozen concurrent
#:   requests is a capacity incident rather than an inconvenience.
#: * ``import`` — every attempt parses an attacker-supplied file. The wizard is four
#:   requests per real import, so ten is roughly "two imports and a mistake".
#: * ``reveal`` — one sensitive value per request; see :mod:`vantage.revealviews`.
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "autocomplete": (120, 60),
    "export": (12, 300),
    "import": (10, 300),
    # Thirty sensitive values per five minutes. Someone checking a customer's key while
    # on the phone with them never meets it; a script walking every row meets it on
    # the thirty-first. Spent by vantage.revealviews itself, so it holds without the
    # middleware installed.
    "reveal": (30, 300),
}


def _setting(name: str, default=None):
    """One Vantage setting, resolved through the settings layer then this module.

    ``vantage.conf.DEFAULTS`` registers each of these keys with ``None``, meaning "no
    opinion here" — the defaults themselves stay in :data:`DEFAULTS` below, next to
    the reasoning for each number, so the two files cannot drift into disagreeing.
    Registration is what makes the keys settable at all: ``vantage_settings`` rejects
    unknown keys on purpose, so before it a documented setting written into ``VANTAGE``
    raised ``ImproperlyConfigured`` on the next settings read.

    Read from the raw ``VANTAGE`` dict rather than through ``vantage_settings``, and
    that is deliberate on two counts.

    The cascade consults the :class:`~vantage.models.SiteConfiguration` row, so going
    through it would put a database read behind every one of these — including the
    login throttle, whose entire job is to answer cheaply while somebody is hammering
    the login form. A refused attempt must not cost a query.

    And these must not be editable from the panel's own settings screen at all. A UI
    that can switch off rate limiting is a UI that can be used to switch off rate
    limiting. Deployment settings belong in the file the deployment owns.
    """
    from django.conf import settings as django_settings

    configured = getattr(django_settings, "VANTAGE", None) or {}
    value = configured.get(name)
    if value is not None:
        return value
    if default is not None:
        return default
    return DEFAULTS.get(name)


# ── Content-Security-Policy ─────────────────────────────────────────────────


#: What the panel needs from a policy, directive by directive, with the reason. Quoted
#: verbatim in ``docs/security.md`` so the two cannot drift.
CSP_DIRECTIVES: dict[str, str] = {
    "default-src": "'self' — every fetch the panel makes is same-origin.",
    "script-src": (
        "'self' plus a nonce. One inline script (the no-flash theme stamp) and the "
        "panel's own static bundle. No CDN, no eval, no inline handlers."
    ),
    "style-src": (
        "'self' plus a nonce. One inline style block (the theme tokens) and the "
        "panel's own stylesheets."
    ),
    "img-src": (
        "'self' data: — 'data:' because uploaded branding may be inlined and because "
        "the empty-state marks are data URIs. Add your media host if it is separate."
    ),
    "font-src": "'self' — the panel ships no webfont and loads none.",
    "connect-src": "'self' — autocomplete and inline editing are same-origin fetches.",
    "frame-ancestors": "'none' — nothing in an admin panel should ever be framed.",
    "form-action": "'self' — every form in the panel posts back to the panel.",
    "base-uri": "'none' — the panel emits no <base>, and one injected would be a hijack.",
    "object-src": "'none' — no plugin content, ever.",
}


def csp_nonce(request) -> str:
    """The per-request CSP nonce, or ``""`` when nothing is providing one.

    Three sources are tried, in the order a project is likely to have them:
    ``request.csp_nonce`` (django-csp, and Django's own attribute name on 6.0),
    ``django.middleware.csp.get_nonce`` (Django 6.1 moved it to ``request._csp_nonce``
    behind an accessor), and then nothing.

    The value is materialised with ``str()`` rather than tested for truth first, which
    is not a detail. Django's ``LazyNonce`` is deliberately *falsy until it has been
    generated*, so that a response only carries a nonce in its header if something
    actually asked for one. ``if request.csp_nonce:`` therefore reports "no nonce" on
    every request that has a perfectly good one, and the guard silently disables the
    feature it was meant to protect.
    """
    if request is None:
        return ""

    nonce = getattr(request, "csp_nonce", None)
    if nonce is None:
        try:
            from django.middleware.csp import get_nonce
        except ImportError:  # pragma: no cover - Django < 6.0
            return ""
        nonce = get_nonce(request)

    if nonce is None:
        return ""
    try:
        value = str(nonce)
    except Exception:  # pragma: no cover - a third-party nonce object that misbehaves
        logger.exception("Could not read the CSP nonce from the request.")
        return ""
    return value if value else ""


def nonce_attr(request) -> SafeString:
    """``' nonce="…"'`` for a ``<script>`` or ``<style>``, or nothing at all.

    A whole attribute rather than a bare value, because the alternative asks the
    template to write ``{% if %}`` around it — and the only correct condition is one
    that has already materialised the nonce, which is exactly the trap described on
    :func:`csp_nonce`. Returning the attribute or the empty string moves that decision
    into Python, where it can be tested.

    The leading space is part of the return value so ``<script{{ csp_nonce_attr }}>``
    is well-formed in both directions.
    """
    value = csp_nonce(request)
    if not value:
        return mark_safe("")
    return format_html(' nonce="{}"', value)


def csp_context(request) -> dict:
    """Context processor. Add it to have the shell nonce its own inline blocks.

    Deliberately does not publish ``csp_nonce`` itself: on Django 6.1
    ``django.template.context_processors.csp`` already puts that name in the context,
    and shadowing it with a different type would be a trap for anyone reading the
    template. This adds one name, and it is the one the templates use.
    """
    return {"csp_nonce_attr": nonce_attr(request)}


# ── what a strict policy would block ────────────────────────────────────────
#
# A nonce fixes ``<script>`` and ``<style>`` *elements* and nothing else. It cannot be
# attached to a ``style="…"`` attribute or an ``onclick=`` — those are governed by
# ``style-src-attr`` and ``script-src-attr``, which accept only ``'unsafe-inline'`` or a
# hash. So the package's remaining inline attributes are not a nonce problem to be
# plumbed, they are a fact a deployment has to be told before it writes a policy that
# silently breaks the panel.
#
# They are found by reading the templates rather than by keeping a list, because a list
# is wrong the first time someone edits a template, and because a project that has
# overridden ``vantage/base.html`` in its own template directory has taken ownership of
# the nonce and is exactly the case worth catching.


#: Attribute names treated as event handlers. A curated set rather than ``on[a-z]+``:
#: an attribute called ``online`` or a template tag argument named ``only`` should not
#: be reported as script, and a false positive in a security check is how the check
#: gets switched off.
_EVENT_ATTRS = (
    "click|dblclick|change|input|submit|reset|focus|blur|load|error|scroll|toggle|"
    r"key\w+|mouse\w+|drag\w*|drop|paste|copy|cut|wheel|touch\w+|pointer\w+|animation\w+"
)

#: What may precede an attribute name. ``}`` and ``%`` are in there because these are
#: templates: ``{% if brand %}style="…"`` is an inline style attribute that a scanner
#: looking only for whitespace walks straight past, and a conditional attribute is
#: exactly the kind that gets forgotten.
_BEFORE_ATTR = r"""(?:^|[\s}%"'])"""

_TAG_RE = re.compile(r"<\s*(script|style)\b([^>]*)>", re.IGNORECASE)
_STYLE_ATTR_RE = re.compile(rf"""{_BEFORE_ATTR}style\s*=\s*["']""", re.IGNORECASE)
_EVENT_ATTR_RE = re.compile(rf"""{_BEFORE_ATTR}on(?:{_EVENT_ATTRS})\s*=\s*["']""", re.IGNORECASE)
_SRC_RE = re.compile(r"\bsrc\s*=")
#: A literal ``nonce=`` or the context variable the templates use to emit one.
_NONCE_RE = re.compile(r"\bnonce\s*=|csp_nonce_attr")

#: What each kind of finding needs from a policy, and whether a nonce can supply it.
_KIND_DIRECTIVE: dict[str, tuple[str, bool]] = {
    "script": ("script-src", True),
    "style": ("style-src", True),
    "style-attr": ("style-src-attr", False),
    "event-attr": ("script-src-attr", False),
}


@dataclass(frozen=True)
class InlineFinding:
    """One inline construct in one template, and what a policy must say about it."""

    #: Path as the template loader names it, e.g. ``vantage/base.html``.
    template: str
    line: int
    #: One of ``script``, ``style``, ``style-attr``, ``event-attr``.
    kind: str
    snippet: str

    @property
    def directive(self) -> str:
        return _KIND_DIRECTIVE[self.kind][0]

    @property
    def nonceable(self) -> bool:
        """Whether a nonce can authorise this, or only ``'unsafe-inline'`` can."""
        return _KIND_DIRECTIVE[self.kind][1]

    def __str__(self) -> str:
        return f"{self.template}:{self.line} {self.kind} needs {self.directive}"


#: Django template comments, HTML comments, and ``{# … #}``. Blanked before scanning:
#: this package's templates explain themselves at length, and a comment saying "the
#: ``<script>`` here is external on purpose" would otherwise be reported as an inline
#: script — a false positive in the one place where a false positive is fatal, because
#: the reader learns the check is wrong and stops reading it.
_COMMENT_RE = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}|{#.*?#}|<!--.*?-->", re.DOTALL)


def _blank_comments(source: str) -> str:
    """Replace comment spans with spaces, keeping every newline so lines still count."""
    return _COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), source)


def scan_inline_html(source: str, template: str = "") -> list[InlineFinding]:
    """Every inline construct in one template that a strict CSP would refuse.

    Nonced elements are *not* reported: ``<script{{ csp_nonce_attr }}>`` is already
    solved, and reporting it would drown the two findings that are not. An external
    ``<script src=…>`` is not reported either — that is what ``'self'`` is for. Nor is
    anything inside a comment.
    """
    findings: list[InlineFinding] = []
    for number, line in enumerate(_blank_comments(source).splitlines(), start=1):
        for match in _TAG_RE.finditer(line):
            kind, attrs = match.group(1).lower(), match.group(2)
            if _NONCE_RE.search(attrs):
                continue
            if kind == "script" and _SRC_RE.search(attrs):
                continue
            findings.append(InlineFinding(template, number, kind, match.group(0).strip()[:120]))
        for pattern, kind in ((_STYLE_ATTR_RE, "style-attr"), (_EVENT_ATTR_RE, "event-attr")):
            match = pattern.search(line)
            if match:
                findings.append(InlineFinding(template, number, kind, line.strip()[:120]))
    return findings


def panel_templates() -> list[tuple[str, Path]]:
    """``(name, path)`` for every ``vantage/*.html`` a loader would find, once each.

    Configured ``DIRS`` are walked before app directories and the first hit for a name
    wins, which is the order ``app_directories``/``filesystem`` resolve in. That makes
    the scan report the template that will actually render, so a project override that
    dropped the nonce is caught and the shipped file it shadows is not blamed.
    """
    from django.conf import settings as django_settings
    from django.template.utils import get_app_template_dirs

    roots: list[Path] = []
    for engine in getattr(django_settings, "TEMPLATES", None) or []:
        roots.extend(Path(str(item)) for item in engine.get("DIRS") or [])
    roots.extend(Path(str(item)) for item in get_app_template_dirs("templates"))

    found: dict[str, Path] = {}
    for root in roots:
        base = root / "vantage"
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.html")):
            name = path.relative_to(root).as_posix()
            found.setdefault(name, path)
    return sorted(found.items())


#: Templates whose inline content is not reportable, with the reason. One entry, and it
#: is the last-resort error page: it renders with no context at all — no request, no
#: context processors, no settings — precisely so that it still works when those are
#: what failed. There is nowhere for a nonce to come from. Under a strict policy it
#: renders as unstyled black-on-white text, which is a correct outcome for a page whose
#: job is to say "something went wrong" when the package itself is the suspect.
CSP_EXEMPT_TEMPLATES: dict[str, str] = {
    "vantage/500.html": (
        "the last-resort error page renders with no context, so it cannot carry a "
        "nonce; under a strict policy it degrades to unstyled text"
    ),
}


def scan_panel_templates() -> list[InlineFinding]:
    """Run :func:`scan_inline_html` over every panel template on the loader path.

    :data:`CSP_EXEMPT_TEMPLATES` is skipped. A permanent warning about something the
    reader cannot change is how a checks framework gets added to the ignore list, taking
    the findings that *were* actionable with it.

    A template that cannot be read is skipped rather than raised over. This runs from a
    system check, and a check that explodes on an unreadable file has taken away the
    tool someone was using to find out why their deployment is broken.
    """
    findings: list[InlineFinding] = []
    for name, path in panel_templates():
        if name in CSP_EXEMPT_TEMPLATES:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable template
            logger.warning("Could not read %s while auditing inline HTML.", path)
            continue
        findings.extend(scan_inline_html(source, name))
    return findings


# ── response headers ────────────────────────────────────────────────────────


#: Set on every panel response. One line of reasoning each:
#:
#: ``X-Frame-Options``
#:     ``DENY``, not ``SAMEORIGIN``. The panel has one-click destructive controls and no
#:     reason to appear in any frame, including its own site's. Kept alongside
#:     ``frame-ancestors 'none'`` rather than replaced by it because the header is the
#:     one every browser and every scanner still honours.
#: ``X-Content-Type-Options``
#:     ``nosniff``. The panel serves user-uploaded branding and user-generated exports;
#:     sniffing is how a ``.csv`` of somebody's own text becomes a rendered document.
#: ``Referrer-Policy``
#:     ``same-origin``. Panel URLs carry primary keys, model names and filter values —
#:     ``/admin/hr/employee/4172/`` leaks the row and the schema to whatever a user
#:     clicks through to. ``strict-origin-when-cross-origin``, Django's default, still
#:     sends the origin; there is nothing a third party needs to learn from this panel.
#: ``Cross-Origin-Opener-Policy``
#:     ``same-origin``, so a page that opened the panel keeps no window handle to it.
#: ``Cross-Origin-Resource-Policy``
#:     ``same-origin``, so no other site can pull a panel response in as a subresource
#:     and measure it.
#: ``X-Robots-Tag``
#:     ``noindex, nofollow``. A crawler cannot sign in, but it can index the URL space,
#:     and a list of your models and primary keys is reconnaissance served for free.
PANEL_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Robots-Tag": "noindex, nofollow",
}


def apply_security_headers(response, *, force: bool = False):
    """Add :data:`PANEL_HEADERS` and never-cache headers to one response.

    Existing values are left alone unless ``force``. That is not politeness: a view that
    set a header set it for a reason this function cannot see, and silently overwriting
    it would make the panel's behaviour depend on middleware ordering.

    ``response.xframe_options_exempt`` is honoured, the same flag Django's own
    ``XFrameOptionsMiddleware`` reads, so the one documented way to allow framing keeps
    working instead of becoming two contradictory mechanisms.

    Caching is Django's ``add_never_cache_headers``, applied only when the view has not
    already spoken. Every panel response is per-user and permission-scoped; one cached
    by an intermediary is one user's rows served to another.
    """
    from django.utils.cache import add_never_cache_headers

    exempt = getattr(response, "xframe_options_exempt", False)
    for header, value in PANEL_HEADERS.items():
        if header == "X-Frame-Options" and exempt:
            continue
        if force or header not in response:
            response[header] = value

    if force or "Cache-Control" not in response:
        add_never_cache_headers(response)
    return response


# ── login throttling ────────────────────────────────────────────────────────


def client_ip(request) -> str:
    """The address to key a throttle on. ``""`` when there is not a usable one.

    ``REMOTE_ADDR`` by default, and *only* ``REMOTE_ADDR``. A forwarded header is
    client-supplied unless a proxy the deployment controls overwrites it, and this
    layer cannot know whether one does. Trusting it by default would let an attacker
    put a fresh value in ``X-Forwarded-For`` on every request and never meet the
    throttle at all, which is worse than having no throttle because it looks like one.

    ``LOGIN_THROTTLE_IP_HEADER`` opts into a header once you know your proxy sets it.
    The leftmost entry is taken, which is right only when the proxy chain is trusted
    end to end; behind a single load balancer that overwrites the header it is.

    The value is parsed as an address and re-rendered, so a garbage header cannot
    become a cache key and ``1.2.3.4`` and ``1.2.3.4 `` cannot become two buckets.
    """
    meta = getattr(request, "META", None) or {}

    header = _setting("LOGIN_THROTTLE_IP_HEADER")
    if header:
        raw = (meta.get(header) or "").split(",")[0]
    else:
        raw = meta.get("REMOTE_ADDR") or ""

    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return ""


@dataclass(frozen=True)
class ThrottleState:
    """What the throttle knows about one (username, address) pair right now."""

    #: Failures recorded inside the current window.
    failures: int
    #: The ceiling. Zero or below means the throttle is off.
    limit: int
    #: Seconds until the window expires. Best-effort: not every cache backend can say.
    retry_after: int

    @property
    def blocked(self) -> bool:
        return self.limit > 0 and self.failures >= self.limit

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.failures)


def _bump(cache, key: str, window: int) -> int:
    """Add one to a fixed-window counter and return the new value.

    ``add`` then ``incr`` rather than ``get``/``set``: it is atomic on every backend
    that matters, so two simultaneous requests cannot both read 4 and both write 5.
    ``add`` is a no-op when the key already exists, which is what keeps the window
    *fixed* rather than sliding forward on every attempt — otherwise a steady trickle
    of guesses would extend a lockout forever, which is the username-only denial of
    service arriving by another road.

    Shared by the login throttle and the per-surface rate limits so there is one
    implementation of the only part of this that has to be right under concurrency.
    """
    cache.add(key, 0, window)
    try:
        return int(cache.incr(key))
    except ValueError:
        # The key expired between the add and the incr. One lost tick at a window
        # boundary is not worth a retry loop.
        return 1


@dataclass(frozen=True)
class LoginThrottle:
    """A fixed-window failure counter keyed on username *and* address.

    Both halves of the key, always. The class refuses to be constructed in a way that
    keys on one of them, because the two single-key designs are the two ways this
    feature turns into the incident it was meant to prevent:

    * IP only — one person fumbles a password ten times and their whole office is
      locked out of the admin panel, including the person who could fix it;
    * username only — anybody who knows that ``admin`` exists can hold that account
      shut from anywhere, indefinitely, at the cost of ten HTTP requests.

    A pair key gives up something real in exchange: an attacker with a botnet gets a
    fresh budget per source address. That is the correct trade for a *login* throttle,
    whose job is to make an online dictionary attack from one place uneconomic. The
    defence against a distributed attack is a password policy and MFA, and pretending
    a counter is that defence is how you end up with neither.
    """

    limit: int = 10
    window: int = 300
    cache_alias: str = "default"
    enabled: bool = True

    # ── keys ────────────────────────────────────────────────────────────────

    @staticmethod
    def cache_key(username: str, address: str) -> str:
        """One opaque key per (username, address) pair.

        Hashed rather than interpolated, for three reasons that all bite in
        production: usernames are frequently email addresses and a cache key is not a
        place to put personal data; memcached rejects keys containing spaces or
        control characters and silently misbehaves near its 250-byte limit; and a
        separator in a raw key lets ``("a:b", "c")`` and ``("a", "b:c")`` collide,
        which here would mean one user's failures counting against another's.
        """
        digest = hashlib.sha256(f"{username}\x00{address}".encode(errors="replace")).hexdigest()
        return f"vantage-login-throttle:{digest[:32]}"

    # ── reading ─────────────────────────────────────────────────────────────

    @property
    def cache(self):
        from django.core.cache import caches

        return caches[self.cache_alias]

    def state(self, username: str, address: str) -> ThrottleState:
        """The current count, without recording anything."""
        if not self.enabled or self.limit <= 0:
            return ThrottleState(failures=0, limit=0, retry_after=0)

        try:
            failures = self.cache.get(self.cache_key(username, address)) or 0
        except Exception:
            # A cache that is down must not become a login outage. Failing open is
            # the right call for a throttle and the wrong call for almost anything
            # else, which is why it is stated here rather than assumed.
            logger.exception("Vantage login throttle could not read the cache; allowing.")
            return ThrottleState(failures=0, limit=self.limit, retry_after=0)

        return ThrottleState(failures=int(failures), limit=self.limit, retry_after=self.window)

    def is_blocked(self, username: str, address: str) -> bool:
        return self.state(username, address).blocked

    # ── writing ─────────────────────────────────────────────────────────────

    def record_failure(self, username: str, address: str) -> ThrottleState:
        """Count one failed attempt. Returns the state *after* counting."""
        if not self.enabled or self.limit <= 0:
            return ThrottleState(failures=0, limit=0, retry_after=0)

        key = self.cache_key(username, address)
        try:
            failures = _bump(self.cache, key, self.window)
        except Exception:
            logger.exception("Vantage login throttle could not write to the cache.")
            return ThrottleState(failures=0, limit=self.limit, retry_after=0)

        return ThrottleState(failures=int(failures), limit=self.limit, retry_after=self.window)

    def clear(self, username: str, address: str) -> None:
        """Forget this pair's failures. Called on a successful sign-in.

        Scoped to the pair, not the username: clearing every address on success would
        let an attacker who guesses correctly once wipe the counter they had built up
        elsewhere, and clearing on *someone else's* success is not something a
        successful login has earned.
        """
        try:
            self.cache.delete(self.cache_key(username, address))
        except Exception:  # pragma: no cover - a cache that cannot delete
            logger.exception("Vantage login throttle could not clear the cache.")


def throttle_from_settings() -> LoginThrottle:
    """A :class:`LoginThrottle` built from the configured values."""
    return LoginThrottle(
        limit=int(_setting("LOGIN_THROTTLE_LIMIT")),
        window=int(_setting("LOGIN_THROTTLE_WINDOW")),
        cache_alias=str(_setting("LOGIN_THROTTLE_CACHE")),
        enabled=bool(_setting("LOGIN_THROTTLE")),
    )


# ── rate limiting the panel's own surfaces ──────────────────────────────────


@dataclass(frozen=True)
class RateLimit:
    """A fixed-window request budget for one surface, keyed on one actor.

    Distinct from :class:`LoginThrottle` in what it counts and who it counts against.
    The login throttle counts *failures* against a (username, address) pair because
    there is no account yet to blame and the address is all there is. Past the login
    form there is one, so these count *requests* against the account — which is both
    fairer (an office behind one address is not one budget) and stricter (an attacker
    with a botnet and one stolen session still has one budget).

    Anonymous callers fall back to the address. In practice they get a redirect to the
    sign-in page before reaching a budgeted surface at all; the fallback exists so that
    a misconfiguration cannot turn "no user" into one shared unlimited bucket.
    """

    surface: str
    limit: int
    window: int
    cache_alias: str = "default"
    enabled: bool = True

    @property
    def cache(self):
        from django.core.cache import caches

        return caches[self.cache_alias]

    def cache_key(self, actor: str) -> str:
        """One key per (surface, actor). Hashed, for the reasons on
        :meth:`LoginThrottle.cache_key` — an actor string can be an email address, and
        an email address is neither memcached-safe nor something to write into a key."""
        digest = hashlib.sha256(f"{self.surface}\x00{actor}".encode(errors="replace")).hexdigest()
        return f"vantage-rate:{self.surface}:{digest[:24]}"

    def spend(self, actor: str) -> ThrottleState:
        """Count this request and report the budget as it stood *before* it.

        The off-by-one is deliberate and is what makes ``blocked`` readable at the call
        site: with a limit of ten, requests one to ten see ``failures`` of zero to nine
        and are allowed; the eleventh sees ten and is refused. The caller can therefore
        spend first and ask afterwards, in one cache round trip, and the request that
        gets refused is the one after the budget ran out rather than the one that
        finished it.
        """
        if not self.enabled or self.limit <= 0:
            return ThrottleState(failures=0, limit=0, retry_after=0)
        try:
            count = _bump(self.cache, self.cache_key(actor), self.window)
        except Exception:
            # Same call as the login throttle: a cache outage must not become a panel
            # outage. It is a budget, not an authorisation.
            logger.exception("Vantage rate limit could not reach the cache; allowing.")
            return ThrottleState(failures=0, limit=self.limit, retry_after=0)
        return ThrottleState(failures=max(0, count - 1), limit=self.limit, retry_after=self.window)

    def peek(self, actor: str) -> ThrottleState:
        """The state without spending anything. For tests and for introspection."""
        if not self.enabled or self.limit <= 0:
            return ThrottleState(failures=0, limit=0, retry_after=0)
        try:
            count = self.cache.get(self.cache_key(actor)) or 0
        except Exception:  # pragma: no cover - a cache that cannot read
            logger.exception("Vantage rate limit could not read the cache.")
            return ThrottleState(failures=0, limit=self.limit, retry_after=0)
        return ThrottleState(failures=int(count), limit=self.limit, retry_after=self.window)


def rate_limit_for(surface: str) -> RateLimit | None:
    """The configured budget for ``surface``, or ``None`` if it has none.

    ``VANTAGE["RATE_LIMITS"]`` *merges* over :data:`RATE_LIMITS` rather than replacing
    it. Replacing would mean a project that wanted to loosen exports silently switched
    off autocomplete and import as well — a security setting whose obvious edit turns
    two other protections off is a trap, not a knob. Setting a surface to ``None`` or
    ``0`` is how you switch one off, and it has to be written down to happen.
    """
    if not _setting("RATE_LIMIT"):
        return None

    budgets = dict(RATE_LIMITS)
    override = _setting("RATE_LIMITS")
    if isinstance(override, dict):
        budgets.update(override)

    budget = budgets.get(surface)
    if not budget:
        return None
    limit, window = budget
    if not limit or not window:
        return None

    return RateLimit(
        surface=surface,
        limit=int(limit),
        window=int(window),
        cache_alias=str(_setting("RATE_LIMIT_CACHE")),
    )


def actor_for(request) -> str:
    """Who a budget is spent by: the account, or the address if there is no account."""
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return f"user:{user.pk}"
    address = client_ip(request)
    return f"addr:{address}" if address else "addr:unknown"


# ── uploads ─────────────────────────────────────────────────────────────────
#
# The panel accepts files for every FileField and ImageField on every registered model,
# and it is not the panel that decides what happens to them afterwards: they are stored
# and later served, usually from the same origin as the panel itself. That is the whole
# threat. An HTML file that reaches a browser as text/html, from the origin holding the
# session cookie of someone who can edit every table, is not a defaced page — it is a
# script with the panel's authority.


#: Extension -> the content types that extension may legitimately hold. Only image
#: extensions are listed: those are the ones whose files get rendered by a browser
#: rather than downloaded, so those are the ones where a lie about the type matters.
#: A ``.csv`` that is really a zip is a bad import and the importer's problem; a
#: ``.png`` that is really HTML is stored XSS and this module's problem.
IMAGE_SUFFIXES: dict[str, frozenset[str]] = {
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".gif": frozenset({"image/gif"}),
    ".webp": frozenset({"image/webp"}),
    ".bmp": frozenset({"image/bmp"}),
    ".ico": frozenset({"image/x-icon"}),
    ".tif": frozenset({"image/tiff"}),
    ".tiff": frozenset({"image/tiff"}),
    ".svg": frozenset({"image/svg+xml"}),
}

#: Leading bytes that identify a format. Ordered longest-first where two overlap, so a
#: prefix cannot claim a match that belongs to a longer signature.
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"#!", "text/x-shellscript"),
    (b"\x7fELF", "application/x-executable"),
    (b"MZ", "application/x-dosexec"),
)

#: Text that makes a browser treat a file as a document regardless of its extension,
#: which is exactly what ``X-Content-Type-Options: nosniff`` exists to stop and exactly
#: what is still true for anything served with a guessed ``Content-Type``.
_MARKUP_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"<!doctype html", "text/html"),
    (b"<html", "text/html"),
    (b"<head", "text/html"),
    (b"<script", "text/html"),
    (b"<svg", "image/svg+xml"),
)

#: Constructs that turn an uploaded SVG into stored XSS.
#:
#: A rejection filter, not a sanitiser: it refuses what it does not understand rather
#: than rewriting it into safety, because a rewriter that is wrong once is worse than a
#: refusal that is occasionally annoying.
SVG_HAZARDS: tuple[tuple[re.Pattern[bytes], str], ...] = (
    (re.compile(rb"<\s*(?:[a-z0-9]+:)?script\b", re.IGNORECASE), "a <script> element"),
    (re.compile(rb"\son[a-z]+\s*=", re.IGNORECASE), "an inline event handler"),
    (
        re.compile(rb"attributeName\s*=\s*[\"']?\s*on", re.IGNORECASE),
        "an animation that writes an event handler",
    ),
    (re.compile(rb"javascript\s*:", re.IGNORECASE), "a javascript: URL"),
    (
        re.compile(rb"<\s*(?:[a-z0-9]+:)?(?:foreignObject|iframe|embed|object)\b", re.IGNORECASE),
        "an embedded document",
    ),
)

#: Enough to hold every signature and to see the start of a markup document past a BOM
#: and some whitespace. Deliberately small — this is read on every upload.
SNIFF_BYTES = 1024


def sniff(head: bytes) -> str | None:
    """The content type ``head`` actually is, or ``None`` when nothing is recognised.

    ``None`` means "no opinion", not "safe": a CSV, a plain-text file and a font all
    land there. Callers must treat it as the absence of evidence, which is why
    :func:`inspect_upload` only ever *refuses* on a positive identification.
    """
    if not head:
        return None
    for signature, content_type in SIGNATURES:
        if head.startswith(signature):
            return content_type
    # RIFF____WEBP — the size sits between the two markers, so this cannot be a prefix.
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"

    # Markup after a BOM and any leading whitespace, which is how a browser reads it and
    # therefore the only reading that matters here.
    text = head.lstrip(b"\xef\xbb\xbf").lstrip()
    if text.startswith(b"<?xml"):
        # An XML declaration says nothing by itself; look at the first element.
        after = text.split(b">", 1)[1].lstrip() if b">" in text else b""
        text = after
    lowered = text[:64].lower()
    for prefix, content_type in _MARKUP_PREFIXES:
        if lowered.startswith(prefix):
            return content_type
    return None


def inspect_upload(upload, *, name: str | None = None, max_bytes: int | None = None) -> list[str]:
    """Everything wrong with one uploaded file, in sentences fit to show a user.

    An empty list means nothing was found — which is not the same as "this file is
    safe", and the docs say so. Three classes of problem are reported:

    * **Size.** Enforced here as well as at the web server, because the web server's
      limit is the one that is missing on the deployment that needed it.
    * **A content type that contradicts the extension.** Only for image extensions, and
      only when the content was positively identified. An extension is a claim made by
      whoever uploaded the file; the first bytes are the only part of it they had to
      commit to.
    * **Executable markup.** HTML content under any extension, and an SVG containing
      script. Both run with the panel's origin the moment the file is served back.

    The file is left where it was found — position is restored — because this may run
    before validation, before the form, and before anything else has read it.
    """
    problems: list[str] = []
    name = name if name is not None else (getattr(upload, "name", "") or "")
    suffix = Path(name).suffix.lower()

    ceiling = int(max_bytes if max_bytes is not None else _setting("UPLOAD_MAX_BYTES") or 0)
    size = int(getattr(upload, "size", 0) or 0)
    oversized = bool(ceiling and size > ceiling)
    if oversized:
        problems.append(
            f"{name or 'That file'} is {size // 1024} KB; the limit is {ceiling // 1024} KB."
        )

    head = _head_of(upload)
    found = sniff(head)

    expected = IMAGE_SUFFIXES.get(suffix)
    if expected and found and found not in expected:
        problems.append(
            f"{name or 'That file'} is named {suffix} but its contents are {found}. "
            "The extension is a claim; the bytes are not."
        )

    if found == "text/html" and suffix != ".html":
        problems.append(
            f"{name or 'That file'} contains HTML. Served back from the panel's own "
            "origin it would run as a page with the reader's session."
        )

    if (suffix == ".svg" or found == "image/svg+xml") and not oversized:
        # The whole file, not the first kilobyte: a script tag at the end of an SVG runs
        # exactly as well as one at the start. Bounded by the size ceiling above, which
        # is why this is skipped for a file already refused for being too large.
        payload = head if len(head) >= size else _all_of(upload)
        for pattern, description in SVG_HAZARDS:
            if pattern.search(payload):
                problems.append(
                    f"{name or 'That SVG'} contains {description}, which would run as "
                    "script on every page that shows it."
                )
                break

    return problems


def _head_of(upload) -> bytes:
    """The first :data:`SNIFF_BYTES` of an upload, leaving the position where it was."""
    return _read_of(upload, SNIFF_BYTES)


def _all_of(upload) -> bytes:
    """The whole upload. Only used for SVG, which is already capped by size."""
    return _read_of(upload, None)


def _read_of(upload, length: int | None) -> bytes:
    read = getattr(upload, "read", None)
    if read is None:
        return b""
    position = None
    try:
        position = upload.tell()
    except Exception:  # pragma: no cover - a stream that cannot report a position
        position = None
    try:
        upload.seek(0)
        payload = read() if length is None else read(length)
    except Exception:  # pragma: no cover - an upload that cannot be re-read
        logger.exception("Could not read an upload to inspect it.")
        return b""
    finally:
        try:
            upload.seek(position or 0)
        except Exception:  # pragma: no cover
            pass
    if isinstance(payload, str):  # a file opened in text mode
        payload = payload.encode("utf-8", "replace")
    return payload or b""
