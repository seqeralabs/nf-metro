"""Shared rendering entry points for the parse -> layout -> render pipeline.

The CLI (:mod:`nf_metro.cli`) and any embedding caller (the browser playground,
notebooks, other tools) both turn ``.mmd`` text into a settled
:class:`~nf_metro.parser.model.MetroGraph` and an SVG/HTML string. The option
cascade that drives that path - explicit option > ``%%metro`` directive >
default - lives here so both surfaces resolve it identically.

:func:`prepare_graph` returns a laid-out graph for callers that need to inspect
layout state or build a :class:`~nf_metro.render.plan.RenderPlan`.
:func:`render_string` is the one-call convenience that returns the rendered
string.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from nf_metro.layout import PhaseInvariantError, compute_layout
from nf_metro.options import LAYOUT_OPTIONS, is_line_order
from nf_metro.parser import parse_metro_mermaid
from nf_metro.parser.commitments import (
    CommitmentConflictError,
    LayoutCommitmentOverlay,
)
from nf_metro.parser.directives import apply_legend_directive
from nf_metro.parser.model import LineSpread, MetroGraph, PermissiveGuardWarning
from nf_metro.render.font_embed import apply_font_portability
from nf_metro.render.html import emit_render_plan_html
from nf_metro.render.legend import (
    logo_certainly_shows,
    logo_is_resolvable,
    resolve_logo_file,
)
from nf_metro.render.ns import class_prefix_context
from nf_metro.render.plan import RenderPlan
from nf_metro.render.style import Theme
from nf_metro.render.svg import build_render_plan, emit_render_plan
from nf_metro.text_metrics import (
    MetricsFace,
    metrics_face_context,
    metrics_face_for_portability,
)
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
    baked_mode: str | None = None
    bare: bool = False
    embed_basename: str = "metro_map.html"

    @property
    def font_portability(self) -> Literal["embed", "paths"] | None:
        """Return the requested self-contained font strategy."""
        if self.text_to_paths:
            return "paths"
        if self.embed_font:
            return "embed"
        return None

    @property
    def metrics_face(self) -> MetricsFace:
        """Return the face whose metrics must govern this render."""
        return metrics_face_for_portability(self.font_portability)


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
            if opt.name == "line_order" and is_line_order(value):
                graph.layout_provenance.record_caller_line_order(value)


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


@dataclass(frozen=True)
class RenderResult:
    """Rendered content and the immutable plan used to create it."""

    content: str
    plan: RenderPlan


def _emit_svg_plan(graph: MetroGraph, plan: RenderPlan, cfg: RenderConfig) -> str:
    """Emit one production SVG plan with the complete render configuration."""
    with class_prefix_context(cfg.svg_class_prefix):
        content = emit_render_plan(
            plan,
            animate=graph.animate,
            responsive=cfg.responsive,
            inject_dark_mode_css=cfg.inject_dark_mode_css,
            self_color_scheme=cfg.self_color_scheme,
            baked_mode=cfg.baked_mode,
        )
    return apply_font_portability(content, cfg.font_portability)


def render_graph_result(
    graph: MetroGraph, theme_obj: Theme, cfg: RenderConfig
) -> RenderResult:
    """Render a laid-out graph and return its content and plan."""
    if cfg.output_format == "html":
        plan = build_render_plan(
            graph,
            theme_obj,
            debug=cfg.debug,
            legend_position="none",
            metrics_face=cfg.metrics_face,
        )
        content = emit_render_plan_html(
            plan,
            animate=graph.animate,
            embed_basename=cfg.embed_basename,
            font_portability=cfg.font_portability,
            inject_dark_mode_css=cfg.inject_dark_mode_css,
            baked_mode=cfg.baked_mode,
        )
        return RenderResult(content, plan)

    plan = build_render_plan(
        graph,
        theme_obj,
        debug=cfg.debug,
        chrome_css=cfg.chrome_css,
        bare=cfg.bare,
        metrics_face=cfg.metrics_face,
    )
    return RenderResult(_emit_svg_plan(graph, plan, cfg), plan)


def render_graph(graph: MetroGraph, theme_obj: Theme, cfg: RenderConfig) -> str:
    """Render a laid-out graph using *cfg*.

    Use this when you already hold a laid-out graph (e.g. from
    :func:`prepare_graph`) and want the render half of :func:`render_string`
    without re-parsing.
    """
    return render_graph_result(graph, theme_obj, cfg).content


def _prepare_graph_state(
    text: str,
    *,
    from_nextflow: bool = False,
    title: str | None = None,
    line_spread: str | None = None,
    logo: str | None = None,
    legend: str | None = None,
    layout_options: Mapping[str, object] | None = None,
    source_dir: str = "",
    bare: bool = False,
    output_format: Literal["svg", "html"] = "svg",
    layout_commitments: LayoutCommitmentOverlay | None = None,
) -> MetroGraph:
    """Run the production preparation cascade up to the layout boundary."""
    opts = dict(layout_options or {})
    if layout_commitments is not None:
        for name, committed in (
            ("fold_threshold", layout_commitments.caller.fold_threshold),
            ("line_order", layout_commitments.caller.line_order),
        ):
            if committed is None:
                continue
            supplied = opts.get(name)
            if supplied is not None and supplied != committed:
                raise CommitmentConflictError(
                    f"caller {name!r} values disagree: {supplied!r} and {committed!r}"
                )
            opts[name] = committed

    if from_nextflow:
        from nf_metro.convert import convert_nextflow_dag

        text = convert_nextflow_dag(text, title=title or "")

    fold = opts.get("fold_threshold")
    auto_process = opts.get("auto_process")
    process_scope = opts.get("process_scope")
    caller_line_order = opts.get("line_order")
    graph = parse_metro_mermaid(
        text,
        max_station_columns=fold if isinstance(fold, int) else None,
        auto_process=auto_process if isinstance(auto_process, bool) else None,
        process_scope=process_scope if isinstance(process_scope, str) else None,
        caller_line_order=(
            caller_line_order if is_line_order(caller_line_order) else None
        ),
        _layout_commitments=layout_commitments,
    )

    apply_layout_overrides(graph, opts)

    if source_dir:
        graph.source_dir = source_dir
        for attr in ("logo_path", "logo_path_light", "logo_path_dark"):
            raw: str = getattr(graph, attr)
            resolved = resolve_logo_file(raw, source_dir) if raw else ""
            if resolved:
                setattr(graph, attr, resolved)
    if line_spread is not None:
        graph.line_spread = LineSpread(line_spread)
    if logo is not None:
        graph.logo_path = str(logo)
    if legend is not None:
        apply_legend_directive(legend, graph)
    if title is not None:
        graph.title = title

    for attr in ("logo_path", "logo_path_light", "logo_path_dark"):
        raw = getattr(graph, attr)
        if raw and not logo_is_resolvable(raw):
            raise ValueError(f"%%metro logo: path {raw!r} not found")

    logo_in_legend = logo_certainly_shows(graph) and graph.legend_position != "none"
    graph.reserve_title_band = output_format == "html" or (
        not bare and not logo_in_legend
    )
    return graph


def prepare_graph(
    text: str,
    *,
    from_nextflow: bool = False,
    title: str | None = None,
    line_spread: str | None = None,
    logo: str | None = None,
    legend: str | None = None,
    layout_options: Mapping[str, object] | None = None,
    source_dir: str = "",
    bare: bool = False,
    output_format: Literal["svg", "html"] = "svg",
    metrics_face: MetricsFace = MetricsFace.FALLBACK,
) -> MetroGraph:
    """Parse *text*, apply option overrides, and compute the layout in place.

    ``bare`` and ``output_format`` mirror the eventual render call (the CLI's
    ``--bare``/``--format`` flags): used here only to resolve
    ``graph.reserve_title_band`` before layout runs, so the title-band
    clearance in ``layout/phases/canvas.py`` doesn't reserve space for a
    header the render won't actually draw. HTML output always draws its own
    title/logo band regardless of ``bare``/``%%metro legend:`` (its embedded
    SVG forces the legend off and ignores ``bare``), so it always reserves.

    Returns the settled graph.

    **Exception contract.** Propagates the pipeline's typed errors. The
    specific parse/layout-authoring types below all subclass
    :class:`~nf_metro.NfMetroError`, so catching that one type handles "this
    input was rejected" generically; catch a specific type instead to handle
    that case distinctly. A grammar or directive error in the ``.mmd`` text
    can also surface as a plain :class:`ValueError` that is *not* an
    :class:`~nf_metro.NfMetroError` (the parser raises many of these ad hoc,
    not through a dedicated type); catch ``ValueError`` separately to cover
    that case too.

    - A dangling edge or port reference that survived parsing:
      :class:`~nf_metro.parser.UnresolvedEndpointError` /
      :class:`~nf_metro.parser.UnresolvedPortSectionError` (both also
      :class:`ValueError`).
    - A cyclic station graph: :class:`~nf_metro.parser.CyclicGraphError`
      (also a :class:`ValueError`).
    - A section grid that cannot be rendered honestly:
      :class:`~nf_metro.layout.BackwardFlowError` (an inter-section edge
      would have to flow backward) or
      :class:`~nf_metro.layout.MixedEntryDirectionError` (one section is
      entered from more than one approach direction) - both also
      :class:`ValueError`.
    - An engine self-check failing mid-layout:
      :class:`~nf_metro.layout.PhaseInvariantError` (not a ``ValueError``;
      indicates a problem in the layout engine's own intermediate state
      rather than the input).

    This function only parses and lays out the graph; it never draws it, so
    it cannot raise the render-time self-checks described in
    :func:`render_string`'s docstring.

    When ``graph.permissive`` is set (``%%metro permissive: true`` or
    ``--permissive``), a :class:`~nf_metro.layout.PhaseInvariantError` raised
    mid-layout is downgraded to a :class:`UserWarning` instead; the graph is
    returned carrying whatever coordinates the engine had computed before the
    failing phase, for a caller-side best-effort render attempt. A real
    cycle, backward flow, or mixed entry directions raise unconditionally:
    those leave no geometry to fall back to.
    """
    graph = _prepare_graph_state(
        text,
        from_nextflow=from_nextflow,
        title=title,
        line_spread=line_spread,
        logo=logo,
        legend=legend,
        layout_options=layout_options,
        source_dir=source_dir,
        bare=bare,
        output_format=output_format,
    )

    with metrics_face_context(metrics_face):
        if graph.permissive:
            try:
                compute_layout(graph)
            except PhaseInvariantError as e:
                warnings.warn(
                    f"layout guard downgraded under permissive mode: "
                    f"{type(e).__name__}: {e}",
                    category=PermissiveGuardWarning,
                    stacklevel=2,
                )
        else:
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

    Raises everything :func:`prepare_graph` documents, plus three render-only
    cases from the draw step (:func:`render_graph`):

    - A ``fold_threshold`` set too small for the map can leave the router
      unable to separate bundles at the compacted width; this is reframed
      as :class:`~nf_metro.layout.FoldThresholdError` (also an
      :class:`~nf_metro.NfMetroError` and a :class:`ValueError`) naming the
      directive to relax, rather than the render-time engine self-check it
      would otherwise raise.
    - At the map's natural (un-folded) width, a raw render-time self-check
      can fire directly:
      :class:`~nf_metro.layout.routing.invariants.CurveInvariantError`,
      :class:`~nf_metro.layout.routing.convergences.ConvergenceInvariantError`,
      :class:`~nf_metro.render.section_header.SectionHeaderClashError`,
      :class:`~nf_metro.render.section_header.SectionHeaderOverflowError`,
      :class:`~nf_metro.render.section_header.SectionHeaderBandError`,
      :class:`~nf_metro.render.bridges.BridgeInvariantError`, or
      :class:`~nf_metro.layout.routing.offsets.OffsetAnchorError`. These are
      deliberately **not** part of the :class:`~nf_metro.NfMetroError`
      hierarchy: they signal a defect in nf-metro's own drawing of an
      otherwise-valid graph, not a problem with the caller's input, so they
      are left uncaught here the same way the CLI leaves them as a raw
      traceback rather than a clean error message - report them as nf-metro
      bugs rather than handling them as expected input.
    - The post-layout envelope settlement that runs inside the draw step
      checks its own allocation reasoning against the geometry it left, and a
      violation raises :class:`~nf_metro.layout.PhaseInvariantError` from the
      render rather than from layout. Unlike the mid-layout guards of the same
      type, it is never downgraded under ``graph.permissive``: it reports that
      settlement's reasoning and its code have come apart, which leaves no
      geometry worth a best-effort draw.
    """
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
        baked_mode=(mode or "").strip() or None,
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
    effective_cfg = config or flat
    graph = prepare_graph(
        text,
        from_nextflow=from_nextflow,
        title=title,
        line_spread=line_spread,
        logo=logo,
        legend=legend,
        layout_options=layout_options,
        bare=effective_cfg.bare,
        output_format=effective_cfg.output_format,
        metrics_face=effective_cfg.metrics_face,
    )
    if config is None and flat.baked_mode is None:
        flat.baked_mode = graph.mode.strip() or None
    theme_obj = resolve_theme(theme, graph, mode=mode)
    return render_graph(graph, theme_obj, effective_cfg)
