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

import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from nf_metro.layout import compute_layout
from nf_metro.options import LAYOUT_OPTIONS
from nf_metro.parser import parse_metro_mermaid
from nf_metro.parser.directives import apply_legend_directive
from nf_metro.parser.model import LineSpread, MetroGraph
from nf_metro.render import render_svg
from nf_metro.render.html import render_html
from nf_metro.render.style import Theme
from nf_metro.themes import DEFAULT_MODE, THEME_MODES, THEMES

# `style: dark` predates theme names; alias it onto the nfcore brand.
_STYLE_THEME_ALIASES = {"dark": "nfcore"}


@dataclass
class RenderConfig:
    """Render-side options that control SVG/HTML output format and appearance.

    Pass as ``config=RenderConfig(...)`` to :func:`render_string` instead of
    the individual keyword arguments.  When *config* is supplied to
    ``render_string``, the matching flat keyword arguments are ignored.
    """

    output_format: Literal["svg", "html"] = "svg"
    debug: bool = False
    responsive: bool = False
    embed_font: bool = False
    text_to_paths: bool = False
    svg_class_prefix: str = ""
    inject_dark_mode_css: bool = True
    chrome_css: bool = True
    self_color_scheme: bool = True
    bare: bool = False
    embed_basename: str = "metro_map.html"


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
    """Resolve a concrete theme from independent brand and mode axes.

    Brand comes from the explicit ``theme`` name (``--theme``) or the
    ``%%metro style:`` directive (``dark`` aliases to ``nfcore``). Mode comes
    from the explicit ``mode`` argument (``--mode``), then the ``%%metro mode:``
    directive, then ``DEFAULT_MODE``. No brand carries its own mode: a known
    brand always resolves through its light/dark family for the chosen mode.
    """
    if theme is not None:
        brand = theme
    else:
        name = graph.style.strip().lower()
        brand = _STYLE_THEME_ALIASES.get(name, name)

    resolved_mode = (mode or graph.mode).strip().lower() or DEFAULT_MODE
    family = THEME_MODES.get(brand)
    if family and resolved_mode in family:
        return family[resolved_mode]

    return THEMES.get(brand, THEMES["nfcore"])


def render_graph(graph: MetroGraph, theme_obj: Theme, cfg: RenderConfig) -> str:
    """Route a settled graph to the appropriate renderer using *cfg*.

    Use this when you already hold a laid-out graph (e.g. from
    :func:`prepare_graph`) and want the render half of :func:`render_string`
    without re-parsing.
    """
    font_portability: Literal["embed", "paths"] | None = (
        "paths" if cfg.text_to_paths else "embed" if cfg.embed_font else None
    )
    if cfg.output_format == "html":
        return render_html(
            graph,
            theme_obj,
            debug=cfg.debug,
            embed_basename=cfg.embed_basename,
            font_portability=font_portability,
            inject_dark_mode_css=cfg.inject_dark_mode_css,
        )
    return render_svg(
        graph,
        theme_obj,
        debug=cfg.debug,
        responsive=cfg.responsive,
        font_portability=font_portability,
        svg_class_prefix=cfg.svg_class_prefix,
        inject_dark_mode_css=cfg.inject_dark_mode_css,
        chrome_css=cfg.chrome_css,
        self_color_scheme=cfg.self_color_scheme,
        bare=cfg.bare,
    )


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
    config: RenderConfig | None = None,
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
    self_color_scheme: bool = True,
    bare: bool = False,
    embed_basename: str = "metro_map.html",
) -> str:
    """Render *text* to an SVG (default) or interactive HTML string.

    A one-call wrapper over :func:`prepare_graph` plus :func:`render_graph`.
    Callers that also need the graph (e.g. to run
    :func:`nf_metro.render.validate_render` on the output) should call
    :func:`prepare_graph` and :func:`render_graph` directly.

    *config* groups all render-side options into a :class:`RenderConfig` bundle.
    When supplied, the individual render-side keyword arguments (``output_format``,
    ``debug``, ``responsive``, ``embed_font``, ``text_to_paths``,
    ``svg_class_prefix``, ``inject_dark_mode_css``, ``chrome_css``,
    ``self_color_scheme``, ``bare``, ``embed_basename``) are ignored in favour of
    *config*, and passing any of them with a non-default value alongside
    *config* warns. Pass one or the other, not both.

    *self_color_scheme* — when ``True`` (default) the root ``<svg>`` element
    declares ``color-scheme: light dark`` so ``light-dark()`` custom properties
    resolve against the viewer's OS preference. Set ``False`` when inlining the
    SVG into a host page that owns the color-scheme (matches ``--no-self-color-scheme``
    on the CLI).
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
    flat = RenderConfig(
        output_format=output_format,
        debug=debug,
        responsive=responsive,
        embed_font=embed_font,
        text_to_paths=text_to_paths,
        svg_class_prefix=svg_class_prefix,
        inject_dark_mode_css=inject_dark_mode_css,
        chrome_css=chrome_css,
        self_color_scheme=self_color_scheme,
        bare=bare,
        embed_basename=embed_basename,
    )
    if config is not None:
        defaults = RenderConfig()
        shadowed = sorted(
            name
            for name, value in vars(flat).items()
            if value != getattr(defaults, name)
        )
        if shadowed:
            warnings.warn(
                f"render_string: config= supersedes the flat render kwargs; "
                f"ignoring {shadowed}",
                stacklevel=2,
            )
    return render_graph(graph, theme_obj, config or flat)
