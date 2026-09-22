"""CLI for nf-metro."""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import os
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Literal, NamedTuple, NoReturn, TypeVar, cast, get_args

import rich_click as click
from rich.markup import escape

from nf_metro import __version__
from nf_metro.api import (
    RenderConfig,
    _parse_source,
    prepare_graph,
    render_graph_result,
    resolve_theme,
)
from nf_metro.console import (
    console,
    echo_yaml,
    panel,
    print_banner,
    progress_bar,
)
from nf_metro.explain import build_explain, format_explain_json, format_explain_text
from nf_metro.introspect import (
    build_info,
    format_info_json,
    format_info_text,
    to_yaml,
)
from nf_metro.layout import (
    BackwardFlowError,
    FoldThresholdError,
    MixedEntryDirectionError,
    PhaseInvariantError,
    compute_layout,
)
from nf_metro.layout.constants import OFFSET_STEP, SECTION_X_GAP, SECTION_Y_GAP
from nf_metro.live.server import DEFAULT_OVERLAY, OVERLAY_STYLES
from nf_metro.options import (
    DEFAULT_FOLD_THRESHOLD,
    LAYOUT_OPTIONS,
    LayoutOption,
)
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
from nf_metro.render.constants import LOGO_GAP
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
# rich-click forces a terminal (bold ANSI, fixed-width panels) whenever it sees
# FORCE_COLOR, PY_COLORS or GITHUB_ACTIONS, and never checks NO_COLOR itself.
@click.rich_config(
    {
        "theme": "forest-nu",
        "force_terminal": False if "NO_COLOR" in os.environ else None,
        "options_table_column_types": ["required", "opt_long", "opt_short", "help"],
        "options_table_help_sections": [
            "help",
            "metavar",
            "deprecated",
            "envvar",
            "default",
            "required",
        ],
    }
)
@click.command_panel("Render", commands=["render", "render-many", "convert"])
@click.command_panel(
    "Inspect", commands=["validate", "validate-svg", "info", "explain"]
)
@click.command_panel(
    "Live progress", commands=["serve", "serve-multi", "check-mapping"]
)
@click.command_panel("Embed", commands=["embed-script"])
def cli() -> None:
    """nf-metro: Generate metro-map-style SVG diagrams from Mermaid definitions."""
    print_banner()


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
    next ``git`` call in the same job - so a declared path whose *resolved*
    destination lies inside a ``.git`` directory is refused, which covers a
    nested ``assets/.git/`` the same as the checkout's own. A ``..`` hop that
    only lexically passes through ``.git`` before cancelling back out of it
    (``assets/.git/../docs/map.svg``) is not refused: nothing is ever written
    there. Outside a repository there is no tree to scope to, so the boundary
    falls back to the map's own directory.
    """
    base = source.parent.resolve()
    # _project_root walks the filesystem up to find .git, and candidate.resolve()
    # below stats every path component - both skipped unless the flag that
    # consumes them is actually set.
    root = _project_root(base) if reject_outside_source else None
    boundary = root or base
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
        if reject_outside_source:
            target = candidate.resolve()
            escapes = not target.is_relative_to(boundary)
            reaches_git = not escapes and ".git" in target.relative_to(boundary).parts
            if escapes or reaches_git:
                raise click.ClickException(
                    f"{source}: %%metro output {p!r} resolves outside the "
                    f"{scope} (or into a .git/); declared paths must stay within "
                    f"{boundary} and out of .git/"
                )
        resolved.append((candidate, overrides))
    return resolved


class _OutputJob(NamedTuple):
    """One planned write, with the per-output overrides it carries."""

    label: str
    path: Path
    format: RenderFormat
    overrides: dict[str, object]


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


class _DescribedDefault(click.RichOption):
    """Option whose ``--help`` default is described in words, not a value.

    rich-click parenthesises a string ``show_default`` - "[default: (auto)]" -
    but prints a real default bare. Returning the description as the help-time
    default takes that bare path. ``call=True``, the path that actually fills
    the option's value, is left alone.
    """

    def __init__(
        self,
        *args: Any,  # noqa: ANN401
        default_text: str = "",
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.default_text = default_text
        if default_text:
            kwargs["show_default"] = True
        super().__init__(*args, **kwargs)

    def get_default(self, ctx: click.Context, call: bool = True) -> Any:  # noqa: ANN401
        if not call and self.default_text:
            return self.default_text
        return super().get_default(ctx, call=call)


def _trim_number(value: float) -> str:
    """Render *value* without a trailing ``.0``, so --help reads "50" not "50.0"."""
    return f"{value:g}"


#: What --help shows as the default for a layout option whose MetroGraph
#: field is None because the value is resolved later. The resolving constants
#: live below this module in the import graph, hence the lookup here.
_LAYOUT_DEFAULT_TEXT: dict[str, str] = {
    "x_spacing": "auto",
    "y_spacing": "auto",
    "section_x_gap": _trim_number(SECTION_X_GAP),
    "section_y_gap": _trim_number(SECTION_Y_GAP),
    "track_gap": _trim_number(OFFSET_STEP),
    "fold_threshold": str(DEFAULT_FOLD_THRESHOLD),
    "label_angle": "from theme",
    "legend_logo_gap": _trim_number(LOGO_GAP),
    "width": "auto from content",
    "height": "auto from content",
}

_GRAPH_FIELD_DEFAULTS: dict[str, Any] = {
    f.name: f.default for f in dataclasses.fields(MetroGraph)
}


def _layout_default(opt: LayoutOption) -> str:
    """What ``--help`` should print as *opt*'s default.

    A registry option's CLI flag always defaults to ``None`` so an omitted
    flag leaves the directive value alone, which would make click show every
    default as "None". The value a user actually gets is the graph field's
    own default, or - where that is ``None`` too - whatever resolves it
    later, named in :data:`_LAYOUT_DEFAULT_TEXT`.
    """
    if opt.name in _LAYOUT_DEFAULT_TEXT:
        return _LAYOUT_DEFAULT_TEXT[opt.name]
    value = _GRAPH_FIELD_DEFAULTS.get(opt.target_attr)
    # An opt-in flag reads as off already; "[default: False]" is just noise.
    if value is None or value == "" or value is False:
        return ""
    return _trim_number(value) if isinstance(value, float) else str(value)


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
            cls=_DescribedDefault,
            default=None,
            default_text=_layout_default(opt),
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
        cls=_DescribedDefault,
        type=ctype,
        default=None,
        default_text=_layout_default(opt),
        help=opt.help,
        metavar=metavar,
        hidden=opt.hidden,
    )


def layout_cli_options(f: _F) -> _F:
    """Attach a CLI flag for every registry option, in declaration order."""
    for opt in reversed(LAYOUT_OPTIONS):
        f = _layout_cli_option(opt)(f)
    return f


def _echo_block(label: str, entries: Iterable[str], *, style: str = "yellow") -> None:
    """Print a titled panel of *entries* to stderr.

    An entry spanning several lines keeps its continuation lines indented
    under its own bullet, so a guard message carrying per-defect detail reads
    as one item.
    """
    lines: list[str] = []
    for entry in entries:
        head, *rest = entry.split("\n")
        lines.append(f"[{style}]-[/] {escape(head)}")
        lines.extend(f"  [dim]{escape(line.strip())}[/]" for line in rest)
    if not lines:
        return
    console.print(panel("\n".join(lines), title=label, style=style))


def _echo_issues(
    label: str, issues: Iterable[ValidationIssue], path: Path | str
) -> None:
    """Print a block of validation issues, each formatted against *path*."""
    style = "red" if "error" in label.lower() else "yellow"
    _echo_block(label, (issue.format(path) for issue in issues), style=style)


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
    # A panel truncates a title too long for its width, so the source file
    # and the guard preamble go in the body where they can wrap.
    lead = [str(source)] if source is not None else []
    if other_warnings:
        _echo_block("Warnings", lead + [str(w.message) for w in other_warnings])
    if guard_warnings:
        flag = "--permissive: " if permissive else ""
        preamble = (
            f"{flag}{len(guard_warnings)} guard(s) downgraded to warnings; "
            "the rendered geometry may be defective at these points"
        )
        _echo_block(
            "Guard downgrades",
            [*lead, preamble, *(str(w.message) for w in guard_warnings)],
        )


def _run_batch(items: list[tuple[str, Callable[[], None]]]) -> None:
    """Run every item's callable, printing a ✓/✗ line per item.

    Every item runs regardless of whether an earlier one raised, so
    successful outputs are kept. Catches any exception type, since a
    callable's failure mode (parse error, I/O error, an unanticipated bug)
    isn't known to this generic runner. Raises a single ``ClickException``
    summarising the failure count if anything failed (the per-item FAIL
    lines, already on stderr, carry the detail).
    """
    total = len(items)
    failure_count = 0
    for label, job in items:
        try:
            job()
        except Exception as e:
            if _debug_reraise():
                raise
            failure_count += 1
            console.print(f"[red]✗[/] {escape(label)} [dim]— {escape(str(e))}[/]")
        else:
            console.print(f"[green]✓[/] {escape(label)}")
    if failure_count:
        raise click.ClickException(
            f"{failure_count}/{total} render(s) failed; see the errors above"
        )
    console.print(f"[green]✓[/] [bold]{total}[/] file(s) rendered")


@cli.command()
@click.option_panel(
    "Output and source",
    options=[
        "--output",
        "--format",
        "--from-nextflow",
        "--debug",
        "--reject-output-outside-source",
    ],
)
@click.option_panel(
    "Theme and branding",
    options=["--theme", "--mode", "--logo", "--title", "--caption"],
)
@click.option_panel(
    "Legend and logo",
    options=["--legend", "--logo-scale", "--legend-min-height", "--legend-logo-gap"],
)
@click.option_panel(
    "Layout",
    options=[
        "--line-spread",
        "--x-spacing",
        "--y-spacing",
        "--section-x-gap",
        "--section-y-gap",
        "--track-gap",
        "--fold-threshold",
        "--diamond-style",
        "--line-order",
        "--row-align",
        "--center-ports",
        "--compact-offsets",
        "--label-angle",
        "--font-scale",
        "--stroke-scale",
        "--width",
        "--height",
    ],
)
@click.option_panel("Line styling", options=["--inactive-lines", "--directional"])
@click.option_panel(
    "Animation and raster output",
    options=["--animate", "--fps", "--duration", "--scale", "--raster-width"],
)
@click.option_panel(
    "Embedding options",
    options=[
        "--responsive",
        "--embed-font",
        "--text-to-paths",
        "--bare",
        "--svg-class-prefix",
        "--no-self-color-scheme",
        "--no-dark-mode-css",
        "--no-chrome-css",
    ],
)
@click.option_panel(
    "Guard behaviour", options=["--validate", "--strict", "--permissive"]
)
@click.option_panel(
    "Live-progress metadata", options=["--auto-process", "--process-scope"]
)
@click.argument(
    "input_files", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option(
    "-o",
    "--output",
    "outputs",
    type=click.Path(path_type=Path),
    multiple=True,
    cls=_DescribedDefault,
    default_text="<input>.<format>",
    help="Output path. Repeat for several formats.",
)
@click.option(
    "--format",
    "format_",
    type=_FORMAT_TYPE,
    default=None,
    cls=_DescribedDefault,
    default_text="from --output, else svg",
    help="Output format.",
)
@click.option(
    "--scale",
    type=_SCALE_TYPE,
    default=None,
    metavar="FLOAT",
    cls=_DescribedDefault,
    default_text="2 for png, 1 for video",
    help="Raster only: multiply the pixel dimensions by this factor.",
)
@click.option(
    "--raster-width",
    type=_RASTER_WIDTH_TYPE,
    default=None,
    metavar="INTEGER",
    help="Raster only: output width in px. Overrides --scale.",
)
@click.option(
    "--fps",
    type=_FPS_TYPE,
    default=DEFAULT_FPS,
    show_default=True,
    metavar="FLOAT",
    help="Video only: frames per second of the exported loop.",
)
@click.option(
    "--duration",
    type=_DURATION_TYPE,
    default=None,
    metavar="FLOAT",
    cls=_DescribedDefault,
    default_text="the map's animation cycle",
    help="Video only: loop length in seconds.",
)
@click.option(
    "--theme",
    type=click.Choice(sorted(STYLE_NAMES)),
    default=None,
    cls=_DescribedDefault,
    default_text="from %%metro style, else nfcore",
    help="Visual theme.",
)
@click.option(
    "--mode",
    type=click.Choice(["light", "dark"]),
    default=None,
    cls=_DescribedDefault,
    default_text="from %%metro mode",
    help="Palette to render with.",
)
@click.option(
    "--debug/--no-debug",
    default=False,
    help="Show the debug overlay.",
)
@click.option(
    "--logo",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    cls=_DescribedDefault,
    default_text="from %%metro logo",
    help="Logo image path.",
)
@click.option(
    "--line-spread",
    type=click.Choice([m.value for m in LineSpread]),
    default=None,
    cls=_DescribedDefault,
    default_text="from %%metro line_spread, else bundle",
    help="Vertical arrangement of lines sharing a station.",
)
@click.option(
    "--legend",
    default=None,
    cls=_DescribedDefault,
    default_text="from %%metro legend",
    help="Position of the legend+logo block.",
)
@click.option(
    "--from-nextflow",
    is_flag=True,
    default=False,
    help="Convert Nextflow -with-dag mermaid input before rendering.",
)
@click.option(
    "--title",
    type=str,
    default=None,
    cls=_DescribedDefault,
    default_text="from %%metro title",
    help="Pipeline title.",
)
@click.option(
    "--responsive/--no-responsive",
    default=False,
    help="Emit viewBox only (no fixed width/height) for CSS-scalable embedding.",
)
@click.option(
    "--embed-font/--no-embed-font",
    default=False,
    help="Inline an Inter font subset.",
)
@click.option(
    "--text-to-paths/--no-text-to-paths",
    default=False,
    help='Convert text to vector paths. Needs pip install "nf-metro[font]".',
)
@click.option(
    "--svg-class-prefix",
    type=str,
    default="",
    help="Prefix every SVG presentation class.",
)
@click.option(
    "--no-self-color-scheme",
    is_flag=True,
    default=False,
    help="Omit the color-scheme attribute on the root <svg>.",
)
@click.option(
    "--no-dark-mode-css",
    is_flag=True,
    default=False,
    help="Omit the prefers-color-scheme: dark <style> block.",
)
@click.option(
    "--no-chrome-css",
    is_flag=True,
    default=False,
    help="Omit the --nfm-* custom-property <style> block.",
)
@click.option(
    "--bare/--no-bare",
    default=False,
    help="Omit the title and outer padding.",
)
@click.option(
    "--validate",
    "validate_geometry",
    is_flag=True,
    default=False,
    help="Fail if the rendered SVG violates a render-geometry guard.",
)
@click.option(
    "--inactive-lines",
    "inactive_lines",
    default=None,
    help="Comma-separated line IDs to render inactive.",
)
@click.option(
    "--reject-output-outside-source/--no-reject-output-outside-source",
    default=False,
    help="Reject a %%metro output path outside the .mmd's repository.",
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

    Several INPUT_FILEs render in one process. Every file is attempted, and
    the exit code is non-zero if any failed.

    Output paths come from -o, repeatable for several formats, else from the
    map's `%%metro output` directives, else <input>.<format>. A declared
    path may carry its own overrides after a `|`.

    The paths written are printed to stdout as YAML; summaries and warnings
    go to stderr. Set NF_METRO_DEBUG=1 to raise on error instead of
    reporting it.
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
        return _OutputJob(
            out.name, out, format_ or _format_from_output(out), overrides or {}
        )

    def _jobs_for(source: Path, graph: MetroGraph) -> list[_OutputJob]:
        """One job per declared output, else the sibling <source>.<format>."""
        declared = _declared_outputs(
            graph, source, reject_outside_source=reject_output_outside_source
        )
        if declared:
            return [_out_job(out, overrides) for out, overrides in declared]
        fmt = format_ or "svg"
        return [
            _OutputJob(
                source.name, source.with_suffix(f".{fmt}"), cast(RenderFormat, fmt), {}
            )
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
        job_opts = [_job_layout_opts(job.overrides) for job in jobs]
        keys = [
            _graph_key(job.format, opts)
            for job, opts in zip(jobs, job_opts, strict=True)
        ]
        if len(jobs) > 1:
            permissive = bool(layout_opts.get("permissive"))
            with warnings.catch_warnings(record=True) as caught:
                if permissive:
                    warnings.filterwarnings("always", category=PermissiveGuardWarning)
                try:
                    for job, opts, key in zip(jobs, job_opts, keys, strict=True):
                        if key not in graphs:
                            graphs[key] = _prepare_graph_for_render(
                                source,
                                from_nextflow=from_nextflow,
                                title=title,
                                line_spread=line_spread,
                                logo=logo,
                                legend=legend,
                                layout_opts=_layout_opts_for(job.format, opts),
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
                job.label,
                _job(
                    source,
                    job.path,
                    job.format,
                    job.overrides,
                    quiet=quiet,
                    graph=graphs.get(key),
                    parsed=parsed,
                ),
            )
            for job, key in zip(jobs, keys, strict=True)
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
            job = jobs[0]
            _job(
                source, job.path, job.format, job.overrides, quiet=False, parsed=parsed
            )()
        else:
            _render_source(source, jobs, quiet=True, parsed=parsed, batch=True)
        _print_render_result([source], [job.path for job in jobs])
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
            written.extend(job.path for job in jobs)

        return _run

    _run_batch([(f.name, _file_job(f)) for f in input_files])
    _print_render_result(list(input_files), written)


def _print_render_result(sources: list[Path], paths: list[Path]) -> None:
    """Print the render result (version, inputs, output paths) as YAML on stdout.

    JSON-quoted scalars are valid YAML, so no YAML dependency is needed. Called
    only after a successful render, so ``paths`` is never empty and a failed
    render's stdout stays empty: nothing about the result is printed until
    every job it covers has already succeeded.

    ``inputs`` is always a list, even for the single map that most runs
    render, so a consumer never has to branch on its shape.
    """
    document: dict[str, object] = {
        "version": f"v{__version__}",
        "inputs": [str(source) for source in sources],
        "outputs": [str(path) for path in paths],
    }
    echo_yaml("\n".join(to_yaml(document)))


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
            console.print(
                f"[yellow]note[/] [dim]{escape(', '.join(ignored))} only affect "
                "--format svg and are ignored for --format html (the interactive "
                "page is already responsive and scopes each map independently).[/]"
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
                "which this map turns off with %%metro manifest false."
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
        console.print(
            f"[green]✓[/] [dim]{len(graph.stations)} stations, "
            f"{len(graph.edges)} edges, {len(graph.lines)} lines →[/] "
            f"[bold cyan]{escape(str(output))}[/][dim]{escape(detail)}[/]"
        )


def _video_notice(message: str) -> None:
    """Print what a large export is about to cost, before it starts."""
    console.print(f"[yellow]note[/] [dim]{escape(message)}[/]")


def _video_progress(frames: Iterable[bytes], count: int) -> Iterator[bytes]:
    """Draw a narrow diamond progress bar over the frames as they rasterise.

    An export runs for minutes. Rich hides the bar when stderr is not a
    terminal (and not forced in CI), so a piped run stays as quiet as every
    other render.
    """
    with progress_bar("Rendering frames") as progress:
        task = progress.add_task("Rendering frames", total=count)
        for frame in frames:
            yield frame
            progress.advance(task)


@cli.command(name="render-many")
@click.argument("manifest_file", type=click.Path(exists=True, path_type=Path))
def render_many(manifest_file: Path) -> None:
    """Render multiple metro maps from a JSON manifest in one process.

    MANIFEST_FILE is a JSON array of jobs. Each job needs `input` and
    `output`, plus any `nf-metro render` option as a JSON key, with
    layout options nested under `layout_options`.

    Output directories are created as needed. On partial failure the
    successful outputs are kept and the exit code is non-zero.
    """
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
    cls=_DescribedDefault,
    default_text="stdout",
    help="Output .mmd file path.",
)
@click.option(
    "--title",
    type=str,
    default=None,
    help="Pipeline title for the converted output.",
)
def convert(
    input_file: Path,
    output: Path | None,
    title: str | None,
) -> None:
    """Convert a Nextflow -with-dag mermaid file to nf-metro .mmd format.

    Takes a .mmd file produced by `nextflow -with-dag file.mmd`. The output
    can be rendered as-is or hand-tuned first.
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
    help="Also run the layout engine and its invariant suite.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Treat warnings as errors.",
)
def validate(input_file: Path, with_layout: bool, strict: bool) -> None:
    """Validate a Mermaid metro map definition.

    Checks that every edge references a defined line, every section points
    at stations that exist, and the graph is acyclic.
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
    help="Add routes, inferred defaults, and synthetic elements.",
)
def info(input_file: Path, as_json: bool, verbose: bool) -> None:
    """Show information about a Mermaid metro map definition.

    The default output is a human summary; ``--json`` emits the complete
    structure for scripting.
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
        echo_yaml(format_info_text(report, verbose=verbose))


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

    Reports the rule behind each inferred decision (section direction, port
    sides, fold and row layout) and each element the engine inserted.
    ``nf-metro info`` shows what was built; this shows why.
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
@click.option_panel("Server", options=["--port", "--host", "--token", "--open"])
@click.option_panel("Display", options=["--theme", "--overlay"])
@click.option_panel(
    "Shutdown", options=["--shutdown-after-complete", "--shutdown-grace"]
)
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--port", type=int, default=8080, show_default=True, help="Port to listen on."
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Interface to bind. Use 0.0.0.0 to accept remote connections.",
)
@click.option(
    "--theme",
    type=click.Choice(sorted(STYLE_NAMES)),
    default=None,
    cls=_DescribedDefault,
    default_text="from %%metro style, else nfcore",
    help="Visual theme for a .mmd input.",
)
@click.option(
    "--overlay",
    type=click.Choice(OVERLAY_STYLES),
    default=DEFAULT_OVERLAY,
    show_default=True,
    help="Status-overlay style.",
)
@click.option(
    "--token",
    default=None,
    help="Require this token on /events POSTs.",
)
@click.option(
    "--open", "open_browser", is_flag=True, help="Open the live page in a browser."
)
@click.option(
    "--shutdown-after-complete",
    is_flag=True,
    help="Stop the server once the run finishes.",
)
@click.option(
    "--shutdown-grace",
    type=float,
    default=10.0,
    show_default=True,
    help="Seconds to keep the map up after the run finishes.",
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

    Renders the map once and serves it at http://HOST:PORT/. Point a
    Nextflow run's weblog at the events endpoint:

        nextflow run ... -with-weblog http://HOST:PORT/events

    Or pass the run after `--` to have the weblog configured for you:

        nf-metro serve map.mmd --open -- nextflow run my/pipeline

    Only stations carrying a `%%metro process` directive change state.
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
                "Warning: no %%metro process directives; no station will update.",
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
    help="Interface to bind. Use 0.0.0.0 to accept remote connections.",
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
    help="Require this token on POSTs to /maps and /r/*/events.",
)
def serve_multi_cmd(
    port: int, host: str, theme: str, overlay: str, token: str | None
) -> None:
    """Run a persistent live server many pipelines can report into.

    Starts with no map. A pipeline registers one by POSTing the .mmd to
    /maps, then sends weblog events to the run's /r/<id>/events endpoint:

        curl -s --data-binary @map.mmd "http://HOST:PORT/maps?name=myrun"

    The index at http://HOST:PORT/ lists every run with a live status.
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
    help="Nextflow `-with-dag` mermaid file to read process names from.",
)
@click.option(
    "--processes",
    "processes_file",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Newline-delimited process names.",
)
@click.option(
    "--ignore",
    multiple=True,
    help="Regex for processes deliberately left unmapped. Repeatable.",
)
def check_mapping_cmd(
    input_file: Path,
    dag: Path | None,
    processes_file: Path | None,
    ignore: tuple[str, ...],
) -> None:
    """Check a map's `%%metro process` mapping against the processes.

    Reports processes the map cannot show and station patterns that match
    nothing, exiting non-zero if either is found. Supply the pipeline's
    processes with --dag or --processes.
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
    help="Also run the render-geometry guards on the drawn ink.",
)
def validate_svg_cmd(svg_file: Path, geometry: bool) -> None:
    """Validate an SVG's embedded manifest against the manifest JSON Schema.

    With ``--geometry`` it also runs the render-geometry guards on the drawn
    ink.
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

    Prints the ``attachMetroMap()`` driver to stdout, or writes it to ``-o``.
    Load it on a host page alongside an nf-metro SVG.
    """
    from nf_metro.render.driver import get_driver_js

    js = get_driver_js()
    if output is not None:
        output.write_text(js)
        click.echo(f"Written to {output}")
    else:
        click.echo(js, nl=False)
