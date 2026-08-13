"""Adapters for other packages.

Nothing in here is imported by the core. Each module soft-detects what it adapts, so
``vantage.contrib`` costs an installed project exactly nothing until it asks for it.
"""

from __future__ import annotations

__all__ = ["adopt_admin_site", "from_admin"]


def __getattr__(name: str):
    """Lazy re-exports, so importing this package never imports ``contrib.admin``."""
    if name in __all__:
        from vantage.contrib import django_admin

        return getattr(django_admin, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
