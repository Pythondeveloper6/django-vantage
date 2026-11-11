"""Rich-text fields: sanitised HTML on save, sanitised again on render.

The properties that matter are negative ones (nothing executable survives) and the two
places they have to hold independently: the form, which cleans what a person submits, and
the renderer, which cleans what is already in the table whoever put it there.
"""

import json
import re
from pathlib import Path

import pytest
from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.urls import reverse

from tests.testapp.models import ZArticle
from vantage import richtext, site
from vantage.conf import vantage_settings
from vantage.panels import ModelPanel
from vantage.render import render_cell
from vantage.widgets import (
    FieldKind,
    RichTextWidget,
    build_form_class,
    classify,
    infer_widget,
)

STATIC = Path(__file__).resolve().parent.parent / "src/vantage/static/vantage"

HOSTILE = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    '<p onclick="alert(1)">hi</p>',
    '<a href="javascript:alert(1)">x</a>',
    '<a href="JaVaScRiPt:alert(1)">x</a>',
    '<a href="java\tscript:alert(1)">x</a>',
    '<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>',
    '<a href="vbscript:msgbox(1)">x</a>',
    '<p style="position:fixed;inset:0;background:url(//evil)">cover</p>',
    '<iframe src="//evil.example"></iframe>',
    "<svg><script>alert(1)</script></svg>",
    "<math><mi xlink:href=javascript:alert(1)>x</mi></math>",
    "<style>body{display:none}</style>",
    '<object data="x"></object><embed src="x">',
    '<form action="//evil"><input name=a></form>',
    "<!--[if IE]><script>alert(1)</script><![endif]-->",
    "<scr<script>ipt>alert(1)</scr</script>ipt>",
]

DANGEROUS = re.compile(
    r"<script|<iframe|<style|<svg|<math|<object|<embed|<form|<input|onerror|onclick|"
    r"javascript:|vbscript:|data:|style=|<!--",
    re.IGNORECASE,
)


@pytest.fixture
def staff(client):
    user = User.objects.create_superuser("editor", "editor@example.com", "x")
    client.force_login(user)
    return user


def post_payload(**extra):
    return {"title": "T", "body_html": "", "notes_html": "", "summary_html": "<p>s</p>", **extra}


class TestSanitiser:
    @pytest.mark.parametrize("payload", HOSTILE)
    def test_hostile_markup_is_neutralised(self, payload):
        out = richtext.sanitize_html(f"<p>keep</p>{payload}")
        assert not DANGEROUS.search(out), out
        assert "<p>keep</p>" in out

    def test_script_content_is_dropped_not_left_as_text(self):
        assert "alert" not in richtext.sanitize_html("<p>a</p><script>alert(1)</script>")
        assert "display" not in richtext.sanitize_html("<style>p{display:none}</style><p>a</p>")

    def test_event_handlers_and_style_attributes_are_stripped(self):
        out = richtext.sanitize_html('<p style="color:red" onmouseover="x()" class="c">t</p>')
        assert out == "<p>t</p>"

    def test_javascript_urls_are_stripped_but_the_link_text_stays(self):
        out = richtext.sanitize_html('<a href="javascript:alert(1)">click</a>')
        assert "javascript" not in out
        assert "click" in out

    def test_the_editors_own_output_survives_untouched(self):
        html = (
            "<h2>Title</h2><p>One <strong>two</strong> <em>three</em> <u>u</u> <s>s</s> "
            "<code>c</code></p><ul><li>a</li></ul><ol><li>b</li></ol>"
            "<blockquote>q</blockquote><pre>code</pre>"
        )
        assert richtext.sanitize_html(html) == html

    def test_links_keep_href_and_title_and_gain_a_safe_rel(self):
        out = richtext.sanitize_html(
            '<a href="https://example.com/x" title="t" target="_blank">l</a>'
        )
        assert 'href="https://example.com/x"' in out
        assert 'title="t"' in out
        assert 'rel="noopener noreferrer nofollow"' in out
        assert "target" not in out

    @pytest.mark.parametrize("url", ["mailto:a@example.com", "tel:+100", "/relative/path", "#x"])
    def test_other_ordinary_links_are_kept(self, url):
        assert f'href="{url}"' in richtext.sanitize_html(f'<a href="{url}">l</a>')

    def test_sanitising_is_idempotent(self):
        once = richtext.sanitize_html("<p>a &amp; b</p><script>x</script><a href='/x'>l</a>")
        assert richtext.sanitize_html(once) == once

    def test_none_and_blank_become_empty(self):
        assert richtext.sanitize_html(None) == ""
        assert richtext.sanitize_html("  \n") == ""

    def test_text_is_escaped_not_interpreted(self):
        assert richtext.sanitize_html("1 < 2 & 3 > 2") == "1 &lt; 2 &amp; 3 &gt; 2"

    def test_to_text_reads_block_boundaries(self):
        assert richtext.to_text("<p>a</p><p>b &amp; c</p><script>x</script>") == "a b & c"

    def test_an_emptied_editor_counts_as_empty(self):
        assert richtext.is_effectively_empty("<p><br></p>")
        assert not richtext.is_effectively_empty("<p>x</p>")


class TestPolicySettings:
    def test_extra_tags_and_attributes_widen_the_allowlist(self):
        with override_settings(
            VANTAGE={
                "RICHTEXT_EXTRA_TAGS": ["table", "tr", "td"],
                "RICHTEXT_EXTRA_ATTRIBUTES": {"td": ["colspan"], "a": ["target"]},
            }
        ):
            out = richtext.sanitize_html(
                '<table><tr><td colspan="2" onclick="x()">c</td></tr></table>'
            )
        assert out == '<table><tr><td colspan="2">c</td></tr></table>'
        # And the default policy is back once the override ends.
        assert "<table" not in richtext.sanitize_html("<table><tr><td>c</td></tr></table>")

    def test_extra_url_schemes(self):
        with override_settings(VANTAGE={"RICHTEXT_URL_SCHEMES": ["sms"]}):
            assert 'href="sms:+100"' in richtext.sanitize_html('<a href="sms:+100">x</a>')
        assert "sms:" not in richtext.sanitize_html('<a href="sms:+100">x</a>')

    @pytest.mark.parametrize(
        "config",
        [
            {"RICHTEXT_EXTRA_TAGS": ["script"]},
            {"RICHTEXT_EXTRA_TAGS": ["iframe"]},
            {"RICHTEXT_EXTRA_TAGS": "table"},
            {"RICHTEXT_EXTRA_ATTRIBUTES": {"a": ["onclick"]}},
            {"RICHTEXT_EXTRA_ATTRIBUTES": {"p": ["style"]}},
            {"RICHTEXT_EXTRA_ATTRIBUTES": {"*": ["onload"]}},
            {"RICHTEXT_EXTRA_ATTRIBUTES": {"script": ["src"]}},
            {"RICHTEXT_URL_SCHEMES": ["javascript"]},
            {"RICHTEXT_URL_SCHEMES": ["data"]},
            {"RICHTEXT_EDITOR": "wysiwyg"},
        ],
    )
    def test_unsafe_or_malformed_policy_is_refused(self, config):
        with override_settings(VANTAGE=config):
            vantage_settings.reset()
            with pytest.raises(ImproperlyConfigured):
                vantage_settings.SITE_TITLE  # noqa: B018


class TestForm:
    def test_the_inferred_widget_is_the_rich_editor_with_its_hook(self):
        spec = infer_widget(ZArticle._meta.get_field("body_html"))
        assert spec.kind is FieldKind.RICHTEXT
        assert spec.attrs["data-richtext"] == "1"
        assert "data-markdown" not in spec.attrs
        assert isinstance(spec.build_widget(), RichTextWidget)

    def test_markdown_editor_setting_restores_the_toolbar_path(self):
        with override_settings(VANTAGE={"RICHTEXT_EDITOR": "markdown"}):
            spec = infer_widget(ZArticle._meta.get_field("body_html"))
            assert spec.attrs["data-markdown"] == "1"
            assert "data-richtext" not in spec.attrs
            assert not isinstance(spec.build_widget(), RichTextWidget)

    def test_hostile_input_is_sanitised_by_the_form(self):
        form_class = build_form_class(site.get_panel(ZArticle))
        form = form_class(data=post_payload(body_html="<p>ok</p><script>alert(1)</script>"))
        assert form.is_valid(), form.errors
        assert form.cleaned_data["body_html"] == "<p>ok</p>"

    def test_an_emptied_editor_fails_required(self):
        form_class = build_form_class(site.get_panel(ZArticle))
        form = form_class(data=post_payload(summary_html="<p><br></p>"))
        assert not form.is_valid()
        assert "summary_html" in form.errors

    def test_a_sanitiser_that_removes_everything_fails_required_too(self):
        form_class = build_form_class(site.get_panel(ZArticle))
        form = form_class(data=post_payload(summary_html="<script>alert(1)</script>"))
        assert not form.is_valid()

    def test_a_substituted_widget_is_still_sanitised(self):
        panel = type("P", (ModelPanel,), {"widgets": {"body_html": forms.Textarea}})(ZArticle)
        form = build_form_class(panel)(data=post_payload(body_html="<b onclick=x>a</b>"))
        assert form.is_valid(), form.errors
        assert form.cleaned_data["body_html"] == "<b>a</b>"

    def test_markdown_fields_are_stored_as_typed(self):
        panel = type("P", (ModelPanel,), {"markdown_fields": ("notes_html",)})(ZArticle)
        form_class = build_form_class(panel)
        form = form_class(data=post_payload(notes_html="**bold** <b>x</b> & 1 < 2"))
        assert form.is_valid(), form.errors
        assert form.cleaned_data["notes_html"] == "**bold** <b>x</b> & 1 < 2"
        widget = form_class().fields["notes_html"].widget
        assert not isinstance(widget, RichTextWidget)
        assert widget.attrs["data-markdown"] == "1"
        assert isinstance(form_class().fields["body_html"].widget, RichTextWidget)

    def test_a_markdown_field_class_keeps_the_markdown_toolbar(self):
        from django.db import models

        class MarkdownField(models.TextField):
            pass

        class ZNote(models.Model):
            text = MarkdownField()

            class Meta:
                app_label = "testapp"

        spec = infer_widget(ZNote._meta.get_field("text"))
        assert spec.kind is FieldKind.RICHTEXT
        assert spec.attrs["data-markdown"] == "1"
        form = build_form_class(ZNote)(data={"text": "<b>raw</b> **md**"})
        assert form.is_valid()
        assert form.cleaned_data["text"] == "<b>raw</b> **md**"

    def test_the_widget_sanitises_stored_value_before_it_reaches_the_editor(self):
        html = RichTextWidget().render("body_html", "<p>a</p><script>alert(1)</script>")
        assert "<script" not in html
        assert "&lt;p&gt;a&lt;/p&gt;" in html


@pytest.mark.django_db
class TestPages:
    def test_round_trip_through_the_change_form(self, client, staff):
        url = reverse("vantage:testapp-zarticle-add")
        response = client.post(
            url,
            post_payload(
                title="Round trip",
                body_html='<h2>Hi</h2><p>x <a href="https://example.com">l</a></p>'
                "<script>alert(1)</script>",
            ),
        )
        assert response.status_code == 302, response.content[:500]
        article = ZArticle.objects.get(title="Round trip")
        assert "<script" not in article.body_html
        assert "<h2>Hi</h2>" in article.body_html

        page = client.get(reverse("vantage:testapp-zarticle-change", args=[article.pk]))
        assert page.status_code == 200
        content = page.content.decode()
        assert "data-richtext" in content
        # The stored HTML is in the textarea escaped, never as live markup.
        assert "&lt;h2&gt;Hi&lt;/h2&gt;" in content

    def test_change_form_escapes_a_poisoned_row(self, client, staff):
        article = ZArticle.objects.create(
            title="Poisoned", summary_html="x", body_html="</textarea><script>alert(1)</script>"
        )
        content = client.get(
            reverse("vantage:testapp-zarticle-change", args=[article.pk])
        ).content.decode()
        assert "<script>alert(1)</script>" not in content
        assert "</textarea><script" not in content

    def test_detail_page_sanitises_what_is_already_stored(self, client, staff):
        article = ZArticle.objects.create(
            title="Direct write",
            summary_html="x",
            body_html='<p>fine</p><img src=x onerror="alert(1)"><script>alert(1)</script>',
        )
        content = client.get(
            reverse("vantage:testapp-zarticle-detail", args=[article.pk])
        ).content.decode()
        assert '<div class="v-richtext"><p>fine</p></div>' in content
        assert "onerror" not in content
        assert "<script>alert(1)" not in content

    def test_markdown_fields_render_escaped_on_the_detail_page(self, client, staff, monkeypatch):
        monkeypatch.setattr(site.get_panel(ZArticle), "markdown_fields", ("notes_html",))
        article = ZArticle.objects.create(
            title="Md", summary_html="x", notes_html="<b>not html</b> **md**"
        )
        content = client.get(
            reverse("vantage:testapp-zarticle-detail", args=[article.pk])
        ).content.decode()
        assert "&lt;b&gt;not html&lt;/b&gt; **md**" in content

    def test_table_cells_show_text_not_markup(self):
        article = ZArticle(
            title="t", summary_html="x", body_html="<p>Hello <b>there</b></p><script>x</script>"
        )
        cell = render_cell(site.get_panel(ZArticle), article, "body_html")
        assert str(cell) == "Hello there"

    def test_rich_cell_is_escaped_html_free_of_script(self):
        article = ZArticle(title="t", summary_html="x", body_html="<p>a</p><script>x</script>")
        cell = render_cell(site.get_panel(ZArticle), article, "body_html", rich=True)
        assert str(cell) == '<div class="v-richtext"><p>a</p></div>'

    def test_a_text_that_only_looks_like_markup_is_escaped_in_a_cell(self):
        article = ZArticle(title="t", summary_html="x", body_html="1 < 2 & <b>3</b>")
        cell = render_cell(site.get_panel(ZArticle), article, "body_html")
        assert str(cell) == "1 < 2 & 3"  # plain text; the template escapes it on output


@pytest.mark.django_db
class TestApi:
    def test_the_type_is_still_richtext_in_the_schema(self, client, staff):
        response = client.get(reverse("vantage:api-schema"))
        assert response.status_code == 200
        text = response.content.decode()
        assert '"richtext"' in text

    def test_openapi_describes_it_as_a_string(self):
        from vantage.apiopenapi import _KIND_SCHEMAS

        assert _KIND_SCHEMAS["richtext"] == {"type": "string"}

    def test_api_writes_are_sanitised(self, client, staff):
        url = reverse("vantage:api-list", kwargs={"app_label": "testapp", "model_name": "zarticle"})
        response = client.post(
            url,
            data=json.dumps(
                {
                    "fields": {
                        "title": "Api",
                        "summary_html": "<p>s</p>",
                        "body_html": "<p>a</p><script>x</script>",
                    }
                }
            ),
            content_type="application/json",
        )
        assert response.status_code in (200, 201), response.content[:500]
        assert ZArticle.objects.get(title="Api").body_html == "<p>a</p>"

    def test_classification_is_unchanged(self):
        assert classify(ZArticle._meta.get_field("body_html")) is FieldKind.RICHTEXT


class TestScriptShipping:
    def test_the_editor_script_is_a_static_file_in_the_package(self):
        assert (STATIC / "vantage-richtext.js").is_file()

    def test_the_form_page_loads_it_as_an_external_script(self):
        template = (
            Path(__file__).resolve().parent.parent / "src/vantage/templates/vantage/form.html"
        ).read_text()
        assert "vantage/vantage-richtext.js" in template
        tag = re.search(r"<script[^>]*vantage-richtext\.js[^>]*>", template).group(0)
        assert "src=" in tag and "defer" in tag

    def test_the_script_needs_no_inline_handlers_or_eval(self):
        source = (STATIC / "vantage-richtext.js").read_text()
        code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        for forbidden in ("eval(", "new Function", 'setTimeout("', 'javascript:"', " onclick="):
            assert forbidden not in code
        # The one place it assigns HTML is the sanitised copy of the textarea's value.
        assigned = re.findall(r"\.innerHTML\s*=", code)
        assert len(assigned) == 2  # the inert <template> parse, and the cleaned surface

    @pytest.mark.django_db
    def test_the_form_page_adds_no_inline_script_for_the_editor(self, client, staff):
        content = client.get(reverse("vantage:testapp-zarticle-add")).content.decode()
        scripts = re.findall(r"<script\b([^>]*)>", content)
        richtext_tags = [s for s in scripts if "vantage-richtext.js" in s]
        assert len(richtext_tags) == 1
        # Every inline script on the page carries the nonce slot; the editor needs none.
        assert "nonce" not in richtext_tags[0] or "src=" in richtext_tags[0]

    def test_the_script_is_in_the_wheel_by_virtue_of_living_in_the_package(self):
        assert (
            (STATIC / "vantage-richtext.js")
            .resolve()
            .is_relative_to(Path(__file__).resolve().parent.parent / "src/vantage")
        )
