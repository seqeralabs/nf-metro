"""CLI for nf-metro."""

from __future__ import annotations

import copy
import json
import math
import os
import sys
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeVar, cast, get_args

import click

from nf_metro import __version__
from nf_metro.api import (
    RenderConfig,
    _parse_source,
    prepare_graph,
    render_graph_result,
    resolve_theme,
)
from nf_metro.explain import build_explain, format_explain_json, format_explain_text
from nf_metro.introspect import build_info, format_info_json, format_info_text
from nf_metro.layout import (
    BackwardFlowError,
    FoldThresholdError,
    MixedEntryDirectionError,
    PhaseInvariantError,
    compute_layout,
)
from nf_metro.live.server import DEFAULT_OVERLAY, OVERLAY_STYLES
from nf_metro.options import LAYOUT_OPTIONS, LayoutOption
from nf_metro.parser import (
    ERROR,
    WARNING,
    CyclicGraphError,
    ValidationIssue,
    parse_metro_mermaid,
    validate_graph,
)
from nf_metro.parser.model import (
    LineSpread,
    MetroGraph,
    PermissiveGuardWarning,
    split_guard_warnings,
)
from nf_metro.render import validate_render
from nf_metro.render.video import VIDEO_FORMATS, NotAnimatedError, VideoFormat
from nf_metro.themes import DEFAULT_MODE, STYLE_NAMES, THEMES, resolve_style

RenderFormat = Literal["svg", "html", "png", "gif", "webp", "mp4", "webm"]
_RENDER_FORMATS: tuple[str, ...] = get_args(RenderFormat)

#: Formats the rasteriser draws rather than ones written as text. All of them
#: need the same picture-is-final decisions a PNG needs (see _render_one_unsafe).
_RASTER_FORMATS: frozenset[str] = frozenset({"png", *VIDEO_FORMATS})

#: Frames per second an exported loop runs at unless ``--fps`` says otherwise.
DEFAULT_FPS = 12.0

#: Every typed failure the parse/layout pipeline raises for a rejected map,
#: whether it surfaces during planning's parse-only pass or the render's
#: full parse+layout.
_SOURCE_ERRORS = (
    ValueError,
    CyclicGraphError,
    BackwardFlowError,
    MixedEntryDirectionError,
    PhaseInvariantError,
)


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """nf-metro: Generate metro-map-style SVG diagrams from Mermaid definitions."""


_F = TypeVar("_F", bound=Callable[..., Any])


def _parse_inactive_lines(value: object) -> frozenset[str] | None:
    """Normalise an ``--inactive-lines`` value to a set of line IDs, or ``None``.

    Accepts a comma-separated string (the CLI form) or a JSON list of strings
    (the ``render-many`` manifest form). ``None`` (option absent) stays ``None``,
    meaning "no override, use the map's own defaults"; an empty string or list
    yields an empty set, forcing every line active for this render.
    """
    if value is None:
        return None
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, Iterable):
        raise ValueError("inactive_lines must be a string or list of line IDs")
    return frozenset(s for s in (str(i).strip() for i in items) if s)


def _project_root(start: Path) -> Path | None:
    """The git working tree containing *start*, or None if not in one.

    Walks up for a ``.git`` entry - a directory in a normal clone, a file in a
    submodule or linked worktree. No ``git`` binary required, so this works in
    the stripped-down containers CI renders run in.
    """
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _declared_outputs(
    graph: MetroGraph, source: Path, *, reject_outside_source: bool
) -> list[tuple[Path, dict[str, object]]]:
    """The map's ``%%metro output:`` declarations, resolved beside *source*.

    The directive itself is parsed by the parser's own handler (``_dir_output``
    in ``parser/directives.py``), which is the only place that knows the
    ``%%metro key: value`` syntax; all that is left here is where a relative
    declared path points. Each declaration keeps the per-output render
    overrides it was written with, which job planning folds over the run's
    global options. *source*'s own parent is used rather than
    ``graph.source_dir`` (an absolute path), so a relative INPUT_FILE yields
    relative paths in the printed result document.

    With *reject_outside_source*, a declared path resolving outside the git
    working tree holding *source* - or into any ``.git/`` within it - is
    rejected rather than written to: a caller rendering a map it did not
    author (a CI job rendering a fork PR's .mmd, say) has no other chance to
    review where the map's own directive would write, and the write happens
    as an ordinary part of this same process, before any caller could inspect
    and reject the path itself.

    The repository, not the .mmd's own directory, is the boundary because that
    is where the trust boundary actually is: a fork author already controls
    every file in the PR tree, so a write inside the checkout grants them
    nothing they did not already have, while ``assets/metro_map.mmd`` writing
    ``../docs/images/map.svg`` is an ordinary first-party layout. Git's own
    metadata is the one exception - config and hooks there become code on the
    next ``git`` call in the same job - so any ``.git`` path component is
    refused, which covers a nested ``assets/.git/`` the same as the checkout's
    own. Outside a repository there is no tree to scope to, so the old
    source-directory rule stands in.
    """
    base = source.parent.resolve()
    root = _project_root(base)
    boundary = root or base
    # Outside a repository the boundary is the source directory, and saying
    # "the pipeline repository" there would name something that isn't here.
    scope = "pipeline repository" if root is not None else "map's own directory"
    resolved: list[tuple[Path, dict[str, object]]] = []
    for p, overrides in graph.declared_outputs:
        # Collapsed lexically so a legitimate in-repo hop reads as the path
        # it means: `assets/metro_map.mmd` declaring `../docs/images/map.svg`
        # reports `docs/images/map.svg`, not `assets/../docs/images/map.svg`,
        # which is what the GitHub Action publishes as `output-path`. The
        # check below resolves this same collapsed path, so what is validated
        # and what is written are always the same file.
        candidate = Path(os.path.normpath(source.parent / p))
        target = candidate.resolve()
        escapes = not target.is_relative_to(boundary)
        reaches_git = not escapes and ".git" in target.relative_to(boundary).parts
        if reject_outside_source and (escapes or reaches_git):
            raise click.ClickException(
                f"{source}: %%metro output: {p!r} resolves outside the "
                f"{scope} (or into a .git/); declared paths must stay within "
                f"{boundary} and out of .git/"
            )
        resolved.append((candidate, overrides))
    return resolved


# One planned write: (label, output path, format, per-output overrides).
_OutputJob = tuple[str, Path, "RenderFormat", dict[str, object]]


def _format_from_output(output: Path | None) -> RenderFormat:
    """Infer the output format from *output*'s extension, defaulting to SVG.

    Lets ``-o map.png`` stand on its own, so the common case needs no
    ``--format``. An explicit ``--format`` is resolved before this is called
    and wins, including over a mismatched extension.
    """
    suffix = output.suffix.lower().lstrip(".") if output is not None else ""
    return cast(
        RenderFormat,
        suffix if suffix in _RENDER_FORMATS else "svg",
    )


def _svg_format_for(out_format: RenderFormat) -> Literal["svg", "html"]:
    """Return the format a graph is prepared/rendered under.

    Only the interactive page has a backend of its own; PNG and the looping
    video formats are all drawn from the SVG.
    """
    return "html" if out_format == "html" else "svg"


def _default_scale(format_: str) -> float:
    """Return the raster scale to use when ``--scale`` is not given.

    A PNG is usually a retina still, so it doubles by default. A loop is a few
    hundred of those frames in one file, where doubling quadruples both the
    bytes and the time for a picture that plays at screen size anyway.
    """
    return 1.0 if format_ in VIDEO_FORMATS else 2.0


def _forces_animation(
    out_format: RenderFormat, layout_opts: Mapping[str, object]
) -> bool:
    """Whether *out_format* turns the animation on for a caller who did not.

    A video of a map with no balls on it would be a still repeated a few
    hundred times, so asking for one asks for the animation. An explicit
    --animate/--no-animate still wins.
    """
    return out_format in VIDEO_FORMATS and layout_opts.get("animate") is None


def _layout_opts_for(
    out_format: RenderFormat, layout_opts: dict[str, object]
) -> dict[str, object]:
    """Return the layout options *out_format* is rendered under."""
    if not _forces_animation(out_format, layout_opts):
        return layout_opts
    return {**layout_opts, "animate": True}


def _graph_key(
    out_format: RenderFormat, layout_opts: Mapping[str, object]
) -> tuple[Literal["svg", "html"], bool]:
    """Return the cache key for the graph *out_format* is rendered from.

    Two outputs share a prepared graph only when they share both a backend and
    an animation state - whether the animation came from the run's own
    --animate, from a video format forcing it on, or from one output's
    ``%%metro output: ... | animate`` override. Mode and theme are absent on
    purpose: they are baked when the graph is serialised, not laid out, so a
    static SVG and a light/dark PNG pair still share a single layout run.
    """
    animated = bool(layout_opts.get("animate")) or _forces_animation(
        out_format, layout_opts
    )
    return _svg_format_for(out_format), animated


class _FiniteFloatRange(click.FloatRange):
    """A float range that also refuses a non-finite value.

    A bound cannot catch these on its own: every comparison against ``nan`` is
    false, and ``inf`` satisfies an option that declares no maximum. Either
    reaches the drawn SVG as an unusable attribute value.
    """

    def _describe_range(self) -> str:
        """Return the bound hint for ``--help``, empty when there is no bound.

        click describes a range with no bounds at all as ``x<=None``.
        """
        if self.min is None and self.max is None:
            return ""
        return super()._describe_range()

    def convert(
        self, value: float, param: click.Parameter | None, ctx: click.Context | None
    ) -> float:
        converted = super().convert(value, param, ctx)
        if not math.isfinite(converted):
            self.fail(f"{value!r} is not a finite number", param, ctx)
        return converted


def _numeric_cli_type(opt: LayoutOption) -> click.IntRange | _FiniteFloatRange:
    """Build the click type enforcing a numeric option's declared bounds.

    The registry's ``sign`` and ``max_val`` gate the directive plane through
    :func:`nf_metro.options.coerce`; mirroring them here keeps a flag from
    admitting a value the same option refuses as a ``%%metro`` directive,
    where a refusal warns and keeps the default rather than exiting.
    """
    lo = None if opt.sign == "any" else 0
    min_open = opt.sign == "positive"
    if opt.kind == "int":
        hi = None if opt.max_val is None else int(opt.max_val)
        return click.IntRange(min=lo, max=hi, min_open=min_open)
    return _FiniteFloatRange(min=lo, max=opt.max_val, min_open=min_open)


# Reused by the render-many manifest reader below, so a bad value gets the
# same error there as it would from the flag.
_SCALE_TYPE = _FiniteFloatRange(min=0, min_open=True)
_FPS_TYPE = _FiniteFloatRange(min=0, min_open=True)
_DURATION_TYPE = _FiniteFloatRange(min=0, min_open=True)
_RASTER_WIDTH_TYPE = click.IntRange(min=0, min_open=True)
_FORMAT_TYPE = click.Choice(_RENDER_FORMATS)


def _convert_manifest_number(
    param_type: click.ParamType[float | int, object], value: object, name: str
) -> float | int:
    """Run a manifest job's *value* through *param_type*, as the CLI flag would.

    ``bool`` is an ``int`` subclass in Python, so a JSON ``true``/``false``
    would otherwise pass ``_SCALE_TYPE``/``_RASTER_WIDTH_TYPE`` as ``1``/``0``; a
    CLI flag can never receive a bool, so this refuses one here too.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name}: {value!r} is not a number")
    return param_type.convert(value, None, None)


def _layout_cli_option(opt: LayoutOption) -> Callable[..., Any]:
    """Build the ``click.option`` decorator for a registry option.

    All default to ``None`` so an omitted flag leaves the directive value in
    place; a set flag overrides it (the CLI half of the cascade in
    :mod:`nf_metro.options`).
    """
    if opt.kind == "bool":
        no_flag = "--no-" + opt.name.replace("_", "-")
        return click.option(
            f"{opt.cli_flag}/{no_flag}",
            opt.name,
            default=None,
            help=opt.help,
            hidden=opt.hidden,
        )
    ctype: Any
    metavar: str | None = None
    if opt.kind == "choice":
        ctype = click.Choice(opt.choices)
    elif opt.kind in ("int", "float"):
        ctype = _numeric_cli_type(opt)
        # A bounded option's type name would otherwise read "FLOAT RANGE" in
        # --help, where click already appends the bound itself as "[x>0]".
        metavar = "INTEGER" if opt.kind == "int" else "FLOAT"
    else:
        ctype = {"str": str}[opt.kind]
    return click.option(
        opt.cli_flag,
        opt.name,
        type=ctype,
        default=None,
        help=opt.help,
        metavar=metavar,
        hidden=opt.hidden,
    )


def layout_cli_options(f: _F) -> _F:
    """Attach a CLI flag for every registry option, in declaration order."""
    for opt in reversed(LAYOUT_OPTIONS):
        f = _layout_cli_option(opt)(f)
    return f


def _echo_block(label: str, entries: Iterable[str]) -> None:
    """Print a labelled, bulleted block to stderr.

    An entry spanning several lines keeps its continuation lines indented
    under its own bullet, so a guard message carrying per-defect detail reads
    as one item.
    """
    click.echo(f"{label}:", err=True)
    for entry in entries:
        head, *rest = entry.split("\n")
        click.echo(f"  - {head}", err=True)
        for line in rest:
            click.echo(f"    {line.strip()}", err=True)


def _echo_issues(
    label: str, issues: Iterable[ValidationIssue], path: Path | str
) -> None:
    """Print a block of validation issues, each formatted against *path*."""
    _echo_block(label, (issue.format(path) for issue in issues))


def _error_prefix(input_file: Path, quiet: bool) -> str:
    """Return the file prefix an error message needs, if any.

    A batch run labels each job with its file already, so repeating it inside
    the message names the same file twice.
    """
    return "" if quiet else f"{input_file}: "


def _debug_reraise() -> bool:
    """Return whether ``NF_METRO_DEBUG=1`` asks for tracebacks over messages."""
    return os.environ.get("NF_METRO_DEBUG") == "1"


def _clean_error(exc: Exception, prefix: str = "") -> NoReturn:
    """Re-raise *exc* under ``NF_METRO_DEBUG=1``, else present it as one line."""
    if _debug_reraise():
        raise exc
    raise click.ClickException(f"{prefix}{exc}")


def _parse_reporting_warnings(
    input_file: Path, text: str, *, label: str = "Warnings"
) -> tuple[MetroGraph, list[str]]:
    """Parse *text*, returning the graph and the warnings the parse raised.

    A parse failure reports what was recorded before it under *label*: where a
    malformed directive is the real fault, the warning names it and the error
    names only the consequence.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            graph = parse_metro_mermaid(text)
        except ValueError as e:
            if caught:
                _echo_block(label, [str(w.message) for w in caught])
            _clean_error(e, f"{input_file}: ")
    # A `%%metro logo:` path resolves against the directory the map came from,
    # which only this function knows here.
    graph.source_dir = str(input_file.resolve().parent)
    return graph, [str(w.message) for w in caught]


def _report_render_warnings(
    caught: list[warnings.WarningMessage],
    *,
    permissive: bool,
    source: Path | None = None,
) -> None:
    """Present a render's captured warnings as labelled stderr blocks.

    Guard downgrades get their own block: each one names geometry that was
    drawn anyway and may be defective there, unlike a warning about something
    nf-metro ignored or adjusted. *source* labels the blocks for a batch
    render, whose files would otherwise be indistinguishable.
    """
    guard_warnings, other_warnings = split_guard_warnings(caught)
    suffix = f" ({source})" if source is not None else ""
    if other_warnings:
        _echo_block(f"Warnings{suffix}", (str(w.message) for w in other_warnings))
    if guard_warnings:
        flag = "--permissive: " if permissive else ""
        _echo_block(
            f"{flag}{len(guard_warnings)} guard(s) downgraded to warnings{suffix}; "
            "the rendered geometry may be defective at these points",
            (str(w.message) for w in guard_warnings),
        )


def _run_batch(items: list[tuple[str, Callable[[], None]]]) -> None:
    """Run every item's callable, printing a ``[i/total] OK``/``FAIL`` line.

    Every item runs regardless of whether an earlier one raised, so
    successful outputs are kept. Catches any exception type, since a
    callable's failure mode (parse error, I/O error, an unanticipated bug)
    isn't known to this generic runner. Raises a single ``ClickException``
    summarising the failure count if anything failed (the per-item FAIL
    lines, already on stderr, carry the detail).
    """
    total = len(items)
    failure_count = 0
    for idx, (label, job) in enumerate(items, 1):
        try:
            job()
        except Exception as e:
            if _debug_reraise():
                raise
            failure_count += 1
            click.echo(f"[{idx}/{total}] FAIL  {label}: {e}", err=True)
        else:
            click.echo(f"[{idx}/{total}] OK    {label}", err=True)
    if failure_count:
        raise click.ClickException(
            f"{failure_count}/{total} render(s) failed; "
            "see stderr output above for details"
        )


@cli.command()
@click.argument(
    "input_files", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option(
    "-o",
    "--output",
    "outputs",
    type=click.Path(path_type=Path),
    multiple=True,
    help="Output file path. Defaults to <input>.<format>. Only valid with a "
    "single INPUT_FILE. Repeat it to write several formats from one layout "
    "run: -o map.svg -o map.png.",
)
@click.option(
    "--format",
    "format_",
    type=_FORMAT_TYPE,
    default=None,
    help="Output format: 'svg' (default), 'png', 'html' for an interactive "
    "self-contained page with pan/zoom and per-line filtering, or one of "
    "'gif'/'webp'/'mp4'/'webm' for a looping video of the animation. Inferred "
    "from the --output extension when not given.",
)
@click.option(
    "--scale",
    type=_SCALE_TYPE,
    default=None,
    metavar="FLOAT",
    help="Raster formats only: multiply the rendered pixel dimensions by this "
    "factor.  [default: 2 for png, 1 for gif/webp/mp4/webm]",
)
@click.option(
    "--raster-width",
    type=_RASTER_WIDTH_TYPE,
    default=None,
    metavar="INTEGER",
    help="Raster formats only: output width in pixels, height scaled with it. "
    "Overrides --scale. Distinct from --width, which grows the SVG canvas "
    "around a map drawn at its natural size rather than resizing the picture.",
)
@click.option(
    "--fps",
    type=_FPS_TYPE,
    default=DEFAULT_FPS,
    show_default=True,
    metavar="FLOAT",
    help="Video formats only: frames per second of the exported loop.",
)
@click.option(
    "--duration",
    type=_DURATION_TYPE,
    default=None,
    metavar="FLOAT",
    help="Video formats only: length of one loop in seconds, compressing (or "
    "stretching) the map's own animation cycle into it. Defaults to that "
    "cycle, which keeps the balls at exactly the speed the animated SVG "
    "moves them.",
)
@click.option(
    "--theme",
    type=click.Choice(sorted(STYLE_NAMES)),
    default=None,
    help="Visual theme (default: from the %%metro style: directive, else nfcore).",
)
@click.option(
    "--mode",
    type=click.Choice(["light", "dark"]),
    default=None,
    help=(
        "Display mode, independent of the brand theme (default: from the "
        "%%metro mode: directive, else the brand's own default mode). Bakes the "
        "chosen mode's palette - use for light/dark PNG export."
    ),
)
@click.option(
    "--debug/--no-debug",
    default=False,
    help="Show debug overlay (ports, hidden stations, edge waypoints)",
)
@click.option(
    "--logo",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Logo image path (overrides the %%metro logo: directive).",
)
@click.option(
    "--line-spread",
    type=click.Choice([m.value for m in LineSpread]),
    default=None,
    help="How lines sharing a station relate vertically: 'bundle' (default) "
    "merges onto one trunk, 'centered' balances the bundle about the midline, "
    "'rails' draws parallel rails with interchange stations. Overrides the "
    "graph-wide %%metro line_spread: directive (per-section overrides stay).",
)
@click.option(
    "--legend",
    default=None,
    help="Position the legend+logo block (overrides the %%metro legend: "
    "directive). Keyword (bl/br/tl/tr/bottom/right/none), '<keyword> | canvas', "
    "'<keyword> | dx,dy', or absolute 'x,y'.",
)
@click.option(
    "--from-nextflow",
    is_flag=True,
    default=False,
    help="Convert Nextflow -with-dag mermaid input before rendering",
)
@click.option(
    "--title",
    type=str,
    default=None,
    help="Pipeline title (overrides the %%metro title: directive).",
)
@click.option(
    "--responsive/--no-responsive",
    default=False,
    help="Emit viewBox only (no fixed width/height) for CSS-scalable embedding.",
)
@click.option(
    "--embed-font/--no-embed-font",
    default=False,
    help=(
        "Inline a subset of Inter as a base64 @font-face block so the SVG "
        "renders identically on any host regardless of installed fonts."
    ),
)
@click.option(
    "--text-to-paths/--no-text-to-paths",
    default=False,
    help=(
        "Convert all text to vector paths, removing font dependencies entirely. "
        'Loses selectable text. Needs pip install "nf-metro[font]".'
    ),
)
@click.option(
    "--svg-class-prefix",
    type=str,
    default="",
    help=(
        "Prefix every SVG presentation class with this string (e.g. 'myapp' "
        "produces 'myapp-nf-metro-station'). Use distinct prefixes for each map "
        "on a shared page to prevent CSS collisions. Has no effect on the "
        "interactive HTML output, which already scopes each map independently."
    ),
)
@click.option(
    "--no-self-color-scheme",
    is_flag=True,
    default=False,
    help=(
        "Omit the color-scheme: light dark attribute from the root <svg> "
        "element. Use when inlining the SVG into a host page that owns the "
        "theme (e.g. the docs site): the SVG then inherits the page's "
        "color-scheme so a manual light/dark toggle drives light-dark() "
        "resolution rather than the viewer's OS preference."
    ),
)
@click.option(
    "--no-dark-mode-css",
    is_flag=True,
    default=False,
    help=(
        "Suppress the prefers-color-scheme: dark <style> block. "
        "Useful when a host page manages its own theme and the injected "
        "media query would conflict."
    ),
)
@click.option(
    "--no-chrome-css",
    is_flag=True,
    default=False,
    help=(
        "Omit the chrome --nfm-* CSS custom-property <style> block. Colors "
        "still render (they are baked as presentation attributes); only live "
        "host recoloring is dropped. --format png applies it for you; pass "
        "it when handing the SVG to an external rasterizer, since many "
        "cannot parse var() and fail without it."
    ),
)
@click.option(
    "--bare/--no-bare",
    default=False,
    help=(
        "Omit the title and outer padding so the canvas hugs the diagram "
        "content. The attribution watermark is kept. Suitable for embedding "
        "in a host page that supplies its own frame and heading."
    ),
)
@click.option(
    "--validate",
    "validate_geometry",
    is_flag=True,
    default=False,
    help=(
        "After rendering, fail if the render-geometry guards find a defect in "
        "the produced SVG: a route drawn through a station's label or marker, "
        "or two lines collapsed onto one stroke. These read the picture as "
        "drawn, including render-time offsets and label lifts. A Tier-A "
        "layout-invariant violation stays a warning; use --strict to fail on "
        "those. SVG output only, and only for a map that keeps its manifest, "
        "which the guards read the drawn geometry through."
    ),
)
@click.option(
    "--inactive-lines",
    "inactive_lines",
    default=None,
    help=(
        "Comma-separated %%metro line: IDs to render inactive: their strokes, "
        "chevrons, and legend swatches grey out, as do the stations, labels, and "
        "terminus icons touched only by inactive lines. Unlisted lines stay "
        "full-colour. Unknown IDs error. Fully replaces any lines the map marks "
        "inactive by directive; pass an empty value to force every line active. "
        "Does not edit the .mmd."
    ),
)
@click.option(
    "--reject-output-outside-source/--no-reject-output-outside-source",
    default=False,
    help=(
        "Refuse a %%metro output: declaration that resolves outside the "
        "pipeline repository holding the .mmd (an absolute path, or a `..` "
        "escape past the repo root), or into that repository's .git/, "
        "instead of writing there. A `..` hop that stays inside the "
        "checkout is fine, so assets/metro_map.mmd may declare "
        "../docs/images/map.svg. Outside a git working tree the boundary "
        "falls back to the .mmd's own directory. Off by default, since a "
        "trusted local map may legitimately declare a path elsewhere; a "
        "caller rendering a map it did not author (a CI job rendering a "
        "fork PR's .mmd, say) should pass this. Has no effect on an "
        "explicit -o, which the caller already chose."
    ),
)
@layout_cli_options
def render(
    input_files: tuple[Path, ...],
    outputs: tuple[Path, ...],
    format_: RenderFormat | None,
    scale: float | None,
    raster_width: int | None,
    fps: float,
    duration: float | None,
    theme: str | None,
    mode: str | None,
    debug: bool,
    logo: Path | None,
    line_spread: str | None,
    legend: str | None,
    from_nextflow: bool,
    title: str | None,
    responsive: bool,
    embed_font: bool,
    text_to_paths: bool,
    svg_class_prefix: str,
    no_self_color_scheme: bool,
    no_dark_mode_css: bool,
    no_chrome_css: bool,
    bare: bool,
    validate_geometry: bool,
    inactive_lines: str | None,
    reject_output_outside_source: bool,
    **layout_opts: object,
) -> None:
    """Render Mermaid metro map definitions to SVG, PNG, or interactive HTML.

    Given more than one INPUT_FILE, all render within the same process
    (amortising interpreter/import startup across the batch) and each write
    to their own sibling <input>.<format> or, if it declares one, its own
    %%metro output: path(s); every file is attempted even if an earlier one
    fails, successful outputs are kept, and a non-zero exit is returned if
    any failed. A file with several declared outputs is one pass/fail unit:
    if any of its outputs fails the others it already wrote are kept on disk,
    but that file is reported as one FAIL and none of its paths are printed.

    Repeating -o writes one INPUT_FILE to several outputs in the same run,
    taking each output's format from its extension: -o map.svg -o map.png.
    An explicit --format overrides every extension.

    With no -o, a `%%metro output:` directive in the .mmd supplies the default
    output path(s) (comma-separate or repeat it for several), resolved as
    siblings of the source; -o overrides them entirely. Falls back to the
    sibling <input>.<format> when neither is given.

    Each declared path may carry its own render options after a `|` -
    `animate`, `mode=`, `theme=`, `scale=`, `raster_width=` - which beat the
    matching flag for that output alone, so one pass writes a static SVG, an
    animated SVG and a light/dark PNG pair with no flags at all.

    On success, a `nf-metro: v<version>` banner and the output paths are
    printed to stdout as one YAML document; human summaries and warnings go
    to stderr. Stdout stays empty on any failure, so a caller can gate on it
    without also checking the exit code.

    A rejected input, and any other failure, surfaces as a plain error
    message rather than a traceback; set NF_METRO_DEBUG=1 to re-raise the
    original exception instead.
    """
    if len(input_files) > 1 and outputs:
        raise click.UsageError("-o/--output can only be used with a single INPUT_FILE.")

    inactive_line_ids = _parse_inactive_lines(inactive_lines)

    def _job_layout_opts(overrides: Mapping[str, object]) -> dict[str, object]:
        """The run's layout options with this output's own `animate` folded in.

        `animate` is the only per-output override that reaches layout, so it is
        the only one that can split a file's outputs across two layout runs;
        mode, theme, scale and raster_width are all applied downstream of it.
        """
        if "animate" not in overrides:
            return layout_opts
        return {**layout_opts, "animate": overrides["animate"]}

    def _job(
        input_file: Path,
        out_path: Path,
        out_format: RenderFormat,
        overrides: Mapping[str, object],
        *,
        quiet: bool,
        graph: MetroGraph | None = None,
        parsed: MetroGraph | None = None,
    ) -> Callable[[], None]:
        job_scale = cast("float | None", overrides.get("scale", scale))
        job_raster_width = cast(
            "int | None", overrides.get("raster_width", raster_width)
        )
        return lambda: _render_one(
            input_file,
            out_path,
            format_=out_format,
            scale=job_scale if job_scale is not None else _default_scale(out_format),
            raster_width=job_raster_width,
            fps=fps,
            duration=duration,
            theme=cast("str | None", overrides.get("theme", theme)),
            mode=cast("str | None", overrides.get("mode", mode)),
            debug=debug,
            logo=logo,
            line_spread=line_spread,
            legend=legend,
            from_nextflow=from_nextflow,
            title=title,
            responsive=responsive,
            embed_font=embed_font,
            text_to_paths=text_to_paths,
            svg_class_prefix=svg_class_prefix,
            no_self_color_scheme=no_self_color_scheme,
            no_dark_mode_css=no_dark_mode_css,
            no_chrome_css=no_chrome_css,
            bare=bare,
            validate_geometry=validate_geometry,
            inactive_line_ids=inactive_line_ids,
            layout_opts=_job_layout_opts(overrides),
            quiet=quiet,
            graph=graph,
            parsed=parsed,
        )

    def _out_job(out: Path, overrides: dict[str, object] | None = None) -> _OutputJob:
        return (out.name, out, format_ or _format_from_output(out), overrides or {})

    def _jobs_for(source: Path, graph: MetroGraph) -> list[_OutputJob]:
        """One job per declared output, else the sibling <source>.<format>."""
        declared = _declared_outputs(
            graph, source, reject_outside_source=reject_output_outside_source
        )
        if declared:
            return [_out_job(out, overrides) for out, overrides in declared]
        fmt = format_ or "svg"
        return [
            (source.name, source.with_suffix(f".{fmt}"), cast(RenderFormat, fmt), {})
        ]

    def _parse_for_planning(source: Path, *, quiet: bool) -> MetroGraph:
        """Read and parse *source* once, for planning and for the render.

        Planning needs the map's %%metro output: declarations before it can
        say how many jobs the file becomes, and parsing is the cheap half of
        the pipeline - so the result is threaded through to the render rather
        than thrown away: one read, one parse, one set of parse warnings per
        file per run.
        """
        prefix = _error_prefix(source, quiet)
        permissive = bool(layout_opts.get("permissive"))
        with warnings.catch_warnings(record=True) as caught:
            if permissive:
                warnings.filterwarnings("always", category=PermissiveGuardWarning)
            try:
                text = source.read_text()
                return _parse_source(
                    text,
                    from_nextflow=from_nextflow,
                    title=title,
                    line_spread=line_spread,
                    layout_options=layout_opts,
                )
            except (OSError, UnicodeDecodeError, *_SOURCE_ERRORS) as e:
                _clean_error(e, prefix)
            finally:
                _report_render_warnings(
                    caught, permissive=permissive, source=source if quiet else None
                )

    def _render_source(
        source: Path,
        jobs: list[_OutputJob],
        *,
        quiet: bool,
        parsed: MetroGraph | None,
        batch: bool,
    ) -> None:
        """Render every output planned for one source file.

        Jobs sharing a (backend, animation) key share one laid-out graph - a
        video output turns the animation on (see _layout_opts_for), which is
        a different layout from the one a plain .svg beside it wants, so the
        key carries that too rather than handing one graph to both. *parsed*
        lets every job in this call skip its own read+parse, whether they
        share a layout or not.
        """
        graphs: dict[tuple[Literal["svg", "html"], bool], MetroGraph] = {}
        if len(jobs) > 1:
            permissive = bool(layout_opts.get("permissive"))
            with warnings.catch_warnings(record=True) as caught:
                if permissive:
                    warnings.filterwarnings("always", category=PermissiveGuardWarning)
                try:
                    for _, _, out_format, overrides in jobs:
                        job_opts = _job_layout_opts(overrides)
                        key = _graph_key(out_format, job_opts)
                        if key not in graphs:
                            graphs[key] = _prepare_graph_for_render(
                                source,
                                from_nextflow=from_nextflow,
                                title=title,
                                line_spread=line_spread,
                                logo=logo,
                                legend=legend,
                                layout_opts=_layout_opts_for(out_format, job_opts),
                                bare=bare,
                                svg_format=key[0],
                                error_prefix=_error_prefix(source, quiet=True),
                                parsed=parsed,
                            )
                finally:
                    _report_render_warnings(
                        caught, permissive=permissive, source=source if quiet else None
                    )

        calls = [
            (
                label,
                _job(
                    source,
                    out_path,
                    out_format,
                    overrides,
                    quiet=quiet,
                    graph=graphs.get(
                        _graph_key(out_format, _job_layout_opts(overrides))
                    ),
                    parsed=parsed,
                ),
            )
            for label, out_path, out_format, overrides in jobs
        ]
        if batch:
            _run_batch(calls)
        else:
            for _, call in calls:
                call()

    if len(input_files) == 1:
        source = input_files[0]
        if outputs:
            # Explicit -o: the outputs are already known, so nothing is
            # parsed ahead of the render.
            parsed, jobs = None, [_out_job(out) for out in outputs]
        else:
            parsed = _parse_for_planning(source, quiet=False)
            jobs = _jobs_for(source, parsed)

        if len(jobs) == 1:
            _, out_path, out_format, overrides = jobs[0]
            _job(source, out_path, out_format, overrides, quiet=False, parsed=parsed)()
        else:
            _render_source(source, jobs, quiet=True, parsed=parsed, batch=True)
        _print_render_result([out for _, out, _, _ in jobs])
        return

    # Several inputs (and therefore no -o, rejected above). Everything a file
    # needs - the read, the parse its %%metro output: declarations come from,
    # the layout, the write - happens inside its own callable, so a bad file
    # (unreadable, undecodable, or rejected by the parser) only fails that
    # one file's job under _run_batch, instead of aborting job planning for
    # every file before any of them run.
    written: list[Path] = []

    def _file_job(source: Path) -> Callable[[], None]:
        def _run() -> None:
            graph = _parse_for_planning(source, quiet=True)
            jobs = _jobs_for(source, graph)
            _render_source(source, jobs, quiet=True, parsed=graph, batch=False)
            written.extend(out for _, out, _, _ in jobs)

        return _run

    _run_batch([(f.name, _file_job(f)) for f in input_files])
    _print_render_result(written)


def _print_render_result(paths: list[Path]) -> None:
    """Print the render result (version banner + output paths) to stdout as YAML.

    JSON-quoted scalars are valid YAML, so no YAML dependency is needed. Called
    only after a successful render, so ``paths`` is never empty and a failed
    render's stdout stays empty: nothing about the result is printed until
    every job it covers has already succeeded.
    """
    click.echo(f"nf-metro: v{__version__}")
    click.echo("outputs:")
    for path in paths:
        click.echo(f"  - {json.dumps(str(path))}")


def _render_one(
    input_file: Path,
    output: Path,
    *,
    format_: RenderFormat,
    scale: float,
    raster_width: int | None,
    fps: float,
    duration: float | None,
    theme: str | None,
    mode: str | None,
    debug: bool,
    logo: Path | None,
    line_spread: str | None,
    legend: str | None,
    from_nextflow: bool,
    title: str | None,
    responsive: bool,
    embed_font: bool,
    text_to_paths: bool,
    svg_class_prefix: str,
    no_self_color_scheme: bool,
    no_dark_mode_css: bool,
    no_chrome_css: bool,
    bare: bool,
    validate_geometry: bool,
    inactive_line_ids: frozenset[str] | None,
    layout_opts: dict[str, object],
    quiet: bool,
    graph: MetroGraph | None = None,
    parsed: MetroGraph | None = None,
) -> None:
    # Applied here rather than in either caller, so `render` and `render-many`
    # both get it; `render`'s graph cache keys on the same predicate, so a
    # shared graph it hands in was prepared under these same options.
    layout_opts = _layout_opts_for(format_, layout_opts)
    permissive = bool(layout_opts.get("permissive"))

    # PermissiveGuardWarning's filter is forced to "always" so a downgraded
    # guard is never lost to the default once-per-location dedup; other
    # warnings keep that dedup.
    with warnings.catch_warnings(record=True) as caught:
        if permissive:
            warnings.filterwarnings("always", category=PermissiveGuardWarning)
        try:
            _render_one_unsafe(
                input_file,
                output,
                format_=format_,
                scale=scale,
                raster_width=raster_width,
                fps=fps,
                duration=duration,
                theme=theme,
                mode=mode,
                debug=debug,
                logo=logo,
                line_spread=line_spread,
                legend=legend,
                from_nextflow=from_nextflow,
                title=title,
                responsive=responsive,
                embed_font=embed_font,
                text_to_paths=text_to_paths,
                svg_class_prefix=svg_class_prefix,
                no_self_color_scheme=no_self_color_scheme,
                no_dark_mode_css=no_dark_mode_css,
                no_chrome_css=no_chrome_css,
                bare=bare,
                validate_geometry=validate_geometry,
                inactive_line_ids=inactive_line_ids,
                layout_opts=layout_opts,
                quiet=quiet,
                graph=graph,
                parsed=parsed,
            )
        except click.ClickException:
            raise
        except Exception as e:
            _clean_error(e, f"{_error_prefix(input_file, quiet)}unexpected error: ")
        finally:
            _report_render_warnings(
                caught,
                permissive=permissive,
                source=input_file if quiet else None,
            )


def _prepare_graph_for_render(
    input_file: Path,
    *,
    from_nextflow: bool,
    title: str | None,
    line_spread: str | None,
    logo: Path | None,
    legend: str | None,
    layout_opts: dict[str, object],
    bare: bool,
    svg_format: Literal["svg", "html"],
    error_prefix: str,
    parsed: MetroGraph | None = None,
) -> MetroGraph:
    """Lay out *input_file*, reporting a typed failure via `_clean_error`.

    *parsed* is a graph planning already parsed from this file (see
    `_parse_for_planning`); laying it out settles it in place, so each job
    needing its own layout gets its own copy, never the same object twice.
    Without *parsed*, the file is read and parsed here instead.
    """
    source: str | MetroGraph = (
        copy.deepcopy(parsed) if parsed is not None else input_file.read_text()
    )
    try:
        return prepare_graph(
            source,
            from_nextflow=from_nextflow,
            title=title,
            line_spread=line_spread,
            logo=str(logo) if logo is not None else None,
            legend=legend,
            layout_options=layout_opts,
            source_dir=str(input_file.resolve().parent),
            bare=bare,
            output_format=svg_format,
        )
    except _SOURCE_ERRORS as e:
        _clean_error(e, error_prefix)


def _render_one_unsafe(
    input_file: Path,
    output: Path,
    *,
    format_: RenderFormat,
    scale: float,
    raster_width: int | None,
    fps: float,
    duration: float | None,
    theme: str | None,
    mode: str | None,
    debug: bool,
    logo: Path | None,
    line_spread: str | None,
    legend: str | None,
    from_nextflow: bool,
    title: str | None,
    responsive: bool,
    embed_font: bool,
    text_to_paths: bool,
    svg_class_prefix: str,
    no_self_color_scheme: bool,
    no_dark_mode_css: bool,
    no_chrome_css: bool,
    bare: bool,
    validate_geometry: bool,
    inactive_line_ids: frozenset[str] | None,
    layout_opts: dict[str, object],
    quiet: bool,
    graph: MetroGraph | None = None,
    parsed: MetroGraph | None = None,
) -> None:
    error_prefix = _error_prefix(input_file, quiet)
    # PNG renders through the SVG backend and is rasterised afterward; every
    # render-plane decision below follows the SVG path regardless of format_.
    svg_format = _svg_format_for(format_)

    if graph is None:
        graph = _prepare_graph_for_render(
            input_file,
            from_nextflow=from_nextflow,
            title=title,
            line_spread=line_spread,
            logo=logo,
            legend=legend,
            layout_opts=layout_opts,
            bare=bare,
            svg_format=svg_format,
            error_prefix=error_prefix,
            parsed=parsed,
        )

    if format_ in _RASTER_FORMATS:
        # A rasteriser has no CSS cascade and no viewer colour-scheme to
        # consult, so the picture has to be fully decided here rather than
        # left to the flags a caller remembered to pass:
        #  - chrome_css off, or the var() chrome colours reach resvg unresolved
        #  - a concrete baked mode, or light-dark() has nothing to resolve to
        #  - embedded Inter, so the layout is measured against the same face
        #    svg_to_png hands the rasteriser
        no_chrome_css = True
        embed_font = not text_to_paths
        mode = (mode or graph.mode).strip().lower() or DEFAULT_MODE

    theme_obj = resolve_theme(theme, graph, mode=mode)

    if format_ == "html":
        # The interactive page supplies its own responsive frame, chrome, and
        # per-map class scoping, so the SVG-only sizing/namespacing flags have
        # nothing to act on. Font portability and the dark-mode block do reach
        # the inlined SVG, so they are threaded through.
        ignored = [
            name
            for name, enabled in (
                ("--responsive", responsive),
                ("--bare", bare),
                ("--svg-class-prefix", bool(svg_class_prefix)),
            )
            if enabled
        ]
        if ignored:
            click.echo(
                f"Note: {', '.join(ignored)} only affect --format svg and are "
                "ignored for --format html (the interactive page is already "
                "responsive and scopes each map independently).",
                err=True,
            )

    # Tier-A layout-invariant violations on the settled geometry surface here
    # under --strict (LayoutInvariantError is a PhaseInvariantError); without
    # --strict they are warnings, reported by the caller's capture.
    try:
        rendered = render_graph_result(
            graph,
            theme_obj,
            RenderConfig(
                output_format=svg_format,
                debug=debug,
                responsive=responsive,
                embed_font=embed_font,
                text_to_paths=text_to_paths,
                svg_class_prefix=svg_class_prefix,
                inject_dark_mode_css=not no_dark_mode_css,
                chrome_css=not no_chrome_css,
                self_color_scheme=not no_self_color_scheme,
                baked_mode=(mode or graph.mode).strip() or None,
                bare=bare,
                embed_basename=output.name,
                inactive_line_ids=inactive_line_ids,
                animation_frame_slot=format_ in VIDEO_FORMATS,
            ),
        )
        content = rendered.content
    except (ValueError, FoldThresholdError, PhaseInvariantError) as e:
        _clean_error(e, error_prefix)

    if validate_geometry:
        if format_ == "html":
            raise click.ClickException("--validate applies to --format svg only.")
        if not graph.embed_manifest:
            raise click.ClickException(
                "--validate reads the drawn SVG through its embedded manifest, "
                "which this map turns off with %%metro manifest: false."
            )
        findings = validate_render(content, plan=rendered.plan)
        if findings:
            detail = "\n".join(f"  - {f.message}" for f in findings)
            raise click.ClickException(
                f"render-geometry validation found {len(findings)} "
                f"defect(s) in the drawn SVG:\n{detail}"
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    detail = ""
    if format_ in VIDEO_FORMATS:
        from nf_metro.render.video import write_animation

        try:
            export = write_animation(
                content,
                rendered.plan,
                output,
                cast(VideoFormat, format_),
                fps=fps,
                duration=duration,
                scale=scale,
                width=raster_width,
                notify=None if quiet else _video_notice,
                progress=None if quiet else _video_progress,
            )
        except NotAnimatedError as e:
            raise click.ClickException(f"{error_prefix}{e}") from None
        except Exception as e:
            _clean_error(e, f"{error_prefix}{format_} export failed: ")
        detail = (
            f", {export.frames} frames over {export.duration:.1f}s "
            f"at {export.fps:.1f}fps"
        )
    elif format_ == "png":
        from nf_metro.render.raster import svg_to_png

        output.write_bytes(svg_to_png(content, scale=scale, width=raster_width))
    else:
        output.write_text(content if content.endswith("\n") else content + "\n")
    if not quiet:
        # Human summary to stderr; the machine-readable result goes to stdout.
        click.echo(
            f"Rendered {len(graph.stations)} stations, "
            f"{len(graph.edges)} edges, "
            f"{len(graph.lines)} lines -> {output}{detail}",
            err=True,
        )


def _video_notice(message: str) -> None:
    """Print what a large export is about to cost, before it starts."""
    click.echo(f"Note: {message}", err=True)


def _video_progress(frames: Iterable[bytes], count: int) -> Iterator[bytes]:
    """Draw a progress bar over the frames as they rasterise.

    An export runs for minutes, and click hides the bar when stderr is not a
    terminal, so a piped or logged run stays as quiet as every other render.
    """
    with click.progressbar(
        frames, length=count, label="Rendering frames", file=sys.stderr
    ) as tracked:
        yield from tracked


@cli.command(name="render-many")
@click.argument("manifest_file", type=click.Path(exists=True, path_type=Path))
def render_many(manifest_file: Path) -> None:
    """Render multiple metro maps from a JSON manifest in one process.

    MANIFEST_FILE is a JSON array of render jobs.  Each job is an object
    with ``input`` and ``output`` (required) plus any subset of the options
    accepted by ``nf-metro render``, expressed as JSON keys:

    \b
      input                 Path to the source .mmd file (required).
      output                Path for the output file (required).
      format                "svg" (default), "png", "html", or a looping
                            video: "gif", "webp", "mp4", "webm".
      scale                 Raster formats only: pixel multiplier (default:
                            2.0 for png, 1.0 for a video).
      raster_width          Raster formats only: output width in pixels;
                            overrides scale.
      fps                   Video only: frames per second (default: 12).
      duration              Video only: loop length in seconds; defaults to
                            the map's own animation cycle.
      theme                 Theme name (nfcore, light, seqera, …).
      mode                  "light" or "dark" — bakes a concrete palette.
      debug                 Show debug overlay (default: false).
      logo                  Logo image path (overrides %%metro logo:).
      line_spread           "bundle", "centered", or "rails".
      legend                Legend position keyword or coordinate.
      from_nextflow         Convert from Nextflow DAG first (default: false).
      title                 Pipeline title override.
      responsive            Emit viewBox-only SVG (default: false).
      embed_font            Inline Inter @font-face subset (default: false).
      text_to_paths         Convert text to vector paths (default: false).
      svg_class_prefix      Prefix for SVG presentation classes.
      no_self_color_scheme  Omit color-scheme on root <svg> (default: false).
      no_dark_mode_css      Suppress prefers-color-scheme block (default: false).
      no_chrome_css         Omit chrome CSS custom-properties (default: false).
      bare                  Omit title and outer padding (default: false).
      validate              Run render-geometry guards (default: false).
      inactive_lines        Line IDs to render inactive: a comma-separated
                            string or JSON list.  Omit the key to use the map's
                            own inactive-by-directive lines; give [] to force
                            every line active.
      layout_options        Object of layout overrides, e.g.
                            {"manifest": false, "x_spacing": 60}.

    All maps are rendered within the same Python process, amortising
    interpreter and import startup across the whole corpus.  Output
    directories are created as needed.  On partial failure, successful
    outputs are kept and a non-zero exit is returned.
    """
    import json

    try:
        jobs: list[object] = json.loads(manifest_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise click.ClickException(f"cannot read manifest: {e}")

    if not isinstance(jobs, list):
        raise click.ClickException("manifest must be a JSON array")

    if not jobs:
        click.echo("render-many: empty manifest, nothing to do")
        return

    def _label(job: object, idx: int) -> str:
        if isinstance(job, dict) and isinstance(job.get("input"), str):
            return Path(cast(str, job["input"])).name
        return f"job {idx}"

    def _job_call(job: object) -> Callable[[], None]:
        def _run() -> None:
            if not isinstance(job, dict):
                raise ValueError("not an object")

            raw_input = job.get("input")
            raw_output = job.get("output")
            if not isinstance(raw_input, str) or not isinstance(raw_output, str):
                raise ValueError("missing or non-string 'input'/'output'")

            def _str_or_none(key: str) -> str | None:
                v = job.get(key)
                return str(v) if v else None

            job_format = cast(
                RenderFormat,
                _FORMAT_TYPE.convert(job.get("format", "svg"), None, None),
            )
            logo_raw = job.get("logo")
            lo_raw = job.get("layout_options")

            _render_one(
                Path(raw_input),
                Path(raw_output),
                format_=job_format,
                scale=cast(
                    float,
                    _convert_manifest_number(
                        _SCALE_TYPE,
                        job.get("scale", _default_scale(job_format)),
                        "scale",
                    ),
                ),
                fps=cast(
                    float,
                    _convert_manifest_number(
                        _FPS_TYPE, job.get("fps", DEFAULT_FPS), "fps"
                    ),
                ),
                duration=(
                    cast(
                        float,
                        _convert_manifest_number(
                            _DURATION_TYPE, job["duration"], "duration"
                        ),
                    )
                    if "duration" in job
                    else None
                ),
                raster_width=(
                    cast(
                        int,
                        _convert_manifest_number(
                            _RASTER_WIDTH_TYPE, job["raster_width"], "raster_width"
                        ),
                    )
                    if "raster_width" in job
                    else None
                ),
                theme=_str_or_none("theme"),
                mode=_str_or_none("mode"),
                debug=bool(job.get("debug", False)),
                logo=Path(str(logo_raw)) if logo_raw else None,
                line_spread=_str_or_none("line_spread"),
                legend=_str_or_none("legend"),
                from_nextflow=bool(job.get("from_nextflow", False)),
                title=_str_or_none("title"),
                responsive=bool(job.get("responsive", False)),
                embed_font=bool(job.get("embed_font", False)),
                text_to_paths=bool(job.get("text_to_paths", False)),
                svg_class_prefix=str(job.get("svg_class_prefix", "")),
                no_self_color_scheme=bool(job.get("no_self_color_scheme", False)),
                no_dark_mode_css=bool(job.get("no_dark_mode_css", False)),
                no_chrome_css=bool(job.get("no_chrome_css", False)),
                bare=bool(job.get("bare", False)),
                validate_geometry=bool(job.get("validate", False)),
                inactive_line_ids=_parse_inactive_lines(job.get("inactive_lines")),
                layout_opts=dict(lo_raw) if isinstance(lo_raw, dict) else {},
                quiet=True,
            )

        return _run

    _run_batch([(_label(job, idx), _job_call(job)) for idx, job in enumerate(jobs, 1)])


@cli.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output .mmd file path. Defaults to stdout.",
)
@click.option(
    "--title",
    type=str,
    default=None,
    help="Pipeline title for the converted output",
)
def convert(
    input_file: Path,
    output: Path | None,
    title: str | None,
) -> None:
    """Convert a Nextflow -with-dag mermaid file to nf-metro .mmd format.

    Takes a .mmd file produced by `nextflow -with-dag file.mmd` and converts
    it to nf-metro format. The output can then be rendered with `nf-metro render`
    or hand-tuned before rendering.
    """
    from nf_metro.convert import (
        FeedbackEdgesDroppedWarning,
        convert_nextflow_dag,
        is_nextflow_dag,
    )

    text = input_file.read_text()

    if not is_nextflow_dag(text):
        click.echo(
            "Warning: input does not look like a Nextflow DAG "
            "(expected 'flowchart TB' header)",
            err=True,
        )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = convert_nextflow_dag(text, title=title or "")

    if caught:
        _echo_block("Warnings", [str(w.message) for w in caught])

    dropped = sum(
        len(w.message.connections)
        for w in caught
        if isinstance(w.message, FeedbackEdgesDroppedWarning)
    )

    if output is None:
        click.echo(result, nl=False)
    else:
        output.write_text(result if result.endswith("\n") else result + "\n")
        # Count sections and processes in the output
        sections = result.count("subgraph ")
        processes = result.count("([")
        summary = f"Converted {processes} processes, {sections} sections"
        if dropped:
            plural = "" if dropped == 1 else "s"
            summary += f", {dropped} feedback connection{plural} removed"
        click.echo(f"{summary} -> {output}")


@cli.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-layout",
    is_flag=True,
    help="Also run the layout engine with its full invariant suite, reporting "
    "any layout failure as an error instead of a traceback.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Treat warnings (e.g. a non-LR primary direction) as errors.",
)
def validate(input_file: Path, with_layout: bool, strict: bool) -> None:
    """Validate a Mermaid metro map definition.

    The bare command runs graph-semantic checks: every edge references a
    defined line, every section points at stations that exist, and the graph
    is acyclic.  ``--with-layout`` additionally runs the layout engine with
    its full invariant suite, reporting a layout failure as a clean error.
    ``--strict`` escalates warnings to a non-zero exit.
    """
    text = input_file.read_text()

    graph, parse_warnings = _parse_reporting_warnings(
        input_file, text, label="Validation warnings"
    )

    issues = [ValidationIssue(WARNING, message) for message in parse_warnings]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        issues.extend(validate_graph(graph))

        if with_layout:
            try:
                compute_layout(graph, validate=True)
            except (
                CyclicGraphError,
                BackwardFlowError,
                FoldThresholdError,
                MixedEntryDirectionError,
                PhaseInvariantError,
            ) as e:
                issues.append(ValidationIssue(ERROR, str(e)))

    issues.extend(ValidationIssue(WARNING, str(w.message)) for w in caught)

    errors = [i for i in issues if i.severity == ERROR]
    warns = [i for i in issues if i.severity == WARNING]

    if warns:
        _echo_issues("Validation warnings", warns, input_file)
    if errors:
        _echo_issues("Validation errors", errors, input_file)
        raise SystemExit(1)
    if strict and warns:
        click.echo(
            f"Failed: {len(warns)} warning(s) treated as errors under --strict.",
            err=True,
        )
        raise SystemExit(1)

    click.echo(
        f"Valid: {len(graph.stations)} stations, "
        f"{len(graph.edges)} edges, "
        f"{len(graph.lines)} lines, "
        f"{len(graph.sections)} sections"
    )


@cli.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the full introspection as JSON."
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Add the section dependency graph, per-line routes, inferred "
    "auto-layout defaults, and synthetic ports/junctions to the text output.",
)
def info(input_file: Path, as_json: bool, verbose: bool) -> None:
    """Show information about a Mermaid metro map definition.

    The default output is a stable human summary. ``--verbose`` adds the
    richer introspection (what nf-metro derived and inferred); ``--json``
    emits the complete structure for scripting.
    """
    text = input_file.read_text()
    graph, messages = _parse_reporting_warnings(input_file, text)

    # The JSON and the verbose text report both carry the captured warnings;
    # the default summary does not, so they surface on stderr instead.
    if messages and not (as_json or verbose):
        _echo_block("Warnings", messages)

    report = build_info(graph, messages)
    if as_json:
        click.echo(format_info_json(report))
    else:
        click.echo(format_info_text(report, verbose=verbose))


@cli.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the full explanation as JSON."
)
@click.option(
    "--section",
    "section_filter",
    default=None,
    metavar="SECTION_ID",
    help="Restrict output to decisions involving this section.",
)
@click.option(
    "--station",
    "station_filter",
    default=None,
    metavar="STATION_ID",
    help="Restrict output to decisions involving this station.",
)
def explain(
    input_file: Path,
    as_json: bool,
    section_filter: str | None,
    station_filter: str | None,
) -> None:
    """Explain WHY nf-metro made each layout decision.

    Surfaces the rule that fired for each inferred decision (section direction,
    port sides, fold/row layout) and each synthetic element the engine inserted
    (fan-out junctions, bypass-V stations).

    Pairs with ``nf-metro info``, which shows WHAT was built; this command
    shows WHY each non-trivial choice was made.

    Use ``--section SECTION_ID`` or ``--station STATION_ID`` to focus the
    output on decisions involving a specific element.
    """
    text = input_file.read_text()
    graph, messages = _parse_reporting_warnings(input_file, text)

    report = build_explain(
        graph,
        messages,
        section_filter=section_filter,
        station_filter=station_filter,
    )
    if as_json:
        click.echo(format_explain_json(report))
    else:
        click.echo(format_explain_text(report))


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option("--port", type=int, default=8080, help="Port to listen on.")
@click.option(
    "--host",
    default="127.0.0.1",
    help="Interface to bind. Default 127.0.0.1 (local only); "
    "use 0.0.0.0 to accept connections from other hosts.",
)
@click.option(
    "--theme",
    type=click.Choice(sorted(STYLE_NAMES)),
    default=None,
    help="Visual theme (default: from the %%metro style: directive, else nfcore). "
    "Applies to a .mmd input; an SVG input is served as drawn.",
)
@click.option(
    "--overlay",
    type=click.Choice(OVERLAY_STYLES),
    default=DEFAULT_OVERLAY,
    show_default=True,
    help="Status-overlay style shown until a viewer picks another in the page.",
)
@click.option(
    "--token",
    default=None,
    help="If set, /events POSTs must supply ?token=... or an X-Metro-Token header.",
)
@click.option(
    "--open", "open_browser", is_flag=True, help="Open the live page in a browser."
)
@click.option(
    "--shutdown-after-complete",
    is_flag=True,
    help="Stop the server shortly after the run's completed/error event "
    "(or after the launched command exits).",
)
@click.option(
    "--shutdown-grace",
    type=float,
    default=10.0,
    help="Seconds to keep the map up after the run finishes "
    "(with --shutdown-after-complete).",
)
@click.argument("launch_cmd", nargs=-1, type=click.UNPROCESSED)
def serve(
    input_file: Path,
    port: int,
    host: str,
    theme: str | None,
    overlay: str,
    token: str | None,
    open_browser: bool,
    shutdown_after_complete: bool,
    shutdown_grace: float,
    launch_cmd: tuple[str, ...],
) -> None:
    """Serve a live-progress view of a metro map.

    Renders the map once and serves it at http://HOST:PORT/. Point a Nextflow
    run's weblog at the events endpoint to light up stations as tasks run:

        nextflow run ... -with-weblog http://HOST:PORT/events

    Or launch the run in one step (the weblog is wired up automatically) and
    have the server open a browser and stop itself when the run finishes:

        nf-metro serve map.mmd --open --shutdown-after-complete -- \\
            nextflow run my/pipeline -profile docker

    Stations are tied to processes with `%%metro process:` directives in the
    map; only mapped stations change state. Use `nf-metro check-mapping` to
    verify the mapping covers the pipeline.
    """
    from nf_metro.live.server import MapModel, run_lifecycle, serve_model
    from nf_metro.live.server import serve as serve_map

    if input_file.suffix.lower() == ".svg":
        try:
            model = MapModel.from_svg(input_file.read_text())
        except ValueError as e:
            raise click.ClickException(str(e))
        mapped = sorted(model.mapping)
        if not mapped:
            click.echo(
                "Warning: SVG manifest has no process patterns; "
                "no station will update.",
                err=True,
            )
        httpd = serve_model(model, host=host, port=port, token=token, overlay=overlay)
    else:
        try:
            graph = parse_metro_mermaid(input_file.read_text())
            compute_layout(graph)
        except (ValueError, PhaseInvariantError) as e:
            raise click.ClickException(str(e))

        theme_obj = resolve_theme(theme, graph)
        mapped = sorted(graph.process_mapping)
        if not mapped:
            click.echo(
                "Warning: no %%metro process: directives; no station will update.",
                err=True,
            )
        httpd = serve_map(
            graph, theme_obj, host=host, port=port, token=token, overlay=overlay
        )
    if host == "0.0.0.0":  # noqa: S104 - explicit opt-in, warned
        click.echo(
            "Binding 0.0.0.0: reachable from other hosts; "
            "use --token to restrict /events.",
            err=True,
        )
    # Local subprocesses post to a concrete loopback address, not 0.0.0.0.
    run_host = "127.0.0.1" if host == "0.0.0.0" else host
    page_url = f"http://{run_host}:{port}/"
    events_url = f"{page_url}events"
    if token:
        events_url += f"?token={token}"

    click.echo("nf-metro live progress")
    click.echo(f"Mapped stations: {', '.join(mapped) or '(none)'}")
    click.echo("")
    click.echo(f"    ▶ Open: {page_url}")
    click.echo("")
    if not launch_cmd:
        click.echo(f"Send Nextflow weblog events to {events_url}")

    run_lifecycle(
        httpd,
        page_url,
        events_url,
        launch_cmd=launch_cmd,
        shutdown_after_complete=shutdown_after_complete,
        grace=shutdown_grace,
        open_browser=open_browser,
        echo=click.echo,
    )


@cli.command(name="serve-multi")
@click.option("--port", type=int, default=8080, help="Port to listen on.")
@click.option(
    "--host",
    default="127.0.0.1",
    help="Interface to bind. Default 127.0.0.1 (local only); "
    "use 0.0.0.0 to accept connections from other hosts.",
)
@click.option(
    "--theme",
    type=click.Choice(sorted(STYLE_NAMES)),
    default="nfcore",
    show_default=True,
    help="Visual theme applied to every map registered with this server.",
)
@click.option(
    "--overlay",
    type=click.Choice(OVERLAY_STYLES),
    default=DEFAULT_OVERLAY,
    show_default=True,
    help="Status-overlay style shown until a viewer picks another in the page.",
)
@click.option(
    "--token",
    default=None,
    help="If set, POSTs to /maps and /r/*/events must supply ?token=... "
    "or an X-Metro-Token header.",
)
def serve_multi_cmd(
    port: int, host: str, theme: str, overlay: str, token: str | None
) -> None:
    """Run a persistent live server many pipelines can report into.

    Unlike `serve` (one map), this starts with no map. A pipeline registers its
    map by POSTing the .mmd to /maps and then sends weblog events to the run's
    /r/<id>/events endpoint:

        curl -s --data-binary @map.mmd "http://HOST:PORT/maps?name=myrun"
        # -> {"id": "...", "view": "/r/<id>/", "events": "/r/<id>/events"}

    The index at http://HOST:PORT/ lists every run with a live status. The
    nf-metro Nextflow plugin's `metro.server` mode does the register-and-emit
    automatically.
    """
    from nf_metro.live.server import serve_multi

    if host == "0.0.0.0":  # noqa: S104 - explicit opt-in, warned
        click.echo(
            "Binding 0.0.0.0: reachable from other hosts; "
            "use --token to restrict POSTs.",
            err=True,
        )
    httpd = serve_multi(
        THEMES[resolve_style(theme)],
        host=host,
        port=port,
        token=token,
        overlay=overlay,
    )
    display_host = "localhost" if host == "127.0.0.1" else host
    click.echo("nf-metro live progress - persistent server")
    click.echo("")
    click.echo(f"    ▶ Runs index: http://{display_host}:{port}/")
    click.echo("")
    click.echo(f"Pipelines register maps at http://{display_host}:{port}/maps")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nStopping.")
    finally:
        httpd.server_close()


@cli.command(name="check-mapping")
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--dag",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Nextflow `-with-dag` mermaid file; process names are read from its "
    "stadium nodes.",
)
@click.option(
    "--processes",
    "processes_file",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Newline-delimited process names (e.g. captured from a run). "
    "Authoritative alternative to --dag.",
)
@click.option(
    "--ignore",
    multiple=True,
    help="Regex for processes deliberately left unmapped (plumbing). Repeatable.",
)
def check_mapping_cmd(
    input_file: Path,
    dag: Path | None,
    processes_file: Path | None,
    ignore: tuple[str, ...],
) -> None:
    """Check a map's `%%metro process:` mapping against the processes.

    Reports processes the map can't show (drift) and station patterns that
    match nothing (stale), exiting non-zero if any are found so CI can gate on
    map fidelity. Supply the pipeline's processes via --dag (a `nextflow
    -with-dag` export) or --processes (a newline-delimited list).
    """
    from nf_metro.live.mapping import check_mapping, process_names_from_dag

    if not dag and not processes_file:
        raise click.ClickException("provide --dag or --processes")

    graph = parse_metro_mermaid(input_file.read_text())
    station_ids = [s.id for s in graph.stations.values() if not s.is_port]
    if dag is not None:
        process_names = process_names_from_dag(dag.read_text())
    else:
        assert processes_file is not None
        process_names = [
            line.strip()
            for line in processes_file.read_text().splitlines()
            if line.strip()
        ]

    report = check_mapping(
        graph.process_mapping, station_ids, process_names, ignore=list(ignore)
    )

    if report.unmapped_processes:
        click.echo(
            f"Processes with no station (invisible): {len(report.unmapped_processes)}",
            err=True,
        )
        for name in report.unmapped_processes:
            click.echo(f"  - {name}", err=True)
    if report.dead_patterns:
        click.echo(
            f"Station patterns matching no process (stale): "
            f"{len(report.dead_patterns)}",
            err=True,
        )
        for sid, pat in report.dead_patterns:
            click.echo(f"  - {sid}: {pat}", err=True)
    if report.ambiguous_processes:
        click.echo(
            f"Processes matching more than one station (duplicates progress): "
            f"{len(report.ambiguous_processes)}",
            err=True,
        )
        for name, sids in report.ambiguous_processes.items():
            click.echo(f"  - {name}: {', '.join(sids)}", err=True)
    if report.unmapped_stations:
        click.echo(
            f"Stations with no mapping (never light up): "
            f"{', '.join(report.unmapped_stations)}"
        )

    if report.ok:
        click.echo(
            f"Mapping OK: {len(report.matched)}/{len(process_names)} "
            "processes map to a station."
        )
    else:
        raise SystemExit(1)


@cli.command(name="validate-svg")
@click.argument("svg_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--geometry",
    is_flag=True,
    default=False,
    help=(
        "Also run the artifact-only render-geometry guards on the drawn ink "
        "(label strikes and non-consumer marker crossings), not just the "
        "manifest schema. The offset-collapse check needs the engine's assigned "
        "offsets and runs only via 'render --validate'."
    ),
)
def validate_svg_cmd(svg_file: Path, geometry: bool) -> None:
    """Validate an SVG's embedded manifest against the manifest JSON Schema.

    With ``--geometry`` it additionally runs the artifact-only render-geometry
    guards on the drawn ink and reports any defect.
    """
    from nf_metro.render import manifest_schema, read_manifest

    try:
        import jsonschema
    except ImportError:
        raise click.ClickException(
            'validate-svg needs jsonschema: pip install "nf-metro[validate]"'
        )

    svg = svg_file.read_text()
    manifest = read_manifest(svg)
    if manifest is None:
        click.echo(f"{svg_file}: no diagram manifest embedded", err=True)
        raise SystemExit(1)

    try:
        jsonschema.validate(manifest, manifest_schema())
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "<root>"
        click.echo(f"{svg_file}: manifest does not conform to the schema", err=True)
        click.echo(f"  at {where}: {e.message}", err=True)
        raise SystemExit(1)

    if geometry:
        findings = validate_render(svg)
        if findings:
            click.echo(
                f"{svg_file}: {len(findings)} render-geometry defect(s)", err=True
            )
            for finding in findings:
                click.echo(f"  - {finding.message}", err=True)
            raise SystemExit(1)

    click.echo(
        f"Valid: {len(manifest.get('nodes', []))} nodes, "
        f"schema version {manifest.get('version')}"
        + (", render geometry clean" if geometry else "")
    )


@cli.command(name="embed-script")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Write to a file instead of stdout.",
)
def embed_script_cmd(output: Path | None) -> None:
    """Output the nf-metro embed driver JS.

    Prints the ``attachMetroMap()`` driver to stdout (or writes to ``-o``).
    Load it on a host page alongside an nf-metro SVG to get the documented
    interactive API.  See ``docs/embed.md`` for usage.
    """
    from nf_metro.render.driver import get_driver_js

    js = get_driver_js()
    if output is not None:
        output.write_text(js)
        click.echo(f"Written to {output}")
    else:
        click.echo(js, nl=False)
