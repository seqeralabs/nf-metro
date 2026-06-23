"""HTML wrapper around the SVG renderer for interactive metro maps."""

from __future__ import annotations

import hashlib
import html
import json
from importlib.resources import files
from string import Template
from typing import Literal

from nf_metro.parser.model import MetroGraph
from nf_metro.render.driver import get_driver_js
from nf_metro.render.style import Theme
from nf_metro.render.svg import render_svg

_pkg = files(__package__)


class _JsTemplate(Template):
    # `$` and `${...}` collide with JS template literals (`${x}`, `${ln.color}`).
    # Use `@@` so JS source can be pasted verbatim into the templates.
    delimiter = "@@"


_STANDALONE_TEMPLATE = _JsTemplate(_pkg.joinpath("standalone.html").read_text("utf-8"))
_INLINE_TEMPLATE = _JsTemplate(_pkg.joinpath("inline.html").read_text("utf-8"))


def render_html(
    graph: MetroGraph,
    theme: Theme,
    width: int | None = None,
    height: int | None = None,
    animate: bool | None = None,
    debug: bool = False,
    embed_basename: str = "metro_map.html",
    font_portability: Literal["embed", "paths"] | None = None,
    inject_dark_mode_css: bool = True,
) -> str:
    """Render the graph to an interactive standalone HTML page.

    The HTML side panel replaces the SVG legend in interactive mode.

    ``font_portability`` and ``inject_dark_mode_css`` are forwarded to the
    inlined SVG so an embeddable page can carry its own fonts and opt out of
    the dark-mode media query.  See :func:`nf_metro.render.svg.render_svg`.
    """
    svg = render_svg(
        graph,
        theme,
        width=width,
        height=height,
        animate=animate,
        debug=debug,
        legend_position="none",
        font_portability=font_portability,
        inject_dark_mode_css=inject_dark_mode_css,
    )

    title = graph.title or "nf-metro map"
    lines = [
        {
            "id": lid,
            "label": ln.display_name,
            "color": ln.color,
            "style": ln.style or "solid",
        }
        for lid, ln in graph.lines.items()
    ]
    snippet_id = "m" + hashlib.sha1(svg.encode("utf-8")).hexdigest()[:8]
    inline_snippet = _build_inline_snippet(svg, lines, snippet_id)

    # Browsers terminate the outer <script> the moment they see literal
    # </script>, regardless of JS string context. JSON's optional `\/`
    # escape decodes back to `/`, so the snippet survives round-trip.
    inline_snippet_json = json.dumps(inline_snippet).replace("</", "<\\/")

    return _STANDALONE_TEMPLATE.substitute(
        title=html.escape(title),
        svg=svg,
        lines_json=json.dumps(lines),
        embed_basename=html.escape(embed_basename),
        inline_snippet_json=inline_snippet_json,
        shared_js=get_driver_js(),
    )


def _build_inline_snippet(svg: str, lines: list[dict[str, str]], sid: str) -> str:
    return _INLINE_TEMPLATE.substitute(
        sid=sid,
        svg=svg,
        lines_json=json.dumps(lines),
        shared_js=get_driver_js(),
    )
