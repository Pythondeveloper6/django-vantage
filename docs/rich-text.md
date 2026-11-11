# Rich-text fields

A rich-text field gets a small WYSIWYG editor instead of a plain textarea. The value is
stored as HTML, and that HTML is sanitised twice: when it is saved, and again every time
it is drawn.

## Which fields are rich text

Vantage treats a `TextField` as rich text when its field class name suggests it (a name
containing `richtext`, `wysiwyg`, `html` and similar) or its name ends in `_html`. Every
other `TextField` stays a plain textarea. In the JSON API and the OpenAPI document the
type is still `"richtext"` and the value is still a string.

```python
from django.db import models


class Article(models.Model):
    title = models.CharField(max_length=120)
    body_html = models.TextField()      # rich text: edited in the WYSIWYG editor
    notes = models.TextField(blank=True)  # plain textarea
```

## The editor

The editor is plain JavaScript over `contenteditable`, shipped as one static file
(`vantage/vantage-richtext.js`) inside the package. There is nothing to build and no
Node.js step. It is loaded as an external script, so it works under a
Content-Security-Policy that does not allow inline scripts.

It offers bold, italic, inline code, links, a heading, bulleted and numbered lists and a
block quote. Pasting inserts plain text. Without JavaScript the field falls back to a
textarea that holds the HTML, and the form still saves.

## What is allowed

The sanitiser uses an allowlist (it is built on the `nh3` package). Anything not named
below is removed.

| Part | Allowed |
| --- | --- |
| Tags | `p br strong b em i u s code pre blockquote ul ol li a h1`-`h6` |
| Attributes | `href` and `title` on `a`; `rel` is always set to `noopener noreferrer nofollow` |
| URL schemes | `http`, `https`, `mailto`, `tel`, and relative URLs |

Everything else is dropped: `script` and `style` elements (with their contents),
`on*` event handlers, `style` and `class` attributes, `javascript:`, `data:` and
`vbscript:` URLs, comments, forms, frames, `svg` and `math`.

Where it runs:

- **On save.** The form field cleans the submitted value, so the web forms, bulk edit,
  the CSV import and the JSON API all store sanitised HTML, whatever form-field class a
  panel's `formfield_overrides` puts on the field. (Inline cells do not edit rich text at
  all.) An editor that is emptied out
  (or whose content is entirely removed) counts as empty, so a required field still
  complains.
- **On render.** The detail page cleans the stored value before showing it, so a row
  written by a script, a migration or another application cannot put markup on the page.
  The edit form also cleans the value before it reaches the editor. Table cells show the
  visible text only, truncated.

## Widening the policy

Add to the allowlist from `settings.py`:

```python
VANTAGE = {
    # Extra tags.
    "RICHTEXT_EXTRA_TAGS": ["table", "thead", "tbody", "tr", "th", "td"],
    # Extra attributes, per tag.
    "RICHTEXT_EXTRA_ATTRIBUTES": {"td": ["colspan", "rowspan"], "th": ["colspan"]},
    # Extra URL schemes.
    "RICHTEXT_URL_SCHEMES": ["sms"],
}
```

These settings can only add. Active content cannot be switched on: `script`, `style`,
`iframe`, `object`, `embed`, `form` controls, `svg`, `math`, `template`, `noscript` and the
legacy raw-text tags (`xmp`, `noembed`, `noframes`, `plaintext`, `listing`, `marquee`);
`style`, `class`, `id`, `name`, `srcdoc`, every `on*` attribute, URL-valued attributes that
are not scheme-checked (`srcset`, `background`, `poster`, `ping`, `action`, `formaction`,
`data`, `xlink:href`) and focus or UI attributes (`autofocus`, `contenteditable`,
`tabindex`, `accesskey`, `draggable`, `is`, `slot`), including through the `*` key; and the `javascript`, `data`,
`vbscript`, `blob` and `file` schemes. Asking for one raises `ImproperlyConfigured` the
first time settings are read. The editor itself only produces the default tags, so extra
tags are for HTML you load from elsewhere and want to keep.

## Keeping the markdown toolbar

Some fields already hold markdown, not HTML. Two ways to keep the older plain textarea
with a markdown toolbar for them:

```python
from vantage.panels import ModelPanel


class ArticlePanel(ModelPanel):
    # These fields are stored and shown exactly as typed. They are not sanitised as
    # HTML, and they are escaped, never interpreted, when displayed.
    markdown_fields = ("body_html",)
```

Or for every rich-text field in the project:

```python
VANTAGE = {"RICHTEXT_EDITOR": "markdown"}   # default: "html"
```

Existing content in an HTML field is shown as it is, after sanitising. Content that is
plain text or markdown opens in the HTML editor as literal text, so use `markdown_fields`
for fields that hold markdown.
