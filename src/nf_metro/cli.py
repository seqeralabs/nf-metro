"""CLI for nf-metro."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click

from nf_metro import __version__
from nf_metro.layout import PhaseInvariantError, compute_layout
from nf_metro.options import LAYOUT_OPTIONS, LayoutOption
from nf_metro.parser import ERROR, parse_metro_mermaid, validate_graph
from nf_metro.parser.directives import apply_legend_directive
from nf_metro.parser.model import LineSpread, MetroGraph
from nf_metro.render import render_svg
from nf_metro.render.html import render_html
from nf_metro.render.style import Theme
from nf_metro.themes import THEMES


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """nf-metro: Generate metro-map-style SVG diagrams from Mermaid definitions."""


_F = TypeVar("_F", bound=Callable[..., Any])


def _layout_cli_option(opt: LayoutOption) -> Callable[..., Any]:
    """Build the ``click.option`` decorator for a registry option.

    All default to ``None`` so an omitted flag leaves the directive value in
    place; a set flag overrides it (the CLI half of the cascade in
    :mod:`nf_metro.options`).
    """
    if opt.kind == "bool":
        no_flag = "--no-" + opt.name.replace("_", "-")
        return click.option(
            f"{opt.cli_flag}/{no_flag}", opt.name, default=None, help=opt.help
        )
    ctype: Any = (
        click.Choice(opt.choices)
        if opt.kind == "choice"
        else {
            "int": int,
            "float": float,
            "str": str,
        }[opt.kind]
    )
    return click.option(opt.cli_flag, opt.name, type=ctype, default=None, help=opt.help)


def layout_cli_options(f: _F) -> _F:
    """Attach a CLI flag for every registry option, in declaration order."""
    for opt in reversed(LAYOUT_OPTIONS):
        f = _layout_cli_option(opt)(f)
    return f


def _apply_layout_overrides(graph: MetroGraph, opts: dict[str, object]) -> None:
    """Write each explicitly-set CLI option onto its graph field.

    Parse-time options (``fold_threshold``) are skipped: their value reaches
    the graph through ``parse_metro_mermaid`` instead.
    """
    for opt in LAYOUT_OPTIONS:
        if opt.parse_time:
            continue
        value = opts.get(opt.name)
        if value is not None:
            setattr(graph, opt.target_attr, value)


# Maps legacy `style:` values onto theme keys (the nfcore theme is the dark one).
_STYLE_THEME_ALIASES = {"dark": "nfcore"}


def _resolve_theme(theme: str | None, graph: MetroGraph) -> Theme:
    """Pick the theme: an explicit ``--theme`` wins over the ``style:`` directive."""
    if theme is not None:
        return THEMES[theme]
    name = graph.style.strip().lower()
    return THEMES.get(_STYLE_THEME_ALIASES.get(name, name), THEMES["nfcore"])


@cli.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file path. Defaults to <input>.<format>",
)
@click.option(
    "--format",
    "format_",
    type=click.Choice(["svg", "html"]),
    default="svg",
    help="Output format: 'svg' (default) or 'html' for an interactive "
    "self-contained page with pan/zoom and per-line filtering.",
)
@click.option(
    "--theme",
    type=click.Choice(list(THEMES.keys())),
    default=None,
    help="Visual theme (default: from the %%metro style: directive, else nfcore).",
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
@layout_cli_options
def render(
    input_file: Path,
    output: Path | None,
    format_: str,
    theme: str | None,
    debug: bool,
    logo: Path | None,
    line_spread: str | None,
    legend: str | None,
    from_nextflow: bool,
    title: str | None,
    **layout_opts: object,
) -> None:
    """Render a Mermaid metro map definition to SVG or interactive HTML."""
    text = input_file.read_text()

    if from_nextflow:
        from nf_metro.convert import convert_nextflow_dag

        text = convert_nextflow_dag(text, title=title or "")

    fold = layout_opts.get("fold_threshold")
    try:
        graph = parse_metro_mermaid(
            text,
            max_station_columns=fold if isinstance(fold, int) else None,
        )
    except ValueError as e:
        raise click.ClickException(str(e))

    _apply_layout_overrides(graph, layout_opts)

    if line_spread is not None:
        graph.line_spread = LineSpread(line_spread)
    if logo is not None:
        graph.logo_path = str(logo)
    if legend is not None:
        apply_legend_directive(legend, graph)
    if title is not None:
        graph.title = title

    try:
        compute_layout(graph)
    except PhaseInvariantError as e:
        raise click.ClickException(str(e))

    theme_obj = _resolve_theme(theme, graph)

    if output is None:
        output = input_file.with_suffix(f".{format_}")

    if format_ == "html":
        content = render_html(graph, theme_obj, debug=debug, embed_basename=output.name)
    else:
        content = render_svg(graph, theme_obj, debug=debug)

    output.write_text(content if content.endswith("\n") else content + "\n")
    click.echo(
        f"Rendered {len(graph.stations)} stations, "
        f"{len(graph.edges)} edges, "
        f"{len(graph.lines)} lines -> {output}"
    )


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
    from nf_metro.convert import convert_nextflow_dag, is_nextflow_dag

    text = input_file.read_text()

    if not is_nextflow_dag(text):
        click.echo(
            "Warning: input does not look like a Nextflow DAG "
            "(expected 'flowchart TB' header)",
            err=True,
        )

    result = convert_nextflow_dag(text, title=title or "")

    if output is None:
        click.echo(result, nl=False)
    else:
        output.write_text(result if result.endswith("\n") else result + "\n")
        # Count sections and processes in the output
        sections = result.count("subgraph ")
        processes = result.count("([")
        click.echo(f"Converted {processes} processes, {sections} sections -> {output}")


@cli.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
def validate(input_file: Path) -> None:
    """Validate a Mermaid metro map definition."""
    text = input_file.read_text()
    try:
        graph = parse_metro_mermaid(text)
    except Exception as e:
        click.echo(f"Parse error: {e}", err=True)
        raise SystemExit(1)

    errors = [issue for issue in validate_graph(graph) if issue.severity == ERROR]

    if errors:
        click.echo("Validation errors:", err=True)
        for issue in errors:
            click.echo(f"  - {issue.message}", err=True)
        raise SystemExit(1)

    click.echo(
        f"Valid: {len(graph.stations)} stations, "
        f"{len(graph.edges)} edges, "
        f"{len(graph.lines)} lines, "
        f"{len(graph.sections)} sections"
    )


@cli.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
def info(input_file: Path) -> None:
    """Show information about a Mermaid metro map definition."""
    text = input_file.read_text()
    try:
        graph = parse_metro_mermaid(text)
    except ValueError as e:
        raise click.ClickException(str(e))

    click.echo(f"Title: {graph.title or '(none)'}")
    click.echo(f"Style: {graph.style}")
    click.echo(f"Stations: {len(graph.stations)}")
    click.echo(f"Edges: {len(graph.edges)}")
    click.echo(f"Lines: {len(graph.lines)}")
    for lid, line in graph.lines.items():
        stations = graph.line_stations(lid)
        click.echo(f"  {line.display_name} ({line.color}): {len(stations)} stations")
    click.echo(f"Sections: {len(graph.sections)}")
    for section in graph.sections.values():
        click.echo(
            f"  [{section.number}] {section.name}: {len(section.station_ids)} stations"
        )
