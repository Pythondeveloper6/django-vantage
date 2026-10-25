"""The relationship map: every registered model and how they connect.

Two things make an admin's schema view worth building rather than reaching for a
diagramming tool. It is always current, because it is derived from the registry at
request time rather than drawn by hand and left to rot. And every node is a link, so
it doubles as navigation — seeing that Orders point at Customers and clicking straight
through is a different act from reading a picture.

Layout is done here in Python rather than by a JavaScript graph library. A schema graph
is small (tens of nodes, not thousands), and a layered layout computed server-side means
the page needs no JavaScript at all, works with the print stylesheet, and cannot flash.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vantage.introspection import concrete_fields, m2m_fields

__all__ = ["Edge", "Graph", "Node", "build_graph"]

#: Geometry. Chosen so a node's label has room at the default font size and so a
#: typical schema fits a laptop screen without scrolling horizontally.
NODE_WIDTH = 168
NODE_HEIGHT = 46
COLUMN_GAP = 96
ROW_GAP = 22
#: How far a self-referential loop's control points rise above its node.
LOOP_RISE = 40


@dataclass
class Node:
    key: str
    label: str
    url: str | None
    count_fields: int
    x: int = 0
    y: int = 0
    layer: int = 0

    @property
    def cx(self) -> int:
        return self.x + NODE_WIDTH // 2

    @property
    def cy(self) -> int:
        return self.y + NODE_HEIGHT // 2


@dataclass
class Edge:
    source: str
    target: str
    label: str
    kind: str  # fk | o2o | m2m
    #: True when the relation points at the model it is declared on.
    self_referential: bool = False
    path: str = ""


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    width: int = 0
    height: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.nodes


def build_graph(site, request=None) -> Graph:
    """Derive the schema graph from the registry, filtered by what the user may see."""
    from django.urls import NoReverseMatch, reverse

    from vantage.permissions import get_permissions

    panels = list(site.panels())
    if request is not None:
        panels = get_permissions().visible_panels(request, panels)

    nodes: dict[str, Node] = {}
    for panel in panels:
        try:
            url = reverse(panel.list_url_name)
        except NoReverseMatch:  # pragma: no cover
            url = None
        nodes[panel.opts.label_lower] = Node(
            key=panel.opts.label_lower,
            label=panel.get_label_plural(),
            url=url,
            count_fields=len(concrete_fields(panel.model)),
        )

    edges: list[Edge] = []
    for panel in panels:
        source = panel.opts.label_lower
        for f in concrete_fields(panel.model):
            if not (f.is_relation and (f.many_to_one or f.one_to_one)):
                continue
            target = f.related_model._meta.label_lower
            # An edge to a model the user cannot see would be a dangling line, and
            # the fact that the relation exists is itself schema information.
            if target not in nodes:
                continue
            edges.append(
                Edge(
                    source=source,
                    target=target,
                    label=f.name,
                    kind="o2o" if f.one_to_one else "fk",
                    self_referential=source == target,
                )
            )
        for f in m2m_fields(panel.model):
            target = f.related_model._meta.label_lower
            if target not in nodes:
                continue
            edges.append(
                Edge(
                    source=source,
                    target=target,
                    label=f.name,
                    kind="m2m",
                    self_referential=source == target,
                )
            )

    return _assemble(nodes, edges)


def _assemble(nodes: dict[str, Node], edges: list[Edge]) -> Graph:
    """Position the nodes, draw the edges, and size the canvas around both."""
    _lay_out(nodes, edges)
    if any(edge.self_referential for edge in edges):
        # A self-reference loops above its node; leave the top row room for it.
        for node in nodes.values():
            node.y += LOOP_RISE
    ordered = sorted(nodes.values(), key=lambda n: (n.layer, n.y))
    width = max((n.x + NODE_WIDTH for n in ordered), default=0) + 8
    height = max((n.y + NODE_HEIGHT for n in ordered), default=0) + 8

    for edge in edges:
        edge.path = _edge_path(nodes[edge.source], nodes[edge.target])

    return Graph(nodes=ordered, edges=edges, width=width, height=height)


def _lay_out(nodes: dict[str, Node], edges: list[Edge]) -> None:
    """Assign each node a layer, then stack layers left to right.

    Layer 0 holds the models nothing points at from within this graph — the leaves of
    the dependency order, which are usually the lookup tables. Each subsequent layer
    holds models whose own references are all already placed. Cycles are broken by
    placing whatever is left, so a self-referential or mutually-referential schema
    still lays out rather than looping forever.
    """
    outgoing: dict[str, set[str]] = {key: set() for key in nodes}
    for edge in edges:
        if not edge.self_referential:
            outgoing[edge.source].add(edge.target)

    placed: set[str] = set()
    layer = 0
    while len(placed) < len(nodes):
        ready = [k for k in nodes if k not in placed and outgoing[k] <= placed]
        if not ready:
            # A cycle. Place the rest of this round together rather than stalling.
            ready = [k for k in nodes if k not in placed]
        for key in sorted(ready):
            nodes[key].layer = layer
        placed.update(ready)
        layer += 1

    by_layer: dict[int, list[Node]] = {}
    for node in nodes.values():
        by_layer.setdefault(node.layer, []).append(node)

    # Centre each column vertically so the diagram reads as a shape rather than a
    # left-aligned list of boxes.
    tallest = max(
        (len(column) * (NODE_HEIGHT + ROW_GAP) for column in by_layer.values()), default=0
    )
    for index, column in sorted(by_layer.items()):
        column.sort(key=lambda n: n.label.lower())
        span = len(column) * (NODE_HEIGHT + ROW_GAP)
        offset = (tallest - span) // 2
        for position, node in enumerate(column):
            node.x = 8 + index * (NODE_WIDTH + COLUMN_GAP)
            node.y = 8 + offset + position * (NODE_HEIGHT + ROW_GAP)


def _edge_path(source: Node, target: Node) -> str:
    """A cubic bezier between two nodes, as an SVG path.

    Curves rather than straight lines because parallel straight edges between the same
    two columns overlap into an unreadable band.
    """
    if source.key == target.key:
        # A loop above the node, for a self-referential relation.
        x, y = source.cx, source.y
        top = y - LOOP_RISE
        return f"M {x - 22} {y} C {x - 34} {top}, {x + 34} {top}, {x + 22} {y}"

    x1, y1 = source.x, source.cy
    x2, y2 = target.x + NODE_WIDTH, target.cy
    midpoint = (x1 + x2) / 2
    return f"M {x1} {y1} C {midpoint} {y1}, {midpoint} {y2}, {x2} {y2}"
