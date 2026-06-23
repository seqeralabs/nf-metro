"""Versioned embed driver for interactive nf-metro SVG maps.

``get_driver_js()`` returns the ``attachMetroMap`` function source, which both
the standalone HTML page and the inline embed snippet inline verbatim so the
two output paths share one implementation.

``attachMetroMap(opts)`` returns a public API object:
``highlightLine``, ``clearHighlight``, ``getManifest``, ``selectNode``, ``reset``.

See ``docs/embed.md`` for the full contract, CSS class names, and integration
examples.
"""

from __future__ import annotations

from importlib.resources import files

DRIVER_CONTRACT_VERSION = "1.0"


def get_driver_js() -> str:
    """Return the embed driver JS source string."""
    return files(__package__).joinpath("driver.js").read_text("utf-8")
