"""Shared rendering entry points for the parse -> layout -> render pipeline.

The CLI (:mod:`nf_metro.cli`) and any embedding caller (the browser playground,
notebooks, other tools) both turn ``.mmd`` text into a settled
:class:`~nf_metro.parser.model.MetroGraph` and an SVG/HTML string. The option
cascade that drives that path - explicit option > ``%%metro`` directive >
default - lives here so both surfaces resolve it identically.

:func:`prepare_graph` returns a laid-out graph (so a caller that also needs the
graph, e.g. for post-render geometry validation, keeps it in hand);
:func:`render_string` is the one-call convenience that returns the rendered
string.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from nf_metro.layout import compute_layout
from nf_metro.options import LAYOUT_OPTIONS
from nf_metro.parser import parse_metro_mermaid
from nf_metro.parser.directives import apply_legend_directive
from nf_metro.parser.model import LineSpread, MetroGraph
from nf_metro.render import render_svg
from nf_metro.render.html import render_html
from nf_metro.render.style import Theme
from nf_metro.themes import THEME_FAMILIES, THEMES

# `style: dark` predates theme names; alias it onto the nfcore theme.
_STYLE_THEME_ALIASES = {"dark": "nfcore"}


def apply_layout_overrides(graph: MetroGraph, opts: Mapping[str, object]) -> None:
    """Write each explicitly-set registry option onto its graph field.

    Parse-time options (``fold_threshold``) are skipped: their value reaches
    the graph through :func:`~nf_metro.parser.parse_metro_mermaid` instead.
    """
    for opt in LAYOUT_OPTIONS:
        if opt.parse_time:
            continue
        value = opts.get(opt.name)
        if value is not None:
            setattr(graph, opt.target_attr, value)


def resolve_theme(
    theme: str | None, graph: MetroGraph, mode: str | None = None
) -> Theme:
    """Resolve the final theme from brand and mode axes independently.

    Resolution order for the brand (palette/identity):
      1. Explicit ``theme`` argument (``--theme`` CLI flag).
      2. ``%%metro style:`` directive on the graph (or ``dark`` alias → ``nfcore``).

    Resolution order for the mode (light/dark):
      1. Explicit ``mode`` argument (``--mode`` CLI flag).
      2. ``%%metro mode:`` directive on the graph.

    When a mode is explicitly set and the brand belongs to a theme family
    (``THEME_FAMILIES``), the family variant for that mode is returned so that
    brand and mode are fully independent axes. When no mode is set, the brand
    name is looked up directly in ``THEMES`` (preserving all existing defaults).
    """
    brand = theme or _STYLE_THEME_ALIASES.get(
        graph.style.strip().lower(), graph.style.strip().lower()
    )
    resolved_mode = (mode or graph.mode).strip().lower() or None

    if resolved_mode and brand in THEME_FAMILIES:
        family = THEME_FAMILIES[brand]
        if resolved_mode in family:
            return family[resolved_mode]

    return THEMES.get(brand, THEMES["nfcore"])


def prepare_graph(
    text: str,
    *,
    from_nextflow: bool = False,
    title: str | None = None,
    line_spread: str | None = None,
    logo: str | None = None,
    legend: str | None = None,
    layout_options: Mapping[str, object] | None = None,
) -> MetroGraph:
    """Parse *text*, apply option overrides, and compute the layout in place.

    Returns the settled graph. Propagates the pipeline's typed errors:
    :class:`ValueError` (parse), and the layout errors
    :class:`~nf_metro.layout.CyclicGraphError`,
    :class:`~nf_metro.layout.BackwardFlowError`,
    :class:`~nf_metro.layout.MixedEntryDirectionError`,
    :class:`~nf_metro.layout.PhaseInvariantError`.
    """
    opts = layout_options or {}

    if from_nextflow:
        from nf_metro.convert import convert_nextflow_dag

        text = convert_nextflow_dag(text, title=title or "")

    fold = opts.get("fold_threshold")
    auto_process = opts.get("auto_process")
    process_scope = opts.get("process_scope")
    graph = parse_metro_mermaid(
        text,
        max_station_columns=fold if isinstance(fold, int) else None,
        auto_process=auto_process if isinstance(auto_process, bool) else None,
        process_scope=process_scope if isinstance(process_scope, str) else None,
    )

    apply_layout_overrides(graph, opts)

    if line_spread is not None:
        graph.line_spread = LineSpread(line_spread)
    if logo is not None:
        graph.logo_path = str(logo)
    if legend is not None:
        apply_legend_directive(legend, graph)
    if title is not None:
        graph.title = title

    compute_layout(graph)
    return graph


def render_string(
    text: str,
    *,
    theme: str | None = None,
    mode: str | None = None,
    output_format: Literal["svg", "html"] = "svg",
    from_nextflow: bool = False,
    title: str | None = None,
    line_spread: str | None = None,
    logo: str | None = None,
    legend: str | None = None,
    layout_options: Mapping[str, object] | None = None,
    debug: bool = False,
    responsive: bool = False,
    embed_font: bool = False,
    text_to_paths: bool = False,
    svg_class_prefix: str = "",
    inject_dark_mode_css: bool = True,
    chrome_css: bool = True,
    bare: bool = False,
    embed_basename: str = "metro_map.html",
) -> str:
    """Render *text* to an SVG (default) or interactive HTML string.

    A one-call wrapper over :func:`prepare_graph` plus the renderer. Callers
    that also need the graph (e.g. to run :func:`nf_metro.render.validate_render`
    on the output) should call :func:`prepare_graph` and the renderer directly.
    """
    graph = prepare_graph(
        text,
        from_nextflow=from_nextflow,
        title=title,
        line_spread=line_spread,
        logo=logo,
        legend=legend,
        layout_options=layout_options,
    )
    theme_obj = resolve_theme(theme, graph, mode=mode)
    font_portability: Literal["embed", "paths"] | None = (
        "paths" if text_to_paths else "embed" if embed_font else None
    )

    if output_format == "html":
        return render_html(
            graph,
            theme_obj,
            debug=debug,
            embed_basename=embed_basename,
            font_portability=font_portability,
            inject_dark_mode_css=inject_dark_mode_css,
        )
    return render_svg(
        graph,
        theme_obj,
        debug=debug,
        responsive=responsive,
        font_portability=font_portability,
        svg_class_prefix=svg_class_prefix,
        inject_dark_mode_css=inject_dark_mode_css,
        chrome_css=chrome_css,
        bare=bare,
    )
