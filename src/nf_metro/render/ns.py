"""SVG class-namespace utilities shared across render modules."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Generator

__all__ = ["ns", "class_prefix_context", "adaptive_logo_mask_ids"]

_render_class_prefix: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_render_class_prefix", default=""
)


def ns(cls: str) -> str:
    """Apply the active render namespace prefix to an SVG class name."""
    p = _render_class_prefix.get()
    return f"{p}-{cls}" if p else cls


def adaptive_logo_mask_ids() -> tuple[str, str]:
    """Return the SVG mask IDs for an adaptive logo pair.

    There is one mask per display-mode role, not per asset: each is a
    ``light-dark()`` rect sized in ``objectBoundingBox`` units, so its content
    is a function of the role alone and it clips whatever element references it
    to that element's own box. Two assets of different sizes therefore share a
    mask correctly. Keying the ID on the asset would advertise a uniqueness the
    mask does not have, and any key derived from the resolved path would put
    the location of the checkout into the rendered bytes. ``ns()`` supplies the
    per-render prefix that separates SVGs inlined on one page.
    """
    return ns("nfm-logo-mask-dark"), ns("nfm-logo-mask-light")


@contextlib.contextmanager
def class_prefix_context(prefix: str) -> Generator[None, None, None]:
    """Context manager that sets the SVG class namespace prefix for the duration."""
    token = _render_class_prefix.set(prefix)
    try:
        yield
    finally:
        _render_class_prefix.reset(token)
