"""SVG class-namespace utilities shared across render modules."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Generator

__all__ = ["ns", "class_prefix_context"]

_render_class_prefix: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_render_class_prefix", default=""
)


def ns(cls: str) -> str:
    """Apply the active render namespace prefix to an SVG class name."""
    p = _render_class_prefix.get()
    return f"{p}-{cls}" if p else cls


@contextlib.contextmanager
def class_prefix_context(prefix: str) -> Generator[None, None, None]:
    """Context manager that sets the SVG class namespace prefix for the duration."""
    token = _render_class_prefix.set(prefix)
    try:
        yield
    finally:
        _render_class_prefix.reset(token)
