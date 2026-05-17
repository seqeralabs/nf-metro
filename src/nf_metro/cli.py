"""CLI for nf-metro."""

from __future__ import annotations

from pathlib import Path

import click

from nf_metro import __version__
from nf_metro.layout import compute_layout
from nf_metro.parser import parse_metro_mermaid
from nf_metro.render import render_svg
from nf_metro.render.html import render_html
from nf_metro.themes import THEMES


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """nf-metro: Generate metro-map-style SVG diagrams from Mermaid definitions."""


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
    default="nfcore",
    help="Visual theme (default: nfcore)",
)
@click.option("--width", type=int, default=None, help="SVG width in pixels")
@click.option("--height", type=int, default=None, help="SVG height in pixels")
@click.option(
    "--x-spacing",
    type=float,
    default=60.0,
    help="Horizontal spacing between layers (default: 60)",
)
@click.option(
    "--y-spacing",
    type=float,
    default=None,
    help="Vertical spacing between tracks (default: auto - derived from "
    "the map's content so captioned icons and dense labels don't collide)",
)
@click.option(
    "--max-layers-per-row",
    type=int,
    default=None,
    help="Max layers before folding to next row (default: auto)",
)
@click.option(
    "--animate/--no-animate",
    default=False,
    help="Add animated balls traveling along lines",
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
    help="Logo image path (overrides %%metro logo: directive)",
)
@click.option(
    "--line-order",
    type=click.Choice(["definition", "span"]),
    default=None,
    help="Line ordering strategy: 'definition' (default) preserves .mmd order, "
    "'span' sorts by section span (longest first)",
)
@click.option(
    "--straight-diamonds/--no-straight-diamonds",
    default=True,
    help="Keep top branch of diamond fork-joins on the main track (default: on). "
    "Use --no-straight-diamonds for symmetric fan-out.",
)
@click.option(
    "--center-ports/--no-center-ports",
    default=None,
    help="Centre inter-section ports on the shorter of the two connected "
    "sections, so lines enter/exit at the visual midpoint. When unset, "
    "the value of the %%metro center_ports: directive (if any) is used.",
)
@click.option(
    "--section-x-gap",
    type=float,
    default=None,
    help="Horizontal gap between sections (default: 50)",
)
@click.option(
    "--section-y-gap",
    type=float,
    default=None,
    help="Vertical gap between sections (default: 40)",
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
    help="Pipeline title (used with --from-nextflow)",
)
def render(
    input_file: Path,
    output: Path | None,
    format_: str,
    theme: str,
    width: int | None,
    height: int | None,
    x_spacing: float,
    y_spacing: float | None,
    max_layers_per_row: int | None,
    animate: bool,
    debug: bool,
    logo: Path | None,
    line_order: str | None,
    straight_diamonds: bool,
    center_ports: bool | None,
    section_x_gap: float | None,
    section_y_gap: float | None,
    from_nextflow: bool,
    title: str | None,
) -> None:
    """Render a Mermaid metro map definition to SVG or interactive HTML."""
    text = input_file.read_text()

    if from_nextflow:
        from nf_metro.convert import convert_nextflow_dag

        text = convert_nextflow_dag(text, title=title or "")

    try:
        graph = parse_metro_mermaid(
            text,
            max_station_columns=max_layers_per_row or 15,
        )
    except ValueError as e:
        raise click.ClickException(str(e))

    if line_order is not None:
        graph.line_order = line_order

    if not straight_diamonds:
        graph.diamond_style = "symmetric"

    if center_ports is not None:
        graph.center_ports = center_ports

    if logo is not None:
        graph.logo_path = str(logo)

    layout_kwargs: dict = dict(
        x_spacing=x_spacing,
        y_spacing=y_spacing,
    )
    if section_x_gap is not None:
        layout_kwargs["section_x_gap"] = section_x_gap
    if section_y_gap is not None:
        layout_kwargs["section_y_gap"] = section_y_gap
    compute_layout(graph, **layout_kwargs)

    theme_obj = THEMES[theme]

    if output is None:
        output = input_file.with_suffix(f".{format_}")

    if format_ == "html":
        content = render_html(
            graph,
            theme_obj,
            width=width,
            height=height,
            animate=animate,
            debug=debug,
            embed_basename=output.name,
        )
    else:
        content = render_svg(
            graph, theme_obj, width=width, height=height, animate=animate, debug=debug
        )

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

    errors = []

    # Check that all edge line_ids reference defined lines
    for edge in graph.edges:
        if edge.line_id != "default" and edge.line_id not in graph.lines:
            errors.append(
                f"Edge {edge.source} -> {edge.target} references "
                f"undefined line '{edge.line_id}'"
            )

    # Check that section station IDs exist
    for section in graph.sections.values():
        for sid in section.station_ids:
            if sid not in graph.stations:
                errors.append(
                    f"Section '{section.name}' references unknown station '{sid}'"
                )

    if errors:
        click.echo("Validation errors:", err=True)
        for err in errors:
            click.echo(f"  - {err}", err=True)
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
