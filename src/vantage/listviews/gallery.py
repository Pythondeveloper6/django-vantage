"""The gallery: a card grid led by each record's picture.

Paginated exactly like the table — the same :class:`~vantage.pagination.CappedPaginator`,
the same ``per_page``, the same ``?page=`` — so moving between the two keeps your
place, and so the gallery inherits the paginator's capped count rather than running an
unbounded ``COUNT(*)`` of its own.

Pictures come from :mod:`vantage.media` at the detail page's ``lg`` size, which means
the derived thumbnail is shared with the detail page rather than being a fourth size on
the storage bill, and that with Pillow absent a card degrades to the same named file
chip a table cell would show — never a broken image.
"""

from __future__ import annotations

from typing import Any

from django.db import models
from django.utils.translation import gettext_lazy as _

from vantage.listviews.base import ListMode, card_for, field_labels, with_related
from vantage.listviews.kanban import local_field

__all__ = ["GalleryMode"]


class GalleryMode(ListMode):
    name = "gallery"
    label = _("Gallery")
    icon = "image"
    template = "vantage/list_views/gallery.html"

    @classmethod
    def supports(cls, panel) -> bool:
        return isinstance(local_field(panel.model, panel.get_gallery_field()), models.FileField)

    @property
    def field(self) -> models.FileField:
        return local_field(self.panel.model, self.panel.get_gallery_field())

    def context(self, request, queryset, *, permissions) -> dict[str, Any]:
        from vantage.media import _basename, alt_text, chip_icon, thumbnail
        from vantage.pagination import CappedPaginator

        name = self.field.name
        card_fields = self.panel.get_card_fields(request, exclude=(name,))
        queryset = with_related(queryset, self.panel.model, card_fields)
        paginator = CappedPaginator(
            queryset,
            self.panel.get_per_page(request),
            request=request,
            permissions=permissions,
        )
        page = paginator.get_page(request.GET.get("page") if request else None)
        labels = field_labels(self.panel, card_fields)

        cards = []
        for obj in page.object_list:
            card = card_for(self.panel, obj, card_fields, labels)
            file = getattr(obj, name, None)
            name_on_disk = getattr(file, "name", "") or ""
            # The picture links to the record, not to the file: on a gallery the card
            # is the way into the record, and a thumbnail that opened a raw JPEG in a
            # new tab would make the largest target on the card the wrong one.
            card["thumb"] = thumbnail(file, "lg") if name_on_disk else None
            card["file_name"] = _basename(name_on_disk)
            card["file_icon"] = chip_icon(name_on_disk) if name_on_disk else "image"
            card["alt"] = alt_text(file, obj)
            cards.append(card)

        return {
            "gallery_field": name,
            "gallery_cards": cards,
            "page": page,
            "paginator": paginator,
            "row_count": paginator.row_count,
        }
