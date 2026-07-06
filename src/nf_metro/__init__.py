"""nf-metro: Generate metro-map-style SVG diagrams from Mermaid graph definitions."""

from nf_metro.api import RenderConfig, prepare_graph, render_graph, render_string

__version__ = "1.1.0"

__all__ = [
    "__version__",
    "RenderConfig",
    "prepare_graph",
    "render_graph",
    "render_string",
]
