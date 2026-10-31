"""The shipped locale catalogues actually work end to end.

Not "the .po file exists" — a real page renders with ``dir="rtl"`` under Arabic, a
known UI string comes back translated, and ``ngettext`` threads a count through the
correct one of Arabic's six plural categories (zero/one/two/few/many/other), not
just "singular or plural" the way English would hide a bug in.

The Arabic catalogues are complete (``TestArabicCataloguesAreComplete``); a string
added to the source after the last catalogue refresh falls back to English until it
is translated. See ``docs/internationalization.md``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.urls import resolve, reverse
from django.utils import translation
from django.utils.translation import gettext, ngettext
from django.views.i18n import JavaScriptCatalog

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clear_view_cache():
    """The jsi18n route is wrapped in cache_page() (see TestTheJavaScriptCatalogueRoute
    below) and this test settings module has no LocaleMiddleware to stamp a
    ``Vary: Accept-Language`` header onto its response — so nothing here makes the
    per-view cache key language-aware, and a test in English would otherwise poison
    the cache for the very next test that overrides to Arabic against the same URL.
    In production this is not a gap: a project using LocaleMiddleware gets that Vary
    header for free (see docs/internationalization.md), which is what makes
    cache_page()'s own built-in Vary-aware keying correct there. This fixture only
    stands in for the header a real per-request language negotiation would send.
    """
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _catalog_of(body: str) -> dict:
    """The ``newcatalog`` object JavaScriptCatalog's response embeds, as real data.

    Django writes it out with ``json.dumps(..., ensure_ascii=True)`` (see
    ``django/views/i18n.py``), so an Arabic value in the raw response body is a run
    of ``\\uXXXX`` escapes, never the literal characters — matching against those
    literal characters directly would silently never find anything. Extracting the
    object and parsing it as the JSON it actually is sidesteps that entirely, and
    reads what a project's own JavaScript would actually receive at ``django.catalog``.
    """
    match = re.search(r"const newcatalog = (\{.*?\n\s*\});", body, re.DOTALL)
    assert match, "no `const newcatalog = {...};` block in the catalogue response"
    return json.loads(match.group(1))


@pytest.fixture
def staff(db):
    return User.objects.create_superuser("i18n", "i18n@example.com", "x")


@pytest.fixture
def client_in(client, staff):
    client.force_login(staff)
    return client


# ── rendering ─────────────────────────────────────────────────────────────────


class TestArabicPageIsRTL:
    def test_dashboard_sets_lang_and_dir_under_arabic(self, client_in):
        with translation.override("ar"):
            body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert '<html lang="ar"' in body
        assert 'dir="rtl"' in body

    def test_default_english_page_carries_no_rtl_direction(self, client_in):
        """The control case: nothing above is hardcoded to always fire."""
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert '<html lang="en' in body
        assert 'dir="rtl"' not in body


# ── translation ──────────────────────────────────────────────────────────────


class TestKnownStringsComeBackInArabic:
    """Strings picked from base.html: rendered on every panel page, unconditionally,
    so this cannot pass by accident of which model or page happened to be requested.
    """

    def test_skip_link_is_translated_on_a_real_page(self, client_in):
        with translation.override("ar"):
            body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "تخطي إلى المحتوى الرئيسي" in body
        assert "Skip to main content" not in body

    def test_nav_landmark_label_is_translated_on_a_real_page(self, client_in):
        with translation.override("ar"):
            body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert 'aria-label="الأقسام"' in body

    def test_gettext_direct(self):
        with translation.override("ar"):
            assert gettext("Sign in") == "تسجيل الدخول"
            assert gettext("Cancel") == "إلغاء"

    def test_a_string_missing_from_the_catalogue_falls_back_to_english(self):
        """A source string added after the last catalogue refresh is simply absent
        from the compiled catalogue; it must render as the source text, never blank."""
        with translation.override("ar"):
            assert gettext("A string no catalogue has ever heard of") == (
                "A string no catalogue has ever heard of"
            )

    def test_a_formerly_untranslated_string_is_translated_now(self):
        with translation.override("ar"):
            assert gettext("Row density") != "Row density"


# ── plurals ──────────────────────────────────────────────────────────────────


class TestNgettextPluralisesThroughArabicsSixForms:
    """``%(count)s selected record`` / ``records`` (actions.py) is the one plural
    entry translated end to end in the Arabic catalogue, specifically so this test
    can prove ``ngettext`` picks Arabic's CLDR category for the count — not just
    "singular vs. plural" the way testing only English would let slip through.
    """

    @pytest.mark.parametrize(
        "count, expected",
        [
            (0, "لا سجلات محددة"),  # zero
            (1, "سجل واحد محدد"),  # one
            (2, "سجلان محددان"),  # two
            (5, "5 سجلات محددة"),  # few: 3-10
            (11, "11 سجلاً محدداً"),  # many: 11-99
            (100, "100 سجل محدد"),  # other
        ],
    )
    def test_each_arabic_plural_category(self, count, expected):
        with translation.override("ar"):
            text = ngettext("%(count)s selected record", "%(count)s selected records", count) % {
                "count": count
            }
        assert text == expected

    def test_distinct_counts_are_not_all_squashed_to_one_form(self):
        """A regression this specific: a broken Plural-Forms expression (or a
        catalogue compiled for the wrong nplurals) tends to fail by picking the
        *same* form for every count rather than by erroring outright."""
        with translation.override("ar"):
            forms = {
                ngettext("%(count)s selected record", "%(count)s selected records", n)
                for n in (0, 1, 2, 5, 11, 100)
            }
        assert len(forms) == 6

    def test_english_still_has_only_two_forms(self):
        with translation.override("en"):
            one = ngettext("%(count)s selected record", "%(count)s selected records", 1)
            many = ngettext("%(count)s selected record", "%(count)s selected records", 5)
        assert (one % {"count": 1}) == "1 selected record"
        assert (many % {"count": 5}) == "5 selected records"


# ── the JavaScript catalogue ────────────────────────────────────────────────
#
# combobox.js, vantage-inline.js and friends call gettext()/ngettext() now, but
# calling them is only half the feature: something has to serve the catalogue those
# functions read at runtime, scoped to this package, and cached. These tests exercise
# the mounted route and the compiled djangojs.mo behind it — not the Python-side
# gettext() used above, which is a different domain ("django") and would report a
# translated string here even if the JS catalogue itself were completely broken.


class TestTheJavaScriptCatalogueRoute:
    def test_it_is_scoped_to_the_vantage_package_only(self):
        """Not the whole project's — a project's own strings are not this package's
        to serve. ``packages=["vantage"]`` is what makes JavaScriptCatalog read only
        vantage/locale/**, never LOCALE_PATHS or another installed app's locale/."""
        match = resolve(reverse("vantage:jsi18n"))
        assert match.func.view_class is JavaScriptCatalog
        assert match.func.view_initkwargs == {"packages": ["vantage"]}

    def test_it_is_reachable_and_returns_javascript(self, client, db):
        # No login required, deliberately: a project's own login page may one day
        # want a translated client-side validation message before anyone is
        # authenticated, the same way Django's own jsi18n is conventionally public.
        response = client.get(reverse("vantage:jsi18n"))
        assert response.status_code == 200
        assert "javascript" in response["Content-Type"]

    def test_it_is_cached(self, client, db):
        """cache_page() is what makes this affordable — the catalogue is identical
        for every request in one language, and building it means walking every
        compiled .mo file's plural forms on every hit if it is not cached."""
        response = client.get(reverse("vantage:jsi18n"))
        # cache_page() calls patch_response_headers(), which is what actually stamps
        # Cache-Control/Expires — asserting on those, not merely "it responded",
        # is the difference between "cached" and "happens to work".
        assert "max-age" in response.get("Cache-Control", "")
        assert "Expires" in response


class TestTheJavaScriptCatalogueTranslatesArabic:
    """The catalogue's own content, not the "django" domain gettext() above — this
    is what proves djangojs.mo actually compiled and actually ships translations,
    not just that Python-side i18n (already covered above) works.
    """

    def test_a_known_js_string_comes_back_in_arabic(self, client, db):
        with translation.override("ar"):
            body = client.get(reverse("vantage:jsi18n")).content.decode()
        # "Selection cleared" (combobox.js)
        assert _catalog_of(body)["Selection cleared"] == "تم مسح التحديد"

    def test_default_english_carries_the_english_source_not_arabic(self, client, db):
        body = client.get(reverse("vantage:jsi18n")).content.decode()
        assert _catalog_of(body)["Selection cleared"] == "Selection cleared"

    def test_a_formerly_untranslated_js_string_is_translated_now(self, client, db):
        with translation.override("ar"):
            body = client.get(reverse("vantage:jsi18n")).content.decode()
        # "this row" (vantage-listprefs.js) used to fall back to English.
        assert _catalog_of(body)["this row"] != "this row"

    def test_the_flagship_plural_carries_all_six_arabic_forms(self, client, db):
        """``%(count)s change`` / ``%(count)s changes`` — the "1 changes" bug this
        whole exercise is about — round-tripped through the compiled catalogue that
        ships to the browser, in every one of Arabic's six plural categories."""
        with translation.override("ar"):
            body = client.get(reverse("vantage:jsi18n")).content.decode()
        forms = _catalog_of(body)["%(count)s change"]
        assert forms == [
            "لا تغييرات",  # zero
            "تغيير واحد",  # one
            "تغييران",  # two
            "%(count)s تغييرات",  # few (3-10)
            "%(count)s تغييراً",  # many (11-99)
            "%(count)s تغيير",  # other (100+)
        ]
        # A regression this specific fails by collapsing every count onto one form.
        assert len(set(forms)) == 6


# ── completeness ─────────────────────────────────────────────────────────────


def _compiled_catalogue(locale: str, domain: str) -> dict:
    """The compiled ``.mo`` as gettext reads it. An entry with an empty msgstr (or a
    fuzzy one) is left out of the ``.mo`` by msgfmt, so "present and non-empty in the
    compiled catalogue" is exactly "translated"."""
    import gettext as gettext_module

    path = REPO_ROOT / "src/vantage/locale" / locale / "LC_MESSAGES" / f"{domain}.mo"
    with path.open("rb") as fh:
        return gettext_module.GNUTranslations(fh)._catalog


@pytest.mark.parametrize("domain", ["django", "djangojs"])
class TestArabicCataloguesAreComplete:
    """``en`` is an identity catalogue, so it lists every extracted msgid; ``ar`` has
    to carry a real translation for each of them, and all six forms of every plural."""

    def test_every_english_message_has_an_arabic_translation(self, domain):
        def msgids(catalogue: dict) -> set[str]:
            return {k[0] if isinstance(k, tuple) else k for k in catalogue} - {""}

        missing = sorted(
            msgids(_compiled_catalogue("en", domain)) - msgids(_compiled_catalogue("ar", domain))
        )
        assert not missing, f"untranslated in ar/{domain}: {missing[:5]}"

    def test_every_arabic_plural_has_all_six_forms(self, domain):
        arabic = _compiled_catalogue("ar", domain)
        forms: dict[str, set[int]] = {}
        for key, value in arabic.items():
            if isinstance(key, tuple):
                assert value.strip(), f"empty plural form {key} in ar/{domain}"
                forms.setdefault(key[0], set()).add(key[1])
        assert forms, "expected at least one plural entry"
        short = {m: sorted(f) for m, f in forms.items() if f != set(range(6))}
        assert not short, f"plurals without all six Arabic forms: {list(short)[:3]}"


# ── graceful degradation ─────────────────────────────────────────────────────


class TestTheShellDegradesWithoutThrowing:
    """A project that has not mounted the catalogue route at all, or whose request
    for it 404s or drops on the way, must not get a page where every deferred
    script after it throws "gettext is not defined" on load. See
    vantage-i18n-fallback.js and the comment above it in base.html.
    """

    @pytest.fixture
    def staff(self, db):
        return User.objects.create_superuser("i18n2", "i18n2@example.com", "x")

    def test_base_html_loads_the_catalogue_before_the_fallback_before_vantage_js(
        self, client, staff
    ):
        client.force_login(staff)
        body = client.get(reverse("vantage:dashboard")).content.decode()

        catalogue_url = reverse("vantage:jsi18n")
        catalogue_at = body.find(catalogue_url)
        fallback_at = body.find("vantage-i18n-fallback.js")
        vantage_js_at = body.find("vantage/vantage.js")

        assert -1 not in (catalogue_at, fallback_at, vantage_js_at)
        assert catalogue_at < fallback_at < vantage_js_at

    def test_the_catalogue_script_and_the_fallback_are_not_deferred(self, client, staff):
        """Deferred scripts run after parsing, in document order — but only after
        every non-deferred script the parser already ran. These two have to be in
        that first group, or a deferred script earlier in the document than expected
        (a template override, a plugin) could still run before gettext() exists."""
        client.force_login(staff)
        body = client.get(reverse("vantage:dashboard")).content.decode()
        catalogue_line = next(
            line for line in body.splitlines() if "vantage:jsi18n" in line or "/_/jsi18n/" in line
        )
        fallback_line = next(
            line for line in body.splitlines() if "vantage-i18n-fallback.js" in line
        )
        assert "defer" not in catalogue_line
        assert "defer" not in fallback_line


# ── the compiled catalogue actually ships ───────────────────────────────────


class TestCompiledCataloguesShipInTheWheel:
    """`.mo` files are a build artefact `hatchling` will not include just because
    they sit in the source tree — see the `artifacts` setting in `pyproject.toml`.
    This builds a real wheel and looks inside the zip, rather than trusting that
    the config means what it says.
    """

    def test_mo_files_present_in_a_built_wheel(self, tmp_path):
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv is not on PATH; cannot build a wheel to inspect")

        result = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

        wheels = list(tmp_path.glob("*.whl"))
        assert wheels, f"no wheel produced in {tmp_path}"

        with zipfile.ZipFile(wheels[0]) as z:
            names = z.namelist()

        # Both domains: "django" (Python + templates) and "djangojs" (the JS
        # catalogue JavaScriptCatalog serves — see TestTheJavaScriptCatalogueRoute
        # above). A wheel that shipped one and silently dropped the other would
        # still pass every test above, which runs against the source tree, not
        # a built and reinstalled package.
        for locale in ("en", "ar"):
            for domain in ("django", "djangojs"):
                po = f"vantage/locale/{locale}/LC_MESSAGES/{domain}.po"
                mo = f"vantage/locale/{locale}/LC_MESSAGES/{domain}.mo"
                assert po in names, f"{po} missing from the built wheel"
                assert mo in names, f"{mo} missing from the built wheel — .mo is not shipping"
