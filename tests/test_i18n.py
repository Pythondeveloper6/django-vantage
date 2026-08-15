"""The shipped locale catalogues actually work end to end.

Not "the .po file exists" — a real page renders with ``dir="rtl"`` under Arabic, a
known UI string comes back translated, and ``ngettext`` threads a count through the
correct one of Arabic's six plural categories (zero/one/two/few/many/other), not
just "singular or plural" the way English would hide a bug in.

``locale/ar/LC_MESSAGES/django.po`` is a deliberately partial catalogue: the
high-traffic strings used below are translated, the rest is left with an empty
msgstr so it falls back to the English source instead of shipping a guess. See
``docs/internationalization.md``.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext, ngettext

REPO_ROOT = Path(__file__).resolve().parent.parent


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

    def test_an_untranslated_string_falls_back_to_english_not_a_blank(self):
        """The catalogue is intentionally partial. An empty msgstr must fall back
        cleanly to the source string, never render as empty text."""
        with translation.override("ar"):
            # Deliberately not in the curated translation set (see translate_ar in
            # the locale update tooling) — proves the fallback path, not a lucky hit.
            assert gettext("Row density") == "Row density"


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
            text = ngettext(
                "%(count)s selected record", "%(count)s selected records", count
            ) % {"count": count}
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

        for locale in ("en", "ar"):
            po = f"vantage/locale/{locale}/LC_MESSAGES/django.po"
            mo = f"vantage/locale/{locale}/LC_MESSAGES/django.mo"
            assert po in names, f"{po} missing from the built wheel"
            assert mo in names, f"{mo} missing from the built wheel — .mo is not shipping"
