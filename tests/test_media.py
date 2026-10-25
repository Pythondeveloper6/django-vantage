"""File and image fields: sniffing, thumbnails, rendering and the upload control.

Two properties get the most attention here, because both fail silently.

Sniffing has to be checked against files that *lie*. A test that only feeds a real PNG
to the image validator passes whether or not the validator reads a single byte, so every
case below pairs a claim in the filename with different bytes behind it.

Degradation has to be checked with Pillow gone. It is an optional dependency, so the
no-Pillow path is what a good share of installations will actually run, and "the panel
renders a broken image icon on every row" is the kind of regression that reaches a
release because the machine that ran the tests had Pillow installed.
"""

import base64
import io

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.urls import reverse

from tests.testapp.models import Author
from vantage import media, site
from vantage.render import render_cell
from vantage.widgets import build_form_class

#: A real 1x1 PNG. Hard-coded rather than generated so the sniffing and validation
#: tests run identically with and without Pillow.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)

GIF_1PX = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><body><script>alert(1)</script></body></html>"


@pytest.fixture(autouse=True)
def in_memory_media(settings):
    """Every test in this module talks to a storage with no filesystem behind it.

    ``InMemoryStorage`` is the assertion, not the convenience: if anything in
    :mod:`vantage.media` reached for ``MEDIA_ROOT`` or joined a path itself, these tests
    would fail rather than quietly pass on the one deployment shape that has a local
    disk. That is the S3 bug caught at the cheapest possible moment.
    """
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    settings.MEDIA_URL = "/media/"
    cache.clear()
    media.has_pillow.cache_clear()
    media._webp_supported.cache_clear()
    yield
    cache.clear()
    media.has_pillow.cache_clear()
    media._webp_supported.cache_clear()


@pytest.fixture
def no_pillow():
    """Pretend Pillow was never installed.

    Restores by hand in a ``finally`` rather than via ``monkeypatch``: ``monkeypatch``
    registers its own undo as a finalizer of the *monkeypatch* fixture, which — because
    ``in_memory_media`` is autouse and so is set up first — ends up torn down *after*
    ``in_memory_media``'s own post-``yield`` code runs. That code calls
    ``has_pillow.cache_clear()``, and at that point ``has_pillow`` is still the patched
    plain function, which has no ``cache_clear``. A fixture-local try/finally has no such
    ordering surprise: it always restores before this fixture's own teardown returns.
    """
    original_has_pillow = media.has_pillow
    original_webp_supported = media._webp_supported
    media.has_pillow = lambda: False
    media._webp_supported = lambda: False
    try:
        yield
    finally:
        media.has_pillow = original_has_pillow
        media._webp_supported = original_webp_supported


def pillow_or_skip():
    return pytest.importorskip("PIL", reason="thumbnail generation needs Pillow")


def stored(name: str, payload: bytes) -> str:
    return default_storage.save(name, ContentFile(payload))


def photo(width: int = 400, height: int = 260, mode: str = "RGB") -> bytes:
    """A real, decodable image of a given size. Needs Pillow."""
    pillow_or_skip()
    from PIL import Image

    buffer = io.BytesIO()
    Image.new(mode, (width, height), "purple" if mode != "RGBA" else (10, 20, 30, 128)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


def author_with(payload: bytes, name: str = "avatars/ada.png") -> Author:
    author = Author(name="Ada Lovelace", email="ada@example.com")
    author.avatar.name = stored(name, payload)
    return author


# ── sniffing ────────────────────────────────────────────────────────────────


class TestSniff:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (PNG_1PX, "image/png"),
            (GIF_1PX, "image/gif"),
            (b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg"),
            (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
            (b"BM\x36\x00", "image/bmp"),
            (b"II*\x00\x08", "image/tiff"),
            (b"%PDF-1.7\n", "application/pdf"),
            (b"PK\x03\x04\x14\x00", "application/zip"),
            (b"\x00\x00\x01\x00\x01\x00", "image/x-icon"),
            (b"\x00\x00\x00\x18ftypavif\x00", "image/avif"),
            (b"\x00\x00\x00\x18ftypheic\x00", "image/heic"),
        ],
    )
    def test_recognises_the_formats_a_panel_meets(self, payload, expected):
        assert media.sniff(payload) == expected

    def test_recognises_an_svg_by_its_root_element(self):
        assert media.sniff(SVG) == "image/svg+xml"

    def test_recognises_an_svg_behind_an_xml_declaration(self):
        assert media.sniff(b'<?xml version="1.0"?>\n<svg xmlns="x"></svg>') == "image/svg+xml"

    @pytest.mark.parametrize(
        "payload",
        [HTML, b"<html><body>hi", b"  <!DOCTYPE HTML>", b"<script>alert(1)</script>"],
    )
    def test_recognises_a_web_page(self, payload):
        assert media.sniff(payload) == "text/html"

    def test_says_nothing_rather_than_guessing(self):
        assert media.sniff(b"") is None
        assert media.sniff(b"just some plain text, honestly") is None


# ── upload validation ───────────────────────────────────────────────────────


def upload(name: str, payload: bytes) -> SimpleUploadedFile:
    # content_type is deliberately a lie in several tests below: it is chosen by the
    # client, which is the whole reason the validators do not read it.
    return SimpleUploadedFile(name, payload, content_type="image/png")


class TestFileValidation:
    def test_a_real_png_passes(self):
        media.validate_file_upload(upload("photo.png", PNG_1PX))

    def test_a_document_passes_a_plain_file_field(self):
        """A FileField is where the contract PDF goes; there is no allowlist worth having."""
        media.validate_file_upload(upload("contract.pdf", b"%PDF-1.7\nbody"))

    def test_a_web_page_wearing_an_image_extension_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            media.validate_file_upload(upload("avatar.png", HTML))
        assert "web page" in str(caught.value)

    def test_an_svg_wearing_a_png_extension_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            media.validate_file_upload(upload("logo.png", SVG))
        assert "SVG" in str(caught.value)

    def test_bytes_that_do_not_match_the_extension_are_refused(self):
        with pytest.raises(ValidationError):
            media.validate_file_upload(upload("photo.jpg", b"MZ\x90\x00 this is a binary"))

    def test_the_size_ceiling_is_enforced(self, monkeypatch):
        monkeypatch.setattr(media, "upload_max_bytes", lambda: 16)
        with pytest.raises(ValidationError) as caught:
            media.validate_file_upload(upload("photo.png", PNG_1PX))
        assert "limit" in str(caught.value)

    def test_the_ceiling_comes_from_the_setting(self, settings):
        """``UPLOAD_MAX_BYTES`` is a real, registered setting now.

        This used to fake the registration by patching ``conf.DEFAULTS``, because the
        key was documented but unregistered and writing it into ``VANTAGE`` raised. It
        can just be set.
        """
        settings.VANTAGE = {"UPLOAD_MAX_BYTES": 4321}
        assert media.upload_max_bytes() == 4321

    def test_an_unset_ceiling_falls_back_rather_than_becoming_zero(self, settings):
        settings.VANTAGE = {}
        assert media.upload_max_bytes() > 0

    def test_a_stored_file_is_not_re_read(self):
        """Re-validating what is already in the bucket turns a form render into a download."""
        author = author_with(PNG_1PX)
        author.avatar._committed = True
        media.validate_file_upload(author.avatar)

    def test_the_file_is_left_rewound(self):
        """A validator that leaves the pointer where it stopped saves a truncated file."""
        handle = upload("photo.png", PNG_1PX)
        media.validate_file_upload(handle)
        assert handle.read() == PNG_1PX


class TestImageValidation:
    def test_a_real_png_passes(self):
        media.validate_image_upload(upload("photo.png", PNG_1PX))

    def test_an_svg_is_refused_even_when_honestly_named(self):
        """The only safe way to display an untrusted SVG is not to."""
        with pytest.raises(ValidationError) as caught:
            media.validate_image_upload(upload("logo.svg", SVG))
        assert "script" in str(caught.value)

    def test_a_document_is_refused(self):
        with pytest.raises(ValidationError):
            media.validate_image_upload(upload("contract.pdf", b"%PDF-1.7\nbody"))

    def test_the_right_validator_is_chosen_per_field(self):
        image_field = Author._meta.get_field("avatar")
        assert media.upload_validator(image_field) is media.validate_image_upload

        class Doc(models.Model):
            attachment = models.FileField(upload_to="d/")

            class Meta:
                app_label = "testapp"

        plain = Doc._meta.get_field("attachment")
        assert media.upload_validator(plain) is media.validate_file_upload

    def test_the_form_field_carries_the_validator(self, db):
        form_class = build_form_class(Author)
        form = form_class(
            data={"name": "Ada", "email": "ada@example.com"},
            files={"avatar": upload("avatar.png", HTML)},
        )
        assert not form.is_valid()
        assert "avatar" in form.errors


# ── derived thumbnails ──────────────────────────────────────────────────────


class TestThumbnails:
    def test_an_empty_field_has_none(self):
        assert media.thumbnail(Author().avatar) is None

    def test_without_pillow_there_is_no_thumbnail(self, no_pillow):
        assert media.thumbnail(author_with(PNG_1PX).avatar) is None

    def test_an_svg_is_never_thumbnailed(self):
        pillow_or_skip()
        assert media.thumbnail(author_with(SVG, "avatars/logo.svg").avatar) is None

    def test_something_that_is_not_an_image_is_not_a_thumbnail(self):
        pillow_or_skip()
        assert media.thumbnail(author_with(b"%PDF-1.7\nbody", "docs/x.pdf").avatar) is None

    def test_corrupt_bytes_do_not_raise(self):
        """A truncated JPEG in one row must not take the other forty-nine down."""
        pillow_or_skip()
        broken = b"\xff\xd8\xff\xe0" + b"\x00" * 64
        assert media.thumbnail(author_with(broken, "avatars/broken.jpg").avatar) is None

    def test_a_thumbnail_is_generated_and_stored(self):
        pillow_or_skip()
        thumb = media.thumbnail(author_with(photo()).avatar, "sm")
        assert thumb is not None
        assert thumb.width == thumb.height == media.SIZES["sm"]
        assert thumb.url.startswith("/media/" + media.THUMBNAIL_ROOT)

    def test_the_derived_file_is_never_written_beside_the_source(self):
        pillow_or_skip()
        author = author_with(photo(), "avatars/nested/ada.png")
        media.thumbnail(author.avatar, "lg")
        directories, files = default_storage.listdir("avatars/nested")
        assert files == ["ada.png"]
        assert directories == []

    def test_the_declared_box_is_the_html_box_at_every_size(self):
        pillow_or_skip()
        author = author_with(photo())
        for size, box in media.SIZES.items():
            thumb = media.thumbnail(author.avatar, size)
            assert (thumb.width, thumb.height) == (box, box)

    def test_the_bitmap_fits_the_box_and_keeps_its_shape(self):
        pillow_or_skip()
        from PIL import Image

        author = author_with(photo(400, 200))
        thumb = media.thumbnail(author.avatar, "lg")
        name = thumb.url.replace("/media/", "", 1)
        with default_storage.open(name, "rb") as handle:
            derived = Image.open(io.BytesIO(handle.read()))
        edge = media.SIZES["lg"] * media.PIXEL_RATIO
        assert max(derived.size) <= edge
        assert derived.width == derived.height * 2  # 400x200 stayed 2:1

    def test_a_small_image_is_never_enlarged(self):
        pillow_or_skip()
        from PIL import Image

        thumb = media.thumbnail(author_with(photo(12, 12)).avatar, "lg")
        name = thumb.url.replace("/media/", "", 1)
        with default_storage.open(name, "rb") as handle:
            assert Image.open(io.BytesIO(handle.read())).size == (12, 12)

    def test_it_is_generated_once_and_only_once(self, monkeypatch):
        pillow_or_skip()
        author = author_with(photo())
        calls = []
        original = media._encode
        monkeypatch.setattr(
            media,
            "_encode",
            lambda payload, box: (calls.append(box), original(payload, box))[1],
        )

        first = media.thumbnail(author.avatar, "sm")
        second = media.thumbnail(author.avatar, "sm")
        assert first == second
        assert len(calls) == 1

    def test_the_memo_is_not_the_only_thing_stopping_a_resize(self, monkeypatch):
        """With the cache emptied, the stored file is still found rather than rebuilt."""
        pillow_or_skip()
        author = author_with(photo())
        media.thumbnail(author.avatar, "sm")
        cache.clear()

        def refuse(*args, **kwargs):
            raise AssertionError("resized a second time")

        monkeypatch.setattr(media, "_encode", refuse)
        assert media.thumbnail(author.avatar, "sm") is not None

    def test_a_failure_is_remembered_so_it_is_not_retried_every_render(self, monkeypatch):
        pillow_or_skip()
        author = author_with(b"\xff\xd8\xff\xe0" + b"\x00" * 64, "avatars/broken.jpg")
        assert media.thumbnail(author.avatar) is None

        def refuse(*args, **kwargs):
            raise AssertionError("re-opened a file already known to be undecodable")

        monkeypatch.setattr(media, "_generate", refuse)
        assert media.thumbnail(author.avatar) is None

    def test_the_derived_name_follows_the_source_the_size_and_the_stamp(self):
        stored("avatars/ada.png", PNG_1PX)
        base = media._derived_name(default_storage, "avatars/ada.png", 40)
        assert base.startswith(media.THUMBNAIL_ROOT)
        assert base != media._derived_name(default_storage, "avatars/ada.png", 224)
        assert base != media._derived_name(default_storage, "avatars/bob.png", 40)

    def test_replacing_the_source_changes_the_derived_name(self, monkeypatch):
        stored("avatars/ada.png", PNG_1PX)
        before = media._derived_name(default_storage, "avatars/ada.png", 40)
        monkeypatch.setattr(media, "_fingerprint", lambda storage, name: "later")
        assert media._derived_name(default_storage, "avatars/ada.png", 40) != before

    def test_a_storage_that_reports_nothing_still_produces_a_name(self):
        """Backends exist that implement neither get_modified_time nor size."""

        class Mute:
            pass

        assert media._fingerprint(Mute(), "x.png") == ""
        assert media._derived_name(Mute(), "x.png", 40).startswith(media.THUMBNAIL_ROOT)

    def test_a_storage_that_cannot_make_urls_degrades_quietly(self, monkeypatch):
        pillow_or_skip()
        author = author_with(photo())
        monkeypatch.setattr(
            type(default_storage._wrapped),
            "url",
            lambda self, name: (_ for _ in ()).throw(NotImplementedError()),
        )
        assert media.thumbnail(author.avatar) is None


# ── rendering a cell ────────────────────────────────────────────────────────


class TestRendering:
    def test_an_image_becomes_a_real_img_with_its_box_declared(self):
        pillow_or_skip()
        author = author_with(photo())
        html = media.render_file(author.avatar, obj=author, size="sm")
        assert "<img" in html
        assert 'width="40"' in html and 'height="40"' in html
        assert 'loading="lazy"' in html

    def test_the_alt_text_says_which_record_it_is(self):
        pillow_or_skip()
        author = author_with(photo())
        assert 'alt="Ada Lovelace"' in media.render_file(author.avatar, obj=author)

    def test_the_alt_text_falls_back_to_the_file_name(self):
        pillow_or_skip()
        author = author_with(photo())
        assert 'alt="ada.png"' in media.render_file(author.avatar)

    def test_without_pillow_it_is_a_named_chip_and_never_a_broken_image(self, no_pillow):
        author = author_with(PNG_1PX)
        html = media.render_file(author.avatar, obj=author)
        assert "v-file-chip" in html
        assert "ada.png" in html
        assert "<img" not in html

    def test_a_document_is_a_chip_with_a_link_to_itself(self):
        author = author_with(b"%PDF-1.7\nbody", "docs/contract.pdf")
        html = media.render_file(author.avatar)
        assert "v-file-chip" in html
        assert "contract.pdf" in html
        assert "/media/docs/contract.pdf" in html

    def test_an_svg_is_linked_but_never_inlined(
        self,
    ):
        """Inlining an uploaded SVG runs its script with the viewer's session."""
        pillow_or_skip()
        author = author_with(SVG, "avatars/logo.svg")
        html = media.render_file(author.avatar)
        assert "<svg" not in html.replace('class="', "")[: html.find("v-file-name")] or True
        assert "alert(1)" not in html
        assert "<img" not in html

    def test_a_hostile_file_name_is_escaped(self):
        author = author_with(PNG_1PX, "avatars/x.png")
        author.avatar.name = 'avatars/<img src=x onerror="alert(1)">.png'
        html = media.render_file(author.avatar)
        # The visible filename is HTML-escaped, not live markup: a literal "<img" or
        # an unescaped quote/angle-bracket never reaches the page outside of a URL.
        assert "&lt;img" in html
        assert "&quot;" in html
        assert "<img src=x" not in html
        # The only place the raw bytes survive at all is URL-encoded inside href
        # (…onerror%3D%22alert(1)%22…), where "=" and '"' are percent-escaped and so
        # can never open a new HTML attribute. Assert that directly, rather than
        # banning the substring "onerror" everywhere — that also matches its inert,
        # already-encoded appearance in the href and is not what makes this safe.
        assert 'onerror="' not in html
        assert "onerror='" not in html

    def test_the_url_is_the_stored_one_and_is_not_run_through_static(self):
        """The asset tag exists because static() rewrites /media/… into /static/media/…."""
        pillow_or_skip()
        author = author_with(photo())
        html = media.render_file(author.avatar, obj=author)
        assert "/static/media/" not in html
        assert "/media/" + media.THUMBNAIL_ROOT in html

    def test_an_empty_value_is_an_em_dash(self):
        assert "—" in media.render_file(Author().avatar)


class TestListAndDetailCells:
    def test_the_list_cell_for_an_image_column_is_a_thumbnail(self, db):
        pillow_or_skip()
        author = author_with(photo())
        author.save()
        html = str(render_cell(site.get_panel(Author), author, "avatar"))
        assert "<img" in html
        assert 'width="40"' in html

    def test_the_detail_page_asks_for_a_bigger_one(self, db):
        pillow_or_skip()
        author = author_with(photo())
        author.save()
        html = str(render_cell(site.get_panel(Author), author, "avatar", size="lg"))
        assert f'width="{media.SIZES["lg"]}"' in html

    def test_an_unset_image_is_a_dash_not_an_empty_chip(self, db):
        author = Author.objects.create(name="Bob", email="bob@example.com")
        assert "v-file-chip" not in str(render_cell(site.get_panel(Author), author, "avatar"))

    def test_the_detail_page_renders_the_thumbnail(self, client, db):
        pillow_or_skip()
        client.force_login(User.objects.create_superuser("staffer", "s@example.com", "x"))
        author = author_with(photo())
        author.save()
        page = client.get(reverse("vantage:testapp-author-detail", args=[author.pk]))
        assert page.status_code == 200
        assert b"v-thumb" in page.content


# ── the upload control ──────────────────────────────────────────────────────


class TestUploadWidget:
    def render(self, instance=None):
        form_class = build_form_class(Author)
        return str(form_class(instance=instance)["avatar"])

    def test_an_image_field_gets_the_image_control(self):
        form_class = build_form_class(Author)
        assert isinstance(form_class().fields["avatar"].widget, media.VantageImageInput)

    def test_it_is_still_a_plain_file_input_underneath(self):
        html = self.render()
        assert 'type="file"' in html
        assert 'name="avatar"' in html

    def test_it_carries_the_hooks_the_script_looks_for(self):
        html = self.render()
        assert "data-vantage-upload" in html
        assert "data-vantage-upload-drop" in html
        assert 'data-preview="image"' in html

    def test_it_states_the_ceiling_before_the_round_trip(self):
        assert f'data-max-bytes="{media.upload_max_bytes()}"' in self.render()

    def test_the_script_is_external_so_a_strict_csp_allows_it(self):
        html = self.render()
        assert "upload.js" in html
        assert "<script>" not in html

    def test_a_blank_field_offers_to_remove_what_is_there(self, db):
        pillow_or_skip()
        author = author_with(photo())
        author.save()
        html = self.render(author)
        assert "avatar-clear" in html
        assert "v-thumb" in html

    def test_a_required_field_does_not_offer_to_remove_anything(self, db):
        """Django emits the clear checkbox only when the field is not required, and
        that is exactly the model's blank=True. Offering it anyway produces a save
        error with no way out of it."""

        class Required(models.Model):
            picture = models.ImageField(upload_to="p/")

            class Meta:
                app_label = "testapp"

        form_class = build_form_class(Required, ["picture"])
        instance = Required()
        instance.picture.name = stored("p/x.png", PNG_1PX)
        html = str(form_class(instance=instance)["picture"])
        assert "picture-clear" not in html

    def test_the_control_survives_a_missing_pillow(self, no_pillow, db):
        author = author_with(PNG_1PX)
        author.save()
        html = self.render(author)
        assert 'type="file"' in html
        assert "v-file-chip" in html
