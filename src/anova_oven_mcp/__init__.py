"""MCP adapter for :mod:`anova_oven`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .server import create_server as create_server

__all__ = ["create_server"]


def __getattr__(name: str) -> Any:
    """Keep optional MCP dependencies lazy for core-library installations."""

    if name != "create_server":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from .server import create_server

    return create_server
