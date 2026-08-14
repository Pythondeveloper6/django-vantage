"""File and image fields, rendered as things you can see.

An ``ImageField`` that renders as ``products/2024/blue-widget.jpg`` is a database column
printed on a screen, not an admin panel. This module is the whole of the file story:
the thumbnail a list cell and a detail page show, the derived-image cache behind it, the
upload control on the form, and the checks that stop a hostile upload becoming a stored
XSS. Four rules shape all of it.

**Pillow is optional and stays optional.** It is soft-detected with ``find_spec``, the
same way :mod:`vantage.health` probes optional packages and :mod:`vantage.permissions`
reaches for guardian — an import guarded at the call site, never at module scope. With
Pillow absent every file degrades to a chip carrying an icon and the file's name. A
panel must never show a broken image and must never raise because a resize failed:
:func:`thumbnail` swallows and logs, because a thumbnail is decoration and the row it
sits in is data.

**Storage is the only filesystem.** Everything here goes through ``Storage.open``,
``.save``, ``.url``, ``.exists`` and ``.get_modified_time``. Nothing reads ``MEDIA_ROOT``
or joins a path, because doing so works on one developer's laptop and on no S3, GCS or
Azure deployment in existence.

**Derived images are generated once and never in the source directory.** The derived
name is a digest of the source path, the box size, the pixel ratio and the source's
last-modified stamp, written under :data:`THUMBNAIL_ROOT`. A changed source produces a
different digest, so nothing has to be invalidated by hand, and a shared prefix means
"delete the thumbnails" is one recursive delete that cannot take an original with it.

**Uploads are hostile until sniffed.** The extension is a claim made by whoever chose
the filename. :func:`sniff` reads the magic bytes instead, and the validators reject a
file whose bytes disagree with its name, a web page wearing an image extension, and
anything over the ceiling. SVG is stored but never inlined and never thumbnailed — an
inline ``<svg>`` from an upload executes script with the viewer's session; see
:func:`validate_branding_upload` in :mod:`vantage.models` for the same reasoning applied
to branding, which is the one place an SVG is accepted at all.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils.safestring import SafeString
from django.utils.text import Truncator
from django.utils.translation import gettext_lazy as _

__all__ = [
    "MAX_SOURCE_BYTES",
    "MAX_SOURCE_PIXELS",
    "SIZES",
    "THUMBNAIL_ROOT",
    "Thumbnail",
    "VantageFileInput",
    "VantageImageInput",
    "file_widget",
    "has_pillow",
    "render_file",
    "sniff",
    "thumbnail",
    "upload_max_bytes",
    "upload_validator",
    "validate_file_upload",
    "validate_image_upload",
]

logger = logging.getLogger("vantage.media")


# ── configuration ───────────────────────────────────────────────────────────

#: Thumbnail box sizes in CSS pixels, by role. ``sm`` is a list cell, ``md`` the preview
#: beside an upload control, ``lg`` the detail page. Three sizes, not a free-form number:
#: every distinct size is a separate derived file on the storage bill, and a panel with
#: an arbitrary size per template ends up storing a dozen near-identical images.
SIZES: dict[str, int] = {"sm": 40, "md": 96, "lg": 224}

#: Derived bitmaps are generated at twice the CSS box so they stay sharp on the laptop
#: displays every one of these panels is actually read on. The ``<img>`` still declares
#: the CSS box in its ``width``/``height``, which is what stops the page reflowing.
PIXEL_RATIO = 2

#: Where derived images live. A fixed prefix, never beside the source: ``upload_to`` is
#: the project's directory and writing into it means a thumbnail can collide with a real
#: upload, get served as one, or be swept up by whatever the project runs over its own
#: media. The trailing slash is deliberate — storage names are POSIX-ish strings.
THUMBNAIL_ROOT = "vantage/thumbnails/"

#: How long a resolved thumbnail URL is remembered in the cache framework.
#:
#: The memo exists to keep a fifty-row page from making a hundred storage round trips —
#: on S3 an ``exists()`` is a HEAD request, and a list page that makes fifty of them is
#: slower than the query that produced the rows. The trade: a source file *replaced
#: under the same name* keeps its old thumbnail until this expires. Django's own
#: ``upload_to`` uniquifies names on collision so that is already the uncommon case, and
#: the derived name embeds the source's modification time, so the new thumbnail appears
#: on its own once the memo lapses. No hand-written invalidation, nothing to forget.
CACHE_SECONDS = 300

#: The most of a source image this module will pull out of storage to resize. Separate
#: from the upload ceiling, which only governs what arrives *now*: a bucket populated
#: before this package existed can hold anything, and a 400 MB TIFF must degrade to a
#: chip rather than pull 400 MB into a web worker's memory.
MAX_SOURCE_BYTES = 25 * 1024 * 1024

#: Refuse to decode an image whose declared dimensions exceed this. A 64 KB PNG can
#: declare 60,000 by 60,000 pixels and cost 14 GB to decode — the decompression bomb.
#: The header is read before any pixels are, so this costs nothing.
MAX_SOURCE_PIXELS = 50_000_000

#: How many bytes of a file :func:`sniff` looks at. Every signature below is decided in
#: the first sixteen; the rest is slack for the textual formats.
HEAD_BYTES = 1024

#: The default per-file upload ceiling, used only until ``UPLOAD_MAX_BYTES`` is a
#: registered key of :data:`vantage.conf.DEFAULTS`. The number itself belongs to
#: :mod:`vantage.security`, which declares the same key for the request-level check;
#: :func:`upload_max_bytes` prefers that declaration so the two can never disagree.
FALLBACK_UPLOAD_MAX_BYTES = 10 * 1024 * 1024


def upload_max_bytes() -> int:
    """The per-file upload ceiling, whether or not the settings layer knows the key yet.

    Mirrors ``vantage.security._setting``: ``vantage.conf.DEFAULTS`` rejects unknown
    ``VANTAGE`` keys on purpose, so a module that reads a not-yet-registered setting has
    to fall back rather than make the key unusable. This starts working the day
    ``UPLOAD_MAX_BYTES`` is registered, with no change here.
    """
    from vantage.conf import DEFAULTS as CONF_DEFAULTS

    if "UPLOAD_MAX_BYTES" in CONF_DEFAULTS:
        from vantage.conf import vantage_settings

        return int(vantage_settings.UPLOAD_MAX_BYTES)

    try:
        from vantage import security

        declared = getattr(security, "DEFAULTS", {}).get("UPLOAD_MAX_BYTES")
    except Exception:  # pragma: no cover - security.py is optional to this module
        declared = None
    return int(declared or FALLBACK_UPLOAD_MAX_BYTES)


# ── soft detection ──────────────────────────────────────────────────────────


@functools.cache
def has_pillow() -> bool:
    """Whether Pillow is importable.

    ``find_spec`` rather than a real import, which is how :mod:`vantage.health` probes
    every other optional package: asking the question must not have the side effect of
    answering it, and importing Pillow costs tens of milliseconds and a few megabytes in
    a process that may never render an image.

    Note for anyone debugging a missing thumbnail: Django's own system check ``E210``
    already refuses to start with an ``ImageField`` and no Pillow, so the degraded path
    here is mostly for ``FileField`` columns that happen to hold images.
    """
    from importlib.util import find_spec

    try:
        return find_spec("PIL") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken sys.path only
        return False


@functools.cache
def _webp_supported() -> bool:
    """Whether this Pillow build can encode WebP.

    Every wheel on PyPI can, but a distribution package built against a stripped
    libwebp cannot, and finding that out from a traceback at render time is not
    acceptable. The answer decides the derived file's extension, so it has to be
    knowable *before* the source is opened — otherwise the cache name depends on the
    image and ``exists()`` can never be checked.
    """
    if not has_pillow():
        return False
    try:
        from PIL import features

        return bool(features.check("webp"))
    except Exception:  # pragma: no cover - a Pillow too old to have `features`
        return False


# ── content sniffing ────────────────────────────────────────────────────────

#: Magic-byte prefixes, longest-first where two overlap. Deliberately a tuple of pairs
#: rather than a dict: order is part of the meaning.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
)

#: ISO base-media brands that mean "a still image", read from bytes 8-12 of an
#: ``ftyp`` box. Phones produce these by default now, so they are not exotic.
_ISO_IMAGE_BRANDS = frozenset({b"avif", b"avis", b"heic", b"heix", b"heim", b"hevc", b"mif1"})

#: Openings that mean a browser would treat the file as a document. ``nosniff`` is set
#: on panel responses, but media is very often served by a CDN that is not the panel.
_HTML_OPENINGS = (b"<!doctype html", b"<html", b"<head", b"<body", b"<script", b"<svg")

#: What :func:`thumbnail` will attempt to decode. SVG is absent on purpose: Pillow
#: cannot read it, and rasterising untrusted XML is a job for a sandbox, not a panel.
RASTER_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "image/avif",
        "image/heic",
        "image/x-icon",
    }
)

#: Extensions that *claim* to be an image. A claim is all they are; the point of
#: :func:`validate_image_upload` is to check it against the bytes.
IMAGE_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".jpe",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".avif",
        ".heic",
        ".heif",
        ".ico",
        ".svg",
    }
)

#: Extension -> the icon a chip shows when there is no thumbnail. Anything unlisted
#: gets ``file-text``, which reads as "a document" rather than as a wrong guess.
_CHIP_ICONS: dict[str, str] = {
    ".zip": "archive",
    ".gz": "archive",
    ".tar": "archive",
    ".tgz": "archive",
    ".rar": "archive",
    ".7z": "archive",
    ".csv": "file-text",
    ".pdf": "file-text",
}


def sniff(head: bytes) -> str | None:
    """The content type of a file from its leading bytes, or ``None`` if unrecognised.

    This is the only opinion about a file's type that is worth having. ``Content-Type``
    on an upload is chosen by the client, and the extension is chosen by whoever named
    the file; both are attacker-controlled in exactly the case that matters.
    """
    if not head:
        return None

    for prefix, content_type in _SIGNATURES:
        if head.startswith(prefix):
            return content_type

    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12].lower() in _ISO_IMAGE_BRANDS:
        return "image/heic" if head[8:12].lower().startswith(b"he") else "image/avif"

    stripped = head.lstrip()[:512].lower()
    if stripped.startswith(b"<?xml") and b"<svg" in head[:HEAD_BYTES].lower():
        return "image/svg+xml"
    if stripped.startswith(b"<svg"):
        return "image/svg+xml"
    for opening in _HTML_OPENINGS:
        if stripped.startswith(opening):
            return "text/html"
    return None


def _suffix(name: str) -> str:
    return PurePosixPath(str(name or "")).suffix.lower()


def _basename(name: str) -> str:
    return PurePosixPath(str(name or "")).name


def _read_head(value: Any) -> bytes:
    """The first :data:`HEAD_BYTES` of an uploaded file, leaving it rewound.

    Rewinding in a ``finally`` is not defensive style for its own sake: a validator that
    leaves the pointer where it stopped hands a truncated file to the storage backend,
    and the upload silently saves as a fragment.
    """
    try:
        value.seek(0)
        head = value.read(HEAD_BYTES)
    except Exception:  # pragma: no cover - an exotic file-like object
        return b""
    finally:
        try:
            value.seek(0)
        except Exception:  # pragma: no cover
            pass
    if isinstance(head, str):  # pragma: no cover - only a text-mode file object
        head = head.encode("utf-8", "replace")
    return head


# ── upload validation ───────────────────────────────────────────────────────


def _check_size(value: Any) -> None:
    limit = upload_max_bytes()
    size = getattr(value, "size", None) or 0
    if limit and size > limit:
        raise ValidationError(
            _("That file is %(size)s MB. The limit is %(limit)s MB.")
            % {"size": f"{size / 1048576:.1f}", "limit": f"{limit / 1048576:.0f}"},
            code="upload_too_large",
        )


def validate_file_upload(value: Any) -> None:
    """What every uploaded file must satisfy, whatever the field.

    Two rules, and only two, because a generic ``FileField`` is where a project puts the
    contract PDF and the firmware blob and there is no allowlist that would not be wrong
    for someone. The ceiling stops a disk being filled; the disagreement check stops the
    classic content-sniffing attack, where ``avatar.png`` is a web page and a browser
    that guesses renders it as one on the media origin.

    An already-stored file is skipped: it was checked when it arrived, and re-reading it
    from storage on every ``full_clean`` turns rendering a form into a download.
    """
    if getattr(value, "_committed", False):
        return

    _check_size(value)

    name = getattr(value, "name", "") or ""
    suffix = _suffix(name)
    detected = sniff(_read_head(value))

    if detected == "text/html" and suffix not in (".html", ".htm", ".xhtml"):
        raise ValidationError(
            _(
                "%(name)s is a web page, not a %(suffix)s file. A page stored under an "
                "innocent extension is how a file upload becomes a scripting hole."
            )
            % {"name": _basename(name), "suffix": suffix or _("data")},
            code="upload_disguised",
        )

    if detected == "image/svg+xml" and suffix != ".svg":
        raise ValidationError(
            _("%(name)s is an SVG wearing a %(suffix)s extension. Rename it or export a PNG.")
            % {"name": _basename(name), "suffix": suffix},
            code="upload_disguised",
        )

    if suffix in IMAGE_SUFFIXES and suffix != ".svg" and detected not in RASTER_TYPES:
        raise ValidationError(
            _("%(name)s is not really an image. Its contents do not match its extension.")
            % {"name": _basename(name)},
            code="upload_not_an_image",
        )


def validate_image_upload(value: Any) -> None:
    """Everything :func:`validate_file_upload` requires, and the bytes must be an image.

    Stricter than the generic case because an ``ImageField`` has already told us what it
    holds. SVG is refused here rather than merely left un-thumbnailed: a field the panel
    will render as a picture must contain something the panel can safely render, and the
    only safe way to show an untrusted SVG is not to.
    """
    if getattr(value, "_committed", False):
        return

    validate_file_upload(value)

    detected = sniff(_read_head(value))
    if detected in RASTER_TYPES:
        return

    if detected == "image/svg+xml":
        raise ValidationError(
            _(
                "SVG cannot be used here. An SVG is a document that can carry script, "
                "so the panel will not display an uploaded one. Export a PNG or WebP."
            ),
            code="upload_svg",
        )
    raise ValidationError(
        _("%(name)s is not an image the panel can read. Use PNG, JPEG, WebP or GIF.")
        % {"name": _basename(getattr(value, "name", "")) or _("That file")},
        code="upload_not_an_image",
    )


def upload_validator(db_field: Any):
    """The validator appropriate to one model field. See :mod:`vantage.widgets`."""
    from django.db import models

    if isinstance(db_field, models.ImageField):
        return validate_image_upload
    return validate_file_upload


# ── derived images ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Thumbnail:
    """A derived image, ready to be an ``<img>``.

    ``width`` and ``height`` are the *CSS box*, not the bitmap: the bitmap is
    :data:`PIXEL_RATIO` times larger and is fitted inside the box by ``object-fit``. The
    box is what goes in the HTML attributes, because reserving the right space before
    the bytes arrive is the entire point of putting them there.
    """

    url: str
    width: int
    height: int


_MISS = object()


def _cache():
    from django.core.cache import cache

    return cache


def _storage_of(file: Any):
    return getattr(file, "storage", None) or default_storage


def _fingerprint(storage: Any, name: str) -> str:
    """A stamp that changes when the source does.

    Modification time first, because that is what "the file changed" means and every
    backend worth using reports it. Size is the fallback for the ones that do not, and
    is better than nothing: it catches most replacements. When neither works the digest
    falls back to the path alone, which is still correct for the overwhelmingly common
    case of a name that is never reused.
    """
    for probe in ("get_modified_time", "size"):
        getter = getattr(storage, probe, None)
        if getter is None:
            continue
        try:
            return str(getter(name))
        except Exception:
            continue
    return ""


def _derived_name(storage: Any, name: str, box: int) -> str:
    """Where the thumbnail of ``name`` at ``box`` lives.

    Keyed on the source path, the box, the pixel ratio and the source's stamp, so a
    replaced source resolves to a different file rather than to a stale one, and so two
    sources that share a basename in different directories cannot collide. Sharded on
    the first two hex characters because a flat directory of a million small files is a
    problem on every local filesystem and on none of the object stores.
    """
    stamp = _fingerprint(storage, name)
    material = f"{name}|{box}|{PIXEL_RATIO}|{stamp}".encode()
    digest = hashlib.sha256(material).hexdigest()[:32]
    extension = "webp" if _webp_supported() else "png"
    return f"{THUMBNAIL_ROOT}{box}/{digest[:2]}/{digest[2:]}.{extension}"


def _encode(payload: bytes, box: int) -> bytes | None:
    """Resize ``payload`` into the box, or ``None`` if it is not an image we will decode.

    Aspect ratio is preserved and the image is never enlarged — an upscaled 16-pixel
    icon is worse than a small one. The result is *contained* rather than cropped, which
    is why the template pairs it with ``object-fit: contain``: cropping a product photo
    to a square silently deletes the part of it the photographer cared about.
    """
    from PIL import Image, ImageOps

    with Image.open(BytesIO(payload)) as source:
        width, height = source.size
        if width * height > MAX_SOURCE_PIXELS:
            logger.warning(
                "Refusing to decode a %dx%d image; the ceiling is %d pixels.",
                width,
                height,
                MAX_SOURCE_PIXELS,
            )
            return None

        # Phone cameras store the orientation in EXIF and the pixels sideways. Without
        # this every portrait photo is a landscape thumbnail lying on its side.
        oriented = ImageOps.exif_transpose(source) or source

        transparent = oriented.mode in ("RGBA", "LA", "PA") or "transparency" in oriented.info
        image = oriented.convert("RGBA" if transparent else "RGB")

    edge = box * PIXEL_RATIO
    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)

    buffer = BytesIO()
    if _webp_supported():
        image.save(buffer, format="WEBP", quality=82, method=4)
    elif transparent:
        image.save(buffer, format="PNG", optimize=True)
    else:
        # No alpha to lose, so JPEG rather than a PNG four times the size.
        image.save(buffer, format="JPEG", quality=82, optimize=True, progressive=True)
    return buffer.getvalue()


def _generate(storage: Any, name: str, target: str, box: int) -> str | None:
    """Read the source through storage, resize it, and store the result. Returns a URL."""
    with storage.open(name, "rb") as handle:
        payload = handle.read(MAX_SOURCE_BYTES + 1)

    if len(payload) > MAX_SOURCE_BYTES:
        logger.info("Not thumbnailing %s: larger than %d bytes.", name, MAX_SOURCE_BYTES)
        return None
    if sniff(payload[:HEAD_BYTES]) not in RASTER_TYPES:
        return None

    encoded = _encode(payload, box)
    if encoded is None:
        return None

    # storage.save returns the name it actually used. Two workers racing on the same
    # cold thumbnail both write; the loser gets a suffixed name and an identical image,
    # which is wasteful for one request and correct forever after.
    saved = storage.save(target, ContentFile(encoded))
    return storage.url(saved)


def thumbnail(file: Any, size: str = "sm") -> Thumbnail | None:
    """The derived image for a ``FieldFile``, generating it once if it does not exist.

    ``None`` means "show a chip instead", and is the answer for an empty field, a file
    that is not a raster image, a deployment without Pillow, and anything that went
    wrong. Nothing in here is allowed to raise: a thumbnail is decoration, and a list
    page that 500s because one row holds a truncated JPEG has lost the other forty-nine.
    """
    box = SIZES.get(size, SIZES["sm"])
    name = getattr(file, "name", "") or ""
    if not file or not name or not has_pillow():
        return None
    if _suffix(name) == ".svg":
        return None

    key = f"vantage.thumb:{box}:{hashlib.sha256(name.encode()).hexdigest()[:32]}"
    try:
        cached = _cache().get(key, _MISS)
    except Exception:  # pragma: no cover - a misconfigured cache backend
        cached = _MISS
    if cached is not _MISS:
        return Thumbnail(cached, box, box) if cached else None

    url: str | None = None
    try:
        storage = _storage_of(file)
        target = _derived_name(storage, name, box)
        url = storage.url(target) if storage.exists(target) else None
        if url is None:
            url = _generate(storage, name, target, box)
    except Exception:
        logger.exception("Could not build a %spx thumbnail for %s.", box, name)
        url = None

    try:
        # The empty string is a negative result, cached on purpose: without it a file
        # that cannot be thumbnailed is re-opened and re-decoded on every single render.
        _cache().set(key, url or "", CACHE_SECONDS)
    except Exception:  # pragma: no cover - a misconfigured cache backend
        pass

    return Thumbnail(url, box, box) if url else None


# ── rendering ───────────────────────────────────────────────────────────────


def file_url(file: Any) -> str | None:
    """``file.url``, or ``None`` when the backend cannot produce one.

    ``.url`` raises on a storage configured without a public base — a fact about the
    deployment, not a reason for a list page to 500.
    """
    try:
        return file.url
    except Exception:
        return None


def chip_icon(name: str) -> str:
    """The icon a file with no thumbnail shows."""
    suffix = _suffix(name)
    if suffix in _CHIP_ICONS:
        return _CHIP_ICONS[suffix]
    if suffix in IMAGE_SUFFIXES:
        return "image"
    return "file-text"


def alt_text(file: Any, obj: Any = None) -> str:
    """Alternative text for a record's image.

    Derived from the record, not from the path: ``products/4a7f8e.jpg`` describes
    nothing, while "Blue Widget" is what the picture is *of* as far as this panel knows.
    The filename is the fallback, and is at least a name a person chose.
    """
    label = str(obj) if obj is not None else ""
    if not label.strip():
        label = _basename(getattr(file, "name", "") or "")
    return Truncator(label).chars(100)


def render_file(file: Any, *, obj: Any = None, size: str = "sm") -> SafeString:
    """The markup for one file value: a thumbnail when we can, a named chip when we cannot.

    Rendered through a template rather than ``format_html`` so a project can override
    ``vantage/partials/thumb.html`` — and so the URL passes through ``{% asset %}``,
    which is the tag that exists because ``{% static %}`` mangles a stored media URL
    into ``/static/media/…``. That bug has been fixed once already.
    """
    from django.template.loader import render_to_string

    name = getattr(file, "name", "") or ""
    return render_to_string(
        "vantage/partials/thumb.html",
        {
            "thumb": thumbnail(file, size),
            "file_url": file_url(file),
            "file_name": _basename(name),
            "alt": alt_text(file, obj),
            "icon": chip_icon(name),
            "size": size,
        },
    )


# ── the upload control ──────────────────────────────────────────────────────


class VantageFileInput(forms.ClearableFileInput):
    """A file input that shows what is already there and accepts a dropped file.

    Subclasses ``ClearableFileInput`` rather than replacing it because the clear
    checkbox is the part with the subtle rule: Django emits it only when the field is
    not required, which is exactly "the model said ``blank=True``". Reimplementing that
    is how a panel ends up offering to remove a value the database will refuse to unset.

    Everything the template adds is progressive. With JavaScript off it is a labelled
    ``<input type="file">`` and a checkbox, which is a working upload control.
    """

    template_name = "vantage/widgets/file.html"
    #: Which thumbnail size the preview beside the control uses.
    preview_size = "md"
    #: Whether the field promises an image, which decides preview-versus-chip.
    is_image = False

    def get_context(self, name: str, value: Any, attrs: dict | None) -> dict:
        context = super().get_context(name, value, attrs)
        widget = context["widget"]

        stored = value if self.is_initial(value) else None
        widget["is_image"] = self.is_image
        widget["max_bytes"] = upload_max_bytes()
        widget["preview_box"] = SIZES.get(self.preview_size, SIZES["md"])
        widget["file_name"] = _basename(getattr(stored, "name", "") or "")
        widget["file_url"] = file_url(stored) if stored is not None else None
        widget["thumb"] = thumbnail(stored, self.preview_size) if stored is not None else None
        widget["icon"] = chip_icon(getattr(stored, "name", "") or "")
        # The record is not in scope here, and the adjacent <label> already names the
        # field, so the filename is the honest alternative text on this screen.
        widget["alt"] = widget["file_name"]
        return context


class VantageImageInput(VantageFileInput):
    """The same control, told that a preview is worth drawing."""

    template_name = "vantage/widgets/image.html"
    is_image = True


def file_widget(kind: Any, attrs: dict[str, str]) -> forms.Widget:
    """The upload widget for a :class:`~vantage.widgets.FieldKind`. See ``build_widget``."""
    widget_class = VantageImageInput if str(kind) == "image" else VantageFileInput
    return widget_class(attrs=attrs)
