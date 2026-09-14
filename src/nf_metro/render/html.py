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
from nf_metro.render.font_embed import apply_font_portability
from nf_metro.render.plan import RenderPlan
from nf_metro.render.style import Theme
from nf_metro.render.svg import build_render_plan, emit_render_plan
from nf_metro.text_metrics import metrics_face_for_portability

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
    baked_mode: str | None = None,
) -> str:
    """Render the graph to an interactive standalone HTML page.

    The HTML side panel replaces the SVG legend in interactive mode.

    ``font_portability``, ``inject_dark_mode_css``, and ``baked_mode`` are
    forwarded to the inlined SVG.  See :func:`nf_metro.render.svg.render_svg`.
    """
    plan = build_render_plan(
        graph,
        theme,
        width=width,
        height=height,
        debug=debug,
        legend_position="none",
        metrics_face=metrics_face_for_portability(font_portability),
    )
    return emit_render_plan_html(
        plan,
        animate=graph.animate if animate is None else animate,
        embed_basename=embed_basename,
        font_portability=font_portability,
        inject_dark_mode_css=inject_dark_mode_css,
        baked_mode=baked_mode,
    )


def emit_render_plan_html(
    plan: RenderPlan,
    *,
    animate: bool = False,
    embed_basename: str = "metro_map.html",
    font_portability: Literal["embed", "paths"] | None = None,
    inject_dark_mode_css: bool = True,
    baked_mode: str | None = None,
) -> str:
    """Create a standalone HTML page from an immutable render plan."""
    svg = emit_render_plan(
        plan,
        animate=animate,
        inject_dark_mode_css=inject_dark_mode_css,
        baked_mode=baked_mode,
    )
    svg = apply_font_portability(svg, font_portability)

    graph = plan.graph
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

    return _STANDALONE_TEMPLATE.substitute(
        title=html.escape(title),
        svg=svg,
        lines_json=_script_safe_json(lines),
        embed_basename=html.escape(embed_basename),
        inline_snippet_json=_script_safe_json(inline_snippet),
        shared_js=get_driver_js(),
    )


def _script_safe_json(value: list[dict[str, str]] | str) -> str:
    """JSON-encode *value* for embedding as a JS literal inside ``<script>``.

    Browsers terminate the outer ``<script>`` the moment they see literal
    ``</script>``, regardless of JS string context. JSON's optional ``\\/``
    escape decodes back to ``/``, so the embedded literal survives round-trip.
    This is the one chokepoint every ``<script>``-embedded JSON value in this
    module goes through, since some of those values (line colours/labels,
    the inline snippet's own SVG) carry directive-authored text.
    """
    return json.dumps(value).replace("</", "<\\/")


def _build_inline_snippet(svg: str, lines: list[dict[str, str]], sid: str) -> str:
    return _INLINE_TEMPLATE.substitute(
        sid=sid,
        svg=svg,
        lines_json=_script_safe_json(lines),
        shared_js=get_driver_js(),
    )
