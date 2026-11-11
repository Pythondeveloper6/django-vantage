"""Rich-text hardening: the policy floor, scheme variants, and every write path.

The sanitiser is the guarantee, so these tests attack it from each side: the settings a
project could use to widen it, the encodings an attacker could use to hide a URL scheme,
and each way a value reaches the database (form, API, CSV import, bulk edit, inline
cell), plus the render step that protects rows written by anything else.
"""

import json
import re

import pytest
from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import path, reverse

from tests.testapp.models import ZArticle
from vantage import inline, richtext, site
from vantage.conf import vantage_settings
from vantage.importviews import build_import_urls
from vantage.panels import ModelPanel
from vantage.records import BULK_EDIT_ENABLE, BULK_EDIT_PREFIX
from vantage.render import render_cell
from vantage.widgets import build_form_class


def _patterns():
    from vantage import urls as vantage_urls

    built = vantage_urls.build(site)
    mounted = {getattr(p, "name", None) for p in built}
    extra = [] if f"{site.get_panel(ZArticle).slug}-inline" in mounted else inline.build_urls(site)
    return (build_import_urls(site) + extra + built, "vantage", "vantage")


urlpatterns = [path("admin/", _patterns())]
pytestmark = pytest.mark.urls("tests.test_richtext_security")

ACTIVE = re.compile(
    r"<script|<iframe|<style|<svg|<math|<object|<embed|<form|<input|<template|<base|"
    r"<noscript|<textarea|<xmp|onerror|onclick|onload|javascript:|vbscript:|data:|style=",
    re.IGNORECASE,
)

SCHEME_PAYLOADS = [
    "javascript:alert(1)",
    "JAVASCRIPT:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "\tjavascript:alert(1)",
    "\njavascript:alert(1)",
    "\x01javascript:alert(1)",
    "\x1fjavascript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "java\rscript:alert(1)",
    "jav&#x09;ascript:alert(1)",
    "jav&#x0A;ascript:alert(1)",
    "jav&#x0D;ascript:alert(1)",
    "&#106;avascript:alert(1)",
    "&#x6A;avascript:alert(1)",
    "&#106&#97&#118&#97&#115&#99&#114&#105&#112&#116&#58;alert(1)",
    "javascript&colon;alert(1)",
    "javascript&#58;alert(1)",
    "javascript&#x3A;alert(1)",
    "&#x20;javascript:alert(1)",
    "\x00javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "DATA:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "da&#x09;ta:text/html,x",
    " data:text/html,x",
    "vbscript:msgbox(1)",
    "VBScript:msgbox(1)",
    "vb\tscript:msgbox(1)",
    "&#118;bscript:msgbox(1)",
    "blob:https://example.com/x",
    "file:///etc/passwd",
]

ACTIVE_MARKUP = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil/x.js></SCRIPT>",
    "<style>@import 'x';</style>",
    "<svg onload=alert(1)>",
    "<svg><a xlink:href='javascript:alert(1)'><text>x</text></a></svg>",
    "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>",
    "<noscript><p title='</noscript><img src=x onerror=alert(1)>'>",
    "<iframe srcdoc='<script>alert(1)</script>'></iframe>",
    "<template><script>alert(1)</script></template>",
    "<base href='//evil/'>",
    "<form action='//evil'><button formaction='//evil'>go</button></form>",
    "<xmp><img src=x onerror=alert(1)></xmp>",
    "<textarea></textarea><img src=x onerror=alert(1)>",
    "<img src=x onerror=alert(1)>",
    "<a href='x' onmouseover=alert(1)>x</a>",
]


def _tags(out):
    """Only the real markup in ``out``: escaped text such as ``&lt;img onerror=..&gt;`` is inert."""
    return " ".join(re.findall(r"<[^>]*>", out))


def _hrefs(out):
    return [re.sub(r"[\x00-\x20]", "", h).lower() for h in re.findall(r'href="([^"]*)"', out)]


def _staff(client):
    user = User.objects.create_superuser("editor", "editor@example.com", "x")
    client.force_login(user)
    return user


def _invalid(config):
    with override_settings(VANTAGE=config):
        vantage_settings.reset()
        with pytest.raises(ImproperlyConfigured):
            vantage_settings.SITE_TITLE  # noqa: B018


# ── the policy floor ────────────────────────────────────────────────────────


class TestPolicyFloor:
    @pytest.mark.parametrize("tag", sorted(richtext.FORBIDDEN_TAGS))
    def test_every_forbidden_tag_is_refused_as_an_extra_tag(self, tag):
        _invalid({"RICHTEXT_EXTRA_TAGS": [tag]})

    @pytest.mark.parametrize(
        "tag",
        [
            "xmp", "noembed", "noframes", "plaintext", "listing", "textarea", "noscript",
            "portal", "marquee", "template", "title", "math", "svg", "object", "embed",
            "applet", "base", "meta", "link", "frame", "frameset", "iframe", "script",
            "style",
        ],
    )  # fmt: skip
    def test_the_reviewed_tag_set_is_covered(self, tag):
        assert tag in richtext.FORBIDDEN_TAGS
        _invalid({"RICHTEXT_EXTRA_TAGS": [tag]})

    @pytest.mark.parametrize(
        "attribute",
        [
            "srcset", "background", "poster", "ping", "action", "formaction", "data",
            "xlink:href", "srcdoc", "autofocus", "contenteditable", "tabindex", "accesskey",
            "draggable", "is", "slot", "style", "class", "id", "name", "onerror", "onload",
            "ONCLICK", "SrcSet", "Poster",
        ],
    )  # fmt: skip
    @pytest.mark.parametrize("tag", ["img", "*"])
    def test_forbidden_attributes_are_refused(self, tag, attribute):
        _invalid({"RICHTEXT_EXTRA_ATTRIBUTES": {tag: [attribute]}})

    def test_harmless_attributes_are_still_allowed(self):
        config = {
            "RICHTEXT_EXTRA_TAGS": ["table", "tr", "td", "img"],
            "RICHTEXT_EXTRA_ATTRIBUTES": {
                "td": ["colspan", "rowspan"],
                "img": ["src", "alt", "width", "height"],
            },
        }
        with override_settings(VANTAGE=config):
            vantage_settings.reset()
            out = richtext.sanitize_html('<table><tr><td colspan="2" tabindex="0">c</td></tr>')
            assert 'colspan="2"' in out and "tabindex" not in out
        vantage_settings.reset()

    @pytest.mark.parametrize(
        "src", ["javascript:1", "JavaScript:1", "data:image/svg+xml,<svg onload=x>", "vbscript:1"]
    )
    def test_an_allowed_img_src_is_scheme_checked(self, src):
        config = {
            "RICHTEXT_EXTRA_TAGS": ["img"],
            "RICHTEXT_EXTRA_ATTRIBUTES": {"img": ["src", "alt"]},
        }
        with override_settings(VANTAGE=config):
            vantage_settings.reset()
            out = richtext.sanitize_html(f'<img src="{src}" alt="a">')
            assert "src=" not in out and "javascript" not in out.lower()
            ok = richtext.sanitize_html('<img src="https://example.com/a.png" alt="a">')
            assert 'src="https://example.com/a.png"' in ok
        vantage_settings.reset()


# ── scheme and markup payloads ──────────────────────────────────────────────


class TestSchemePayloads:
    @pytest.mark.parametrize("payload", SCHEME_PAYLOADS)
    def test_href_with_a_dangerous_scheme_is_dropped(self, payload):
        out = richtext.sanitize_html(f'<a href="{payload}">click</a>')
        assert "click" in out
        # A NUL becomes U+FFFD, which makes the value a relative path, not a scheme.
        for href in _hrefs(out):
            assert not re.match(r"^(javascript|vbscript|data|blob|file):", href), out

    @pytest.mark.parametrize("payload", SCHEME_PAYLOADS)
    def test_the_same_payload_unquoted_or_in_an_image_is_inert(self, payload):
        out = richtext.sanitize_html(f"<a href={payload.split()[0] if payload.strip() else 'x'}>x")
        out += richtext.sanitize_html(f'<img src="{payload}">')
        assert "src=" not in out
        for href in _hrefs(out):
            assert not re.match(r"^(javascript|vbscript|data|blob|file):", href), out

    @pytest.mark.parametrize("payload", ACTIVE_MARKUP)
    def test_active_markup_yields_no_active_content(self, payload):
        out = richtext.sanitize_html(f"<p>keep</p>{payload}")
        assert not ACTIVE.search(_tags(out)), out
        assert "<p>keep</p>" in out
        # Sanitising its own output changes nothing, so a second pass cannot revive it.
        assert richtext.sanitize_html(out) == out


# ── the render step and the cells ───────────────────────────────────────────


@pytest.mark.django_db
class TestRenderTime:
    def test_a_poisoned_row_renders_inert_and_the_stored_value_is_untouched(self, client):
        _staff(client)
        raw = '<p>fine</p><script>alert(1)</script><img src=x onerror="alert(2)">'
        article = ZArticle.objects.create(title="Poisoned", summary_html="x", body_html=raw)
        page = client.get(reverse("vantage:testapp-zarticle-detail", args=[article.pk]))
        content = page.content.decode()
        assert "<p>fine</p>" in content
        assert "<script>alert" not in content
        assert "onerror" not in content
        article.refresh_from_db()
        assert article.body_html == raw

    def test_a_list_cell_is_plain_text_only(self):
        article = ZArticle(
            title="t",
            summary_html="x",
            body_html='<p>Hi <a href="javascript:alert(1)">there</a></p><script>x()</script>',
        )
        cell = str(render_cell(site.get_panel(ZArticle), article, "body_html"))
        assert cell == "Hi there"
        assert "<" not in cell and "javascript" not in cell


# ── every way a value reaches the database ──────────────────────────────────

DIRTY = '<p>ok</p><script>alert(1)</script><img src=x onerror="alert(2)">'


def _assert_clean(value):
    assert value == "<p>ok</p>" or value == '<p>ok</p><img src="x">', value
    assert not ACTIVE.search(value)


@pytest.mark.django_db
class TestWritePaths:
    def test_json_api(self, client):
        _staff(client)
        url = reverse("vantage:api-list", kwargs={"app_label": "testapp", "model_name": "zarticle"})
        response = client.post(
            url,
            data=json.dumps(
                {"fields": {"title": "Api", "summary_html": "<p>s</p>", "body_html": DIRTY}}
            ),
            content_type="application/json",
        )
        assert response.status_code in (200, 201), response.content[:300]
        _assert_clean(ZArticle.objects.get(title="Api").body_html)

    def test_csv_import(self, client):
        _staff(client)
        raw = ("title,summary_html,body_html\nImported,<p>s</p>," + _csv(DIRTY) + "\n").encode()
        started = client.post(
            reverse("vantage:testapp-zarticle-import"),
            {"file": SimpleUploadedFile("a.csv", raw, content_type="text/csv")},
        )
        assert started.status_code == 302
        mapped = client.get(started["Location"])
        data = {f"map-{c['index']}": c["target"] for c in mapped.context["columns"]}
        data["key_field"] = mapped.context["default_key"]
        data["mode"] = mapped.context["default_mode"]
        previewed = client.post(mapped.context["preview_url"], data)
        hidden = {f["name"]: f["value"] for f in previewed.context["hidden"]}
        client.post(previewed.context["commit_url"], hidden)
        _assert_clean(ZArticle.objects.get(title="Imported").body_html)

    def test_bulk_edit(self, client):
        _staff(client)
        article = ZArticle.objects.create(title="Bulk", summary_html="x", body_html="<p>old</p>")
        response = client.post(
            reverse("vantage:testapp-zarticle-bulk-edit"),
            {
                "selected": [str(article.pk)],
                "_bulk_apply": "1",
                BULK_EDIT_ENABLE: ["body_html"],
                f"{BULK_EDIT_PREFIX}-body_html": DIRTY,
            },
        )
        assert response.status_code in (200, 302), response.status_code
        article.refresh_from_db()
        _assert_clean(article.body_html)

    def test_inline_row_cannot_write_a_rich_text_cell_at_all(self, client, monkeypatch):
        """Rich text is not an inline-editable kind; a forged cell is dropped, not stored."""
        _staff(client)
        monkeypatch.setattr(site.get_panel(ZArticle), "list_editable", ["body_html"])
        article = ZArticle.objects.create(title="Inline", summary_html="x", body_html="<p>old</p>")
        from vantage import concurrency

        response = client.patch(
            reverse("vantage:testapp-zarticle-inline"),
            data=json.dumps(
                {
                    "changes": [
                        {
                            "pk": article.pk,
                            "version": concurrency.token_for(article),
                            "fields": {"body_html": DIRTY},
                        }
                    ]
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert json.loads(response.content)["saved"] == 0
        article.refresh_from_db()
        assert article.body_html == "<p>old</p>"


def _csv(value):
    return '"' + value.replace('"', '""') + '"'


# ── a substituted form field class ──────────────────────────────────────────


class _AnyField(forms.Field):
    """A stand-in for a project's own non-CharField form field."""

    def __init__(self, *args, max_length=None, empty_value="", **kwargs):
        super().__init__(*args, **kwargs)


class TestSubstitutedFormField:
    def test_a_non_charfield_override_is_still_sanitised(self):
        from django.db import models

        panel = type(
            "P",
            (ModelPanel,),
            {"formfield_overrides": {models.TextField: {"form_class": _AnyField}}},
        )(ZArticle)
        form = build_form_class(panel)(
            data={"title": "T", "body_html": DIRTY, "notes_html": "", "summary_html": "<p>s</p>"}
        )
        assert form.is_valid(), form.errors
        _assert_clean(form.cleaned_data["body_html"])
