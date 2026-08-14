"""The form layout DSL.

``fields`` handles the simple case. This handles the rest, and it is plain nested
Python rather than a configuration dialect:

    layout = Tabs(
        Tab("Content",
            Section(Row("title", "slug"), "body"),
            Section(Row("status", "published_at"), title="Publishing", collapsed=True),
        ),
        Tab("SEO", Section("meta_title", "meta_description")),
    )

Two properties are worth the small amount of machinery. A layout is *declarative
data*, so it can be validated by a system check before anyone loads the page — a
mistyped field name is caught by ``manage.py check`` rather than rendering a form
that quietly omits a field. And it is *inspectable*, so the renderer can ask which
fields a layout covers and add the ones the author forgot rather than dropping them.

That last part matters more than it sounds. A layout that silently omits a required
field produces a form that cannot be saved and gives no clue why, which is among the
worst failure modes an admin can have.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from django.utils.translation import gettext_lazy as _

__all__ = ["Node", "Row", "Section", "Tab", "Tabs", "normalise", "resolve_layout"]


@dataclass
class Node:
    """Base for every container. Children are nodes or field-name strings."""

    children: tuple[Any, ...] = ()

    def fields(self) -> Iterator[str]:
        """Every field name this node contains, at any depth."""
        for child in self.children:
            if isinstance(child, Node):
                yield from child.fields()
            elif child is not None:
                yield str(child)


@dataclass(init=False)
class Row(Node):
    """Fields side by side. Falls to one column on a narrow screen."""

    kind: str = "row"

    def __init__(self, *children: Any) -> None:
        super().__init__(tuple(children))
        self.kind = "row"


@dataclass(init=False)
class Section(Node):
    """A group of fields, optionally titled.

    ``title`` is keyword-only, deliberately. Treating a leading string as the title
    would make ``Section("Publishing", "status")`` and ``Section("title", "body")``
    indistinguishable, and either reading is wrong half the time. One keyword in the
    titled case is a small price for a rule with no exceptions.
    """

    title: str | None = None
    description: str | None = None
    collapsed: bool = False
    show_if: Any = None
    kind: str = "section"

    def __init__(
        self,
        *children: Any,
        title: str | None = None,
        description: str | None = None,
        collapsed: bool = False,
        show_if: Any = None,
    ) -> None:
        super().__init__(tuple(children))
        self.title = title
        self.description = description
        self.collapsed = collapsed
        self.show_if = show_if
        self.kind = "section"


@dataclass(init=False)
class Tab(Node):
    """One tab. The first argument is its label."""

    label: str = ""
    icon: str | None = None
    kind: str = "tab"

    def __init__(self, label: str, *children: Any, icon: str | None = None) -> None:
        super().__init__(tuple(children))
        self.label = label
        self.icon = icon
        self.kind = "tab"

    @property
    def slug(self) -> str:
        return "".join(c if c.isalnum() else "-" for c in self.label.lower()).strip("-")


@dataclass(init=False)
class Tabs(Node):
    """A set of tabs. Must contain only :class:`Tab` nodes."""

    kind: str = "tabs"

    def __init__(self, *children: Tab) -> None:
        for child in children:
            if not isinstance(child, Tab):
                raise TypeError(f"Tabs may only contain Tab, got {type(child).__name__}.")
        super().__init__(tuple(children))
        self.kind = "tabs"


def normalise(node: Any) -> Node:
    """Wrap a bare field name or a sequence into a node, so callers need not care."""
    if isinstance(node, Node):
        return node
    if isinstance(node, str):
        return Section(node)
    if isinstance(node, (list, tuple)):
        return Section(*node)
    raise TypeError(f"A layout node must be a Node, a field name or a sequence, got {node!r}.")


def resolve_layout(layout: Any, available: list[str]) -> Node:
    """Prepare a declared layout for rendering against the form's actual fields.

    Fields the layout names but the form does not have are dropped — usually a field
    the user has no permission to edit, which should disappear quietly rather than
    raise. Fields the form has but the layout forgot are appended in a trailing
    section, because a form missing a required field cannot be saved and gives no
    indication why.
    """
    root = normalise(layout)
    present = set(available)
    covered = {name for name in root.fields() if name in present}
    root = _prune(root, present)

    missing = [name for name in available if name not in covered]
    if missing:
        extra = Section(*missing, title=_("Other"))
        if isinstance(root, Tabs):
            # A stray section floating beside a tab strip has no home; give the
            # leftovers their own tab instead.
            root = Tabs(*root.children, Tab(_("Other"), extra))
        elif root.kind == "section":
            root = Section(*root.children, extra)
        else:
            root = _append(root, extra)

    return root


def _prune(node: Node, present: set[str]) -> Node:
    kept: list[Any] = []
    for child in node.children:
        if isinstance(child, Node):
            pruned = _prune(child, present)
            # An empty container is chrome with nothing in it.
            if list(pruned.fields()):
                kept.append(pruned)
        elif str(child) in present:
            kept.append(child)
    node.children = tuple(kept)
    return node


def _append(root: Node, extra: Node) -> Node:
    root.children = (*root.children, extra)
    return root


def check_layout(panel, available: list[str]) -> list[str]:
    """Field names the layout mentions that the model does not have.

    Reported by the system checks, because a typo here means a field silently
    missing from a form — the kind of bug found weeks later by a confused user.
    """
    if panel.layout is None:
        return []
    from vantage.introspection import resolve_path

    known = set(available)
    unknown = []
    for name in normalise(panel.layout).fields():
        if name in known:
            continue
        if not resolve_path(panel.model, name):
            unknown.append(name)
    return unknown
