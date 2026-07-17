"""nf-metro: Generate metro-map-style SVG diagrams from Mermaid graph definitions."""

from nf_metro.api import RenderConfig, prepare_graph, render_graph, render_string
from nf_metro.errors import NfMetroError

__version__ = "1.1.0"

__all__ = [
    "__version__",
    "NfMetroError",
    "RenderConfig",
    "prepare_graph",
    "render_graph",
    "render_string",
]
