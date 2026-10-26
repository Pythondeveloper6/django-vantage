"""The map: records plotted by their coordinates, with no map library and no network.

An admin panel that pulls Leaflet from a CDN and tiles from a third-party server has
made two decisions on its owner's behalf: to add a script the CSP must now trust, and
to send every viewer's IP address and the area they are looking at to somebody else.
Neither is this package's to make, so the default is a plain SVG plot rendered on the
server — equirectangular, fitted to the points' own bounds — where every point is a
link to its record and a second link opens the spot on openstreetmap.org, in a new tab,
only when somebody asks.

``VANTAGE["MAP_TILE_URL"]`` is the owner opting in. Set to a template such as
``"https://tile.openstreetmap.org/{z}/{x}/{y}.png"`` it switches the projection to Web
Mercator, picks the deepest zoom at which the points still fit, and lays the raster
tiles under the same SVG. The tile host then has to be allowed by ``img-src`` in the
project's CSP, which is the point: the owner writes that line, knowingly.

The query is bounded: rows without coordinates (or with impossible ones) are excluded
in SQL, and at most :attr:`MapMode.max_points` are fetched.
"""

from __future__ import annotations

import math
from typing import Any

from django.db import models
from django.utils.translation import gettext_lazy as _

from vantage.listviews.base import ListMode, detail_url
from vantage.listviews.inference import is_point_field
from vantage.listviews.kanban import local_field

__all__ = ["MapMode", "fit_equirectangular", "fit_mercator", "osm_url"]

#: The plot's own coordinate space. The SVG scales to its container; these only fix
#: the aspect ratio and the precision the coordinates are rounded to.
WIDTH = 960
HEIGHT = 540
PADDING = 32

#: Web Mercator stops at about ±85.05°; a latitude past it projects to infinity.
MERCATOR_LIMIT = 85.0511
TILE_SIZE = 256
MAX_ZOOM = 18


def osm_url(lat: float, lng: float) -> str:
    """A link to the point on openstreetmap.org. A link only: nothing is fetched."""
    return (
        f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lng:.6f}#map=15/{lat:.5f}/{lng:.5f}"
    )


def _span(low: float, high: float, minimum: float) -> tuple[float, float]:
    """Widen a degenerate range so one point, or a cluster, still has a frame."""
    if high - low < minimum:
        middle = (low + high) / 2
        return middle - minimum / 2, middle + minimum / 2
    return low, high


def fit_equirectangular(points: list[tuple[float, float]]) -> tuple[Any, dict]:
    """A projector from ``(lat, lng)`` to plot coordinates, and the frame it fits.

    Equirectangular because it is the projection a reader needs no explanation for —
    longitude across, latitude up — and because over the extent of most datasets
    (a city, a country) its distortion is invisible. One scale for both axes, so a
    square kilometre stays square-ish rather than being stretched to fill the box.
    """
    lats = [lat for lat, _lng in points]
    lngs = [lng for _lat, lng in points]
    south, north = _span(min(lats), max(lats), 0.02)
    west, east = _span(min(lngs), max(lngs), 0.02)
    scale = min((WIDTH - 2 * PADDING) / (east - west), (HEIGHT - 2 * PADDING) / (north - south))
    offset_x = (WIDTH - (east - west) * scale) / 2
    offset_y = (HEIGHT - (north - south) * scale) / 2

    def project(lat: float, lng: float) -> tuple[float, float]:
        return (
            round(offset_x + (lng - west) * scale, 1),
            round(offset_y + (north - lat) * scale, 1),
        )

    return project, {"south": south, "north": north, "west": west, "east": east}


def _mercator(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    lat = max(-MERCATOR_LIMIT, min(MERCATOR_LIMIT, lat))
    world = TILE_SIZE * (2**zoom)
    x = (lng + 180.0) / 360.0 * world
    radians = math.radians(lat)
    y = (1.0 - math.log(math.tan(radians) + 1.0 / math.cos(radians)) / math.pi) / 2.0 * world
    return x, y


def fit_mercator(points: list[tuple[float, float]], template: str) -> tuple[Any, list[dict]]:
    """A projector in Web Mercator pixels, and the raster tiles that sit under it.

    The deepest zoom at which every point still fits inside the plot, so a city's
    worth of records is drawn at street level and a continent's at country level.
    """
    zoom = 0
    for candidate in range(MAX_ZOOM, -1, -1):
        xs, ys = zip(*(_mercator(lat, lng, candidate) for lat, lng in points), strict=True)
        if max(xs) - min(xs) <= WIDTH - 2 * PADDING and max(ys) - min(ys) <= HEIGHT - 2 * PADDING:
            zoom = candidate
            break
    xs, ys = zip(*(_mercator(lat, lng, zoom) for lat, lng in points), strict=True)
    origin_x = (max(xs) + min(xs)) / 2 - WIDTH / 2
    origin_y = (max(ys) + min(ys)) / 2 - HEIGHT / 2

    def project(lat: float, lng: float) -> tuple[float, float]:
        x, y = _mercator(lat, lng, zoom)
        return round(x - origin_x, 1), round(y - origin_y, 1)

    count = 2**zoom
    tiles = []
    for tile_y in range(int(origin_y // TILE_SIZE), int((origin_y + HEIGHT) // TILE_SIZE) + 1):
        if not 0 <= tile_y < count:
            continue
        for tile_x in range(int(origin_x // TILE_SIZE), int((origin_x + WIDTH) // TILE_SIZE) + 1):
            url = (
                template.replace("{z}", str(zoom))
                .replace("{x}", str(tile_x % count))
                .replace("{y}", str(tile_y))
            )
            tiles.append(
                {
                    "url": url,
                    "x": round(tile_x * TILE_SIZE - origin_x, 1),
                    "y": round(tile_y * TILE_SIZE - origin_y, 1),
                }
            )
    return project, tiles


def _nice_step(span: float) -> float:
    """A round graticule interval giving three to six lines across ``span``."""
    raw = span / 4
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 5, 10):
        if raw <= multiple * magnitude:
            return multiple * magnitude
    return 10 * magnitude  # pragma: no cover


class MapMode(ListMode):
    name = "map"
    label = _("Map")
    icon = "map-pin"
    template = "vantage/list_views/map.html"

    #: The most points one plot draws. Past it the page says so.
    max_points = 1000

    @classmethod
    def supports(cls, panel) -> bool:
        fields = panel.get_map_fields()
        if isinstance(fields, str):
            field = local_field(panel.model, fields)
            return field is not None and is_point_field(field)
        if not fields or len(fields) != 2:
            return False
        numbers = (models.FloatField, models.DecimalField)
        return all(isinstance(local_field(panel.model, name), numbers) for name in fields)

    def filtered(self, queryset):
        fields = self.panel.get_map_fields()
        if isinstance(fields, str):
            return queryset.filter(**{f"{fields}__isnull": False})
        lat, lng = fields
        return queryset.filter(
            **{
                f"{lat}__isnull": False,
                f"{lng}__isnull": False,
                f"{lat}__gte": -90,
                f"{lat}__lte": 90,
                f"{lng}__gte": -180,
                f"{lng}__lte": 180,
            }
        )

    def coordinates(self, obj) -> tuple[float, float] | None:
        fields = self.panel.get_map_fields()
        if isinstance(fields, str):
            point = getattr(obj, fields, None)
            if point is None:
                return None
            return float(point.y), float(point.x)
        lat, lng = (getattr(obj, name, None) for name in fields)
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)

    def context(self, request, queryset, *, permissions) -> dict[str, Any]:
        from vantage.conf import vantage_settings

        rows = list(self.filtered(queryset)[: self.max_points + 1])
        truncated = len(rows) > self.max_points
        rows = rows[: self.max_points]

        located = []
        for obj in rows:
            coords = self.coordinates(obj)
            if coords is not None:
                located.append((obj, coords))

        template = vantage_settings.MAP_TILE_URL
        tiles: list[dict] = []
        graticule: list[dict] = []
        points: list[dict] = []
        if located:
            coords = [coords for _obj, coords in located]
            if template:
                project, tiles = fit_mercator(coords, template)
            else:
                project, frame = fit_equirectangular(coords)
                graticule = self.graticule(project, frame)
            for obj, (lat, lng) in located:
                x, y = project(lat, lng)
                points.append(
                    {
                        "obj": obj,
                        "pk": obj.pk,
                        "title": str(obj),
                        "url": detail_url(self.panel, obj),
                        "osm_url": osm_url(lat, lng),
                        "lat": f"{lat:.5f}",
                        "lng": f"{lng:.5f}",
                        "x": x,
                        "y": y,
                    }
                )

        return {
            "map_points": points,
            "map_tiles": tiles,
            "map_graticule": graticule,
            "map_width": WIDTH,
            "map_height": HEIGHT,
            "map_truncated": truncated,
            "map_max": self.max_points,
        }

    @staticmethod
    def graticule(project, frame: dict) -> list[dict]:
        """Faint lines at round degrees, labelled, so the plot has a sense of place."""
        lines = []
        for axis, low, high in (
            ("lat", frame["south"], frame["north"]),
            ("lng", frame["west"], frame["east"]),
        ):
            step = _nice_step(high - low)
            value = math.ceil(low / step) * step
            while value <= high:
                if axis == "lat":
                    _x, y = project(value, frame["west"])
                    lines.append({"axis": axis, "y": y, "label": f"{value:g}°"})
                else:
                    x, _y = project(frame["north"], value)
                    lines.append({"axis": axis, "x": x, "label": f"{value:g}°"})
                value += step
        return lines
