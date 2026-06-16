"""CLI for nf-metro."""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import click

from nf_metro import __version__
from nf_metro.explain import build_explain, format_explain_json, format_explain_text
from nf_metro.introspect import build_info, format_info_json, format_info_text
from nf_metro.layout import (
    BackwardFlowError,
    MixedEntryDirectionError,
    PhaseInvariantError,
    compute_layout,
)
from nf_metro.options import LAYOUT_OPTIONS, LayoutOption
from nf_metro.parser import (
    ERROR,
    CyclicGraphError,
    parse_metro_mermaid,
    validate_graph,
)
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
            f"{opt.cli_flag}/{no_flag}",
            opt.name,
            default=None,
            help=opt.help,
            hidden=opt.hidden,
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
    return click.option(
        opt.cli_flag,
        opt.name,
        type=ctype,
        default=None,
        help=opt.help,
        hidden=opt.hidden,
    )


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


def _fail_validation(messages: Iterable[str]) -> NoReturn:
    """Print validation errors to stderr and exit non-zero."""
    click.echo("Validation errors:", err=True)
    for message in messages:
        click.echo(f"  - {message}", err=True)
    raise SystemExit(1)


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
    except (
        CyclicGraphError,
        BackwardFlowError,
        MixedEntryDirectionError,
        PhaseInvariantError,
    ) as e:
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
        _fail_validation(issue.message for issue in errors)

    try:
        compute_layout(graph)
    except (BackwardFlowError, MixedEntryDirectionError, PhaseInvariantError) as e:
        _fail_validation([str(e)])

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
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            graph = parse_metro_mermaid(text)
        except ValueError as e:
            raise click.ClickException(str(e))
    messages = [str(w.message) for w in caught]

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
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            graph = parse_metro_mermaid(text)
        except ValueError as e:
            raise click.ClickException(str(e))
    messages = [str(w.message) for w in caught]

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
@click.option("--theme", type=str, default=None, help="Theme name (nfcore, light).")
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
    token: str | None,
    open_browser: bool,
    shutdown_after_complete: bool,
    shutdown_grace: float,
    launch_cmd: tuple[str, ...],
) -> None:
    """Serve a live-progress view of a metro map. [experimental]

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
    from nf_metro.live.server import run_lifecycle
    from nf_metro.live.server import serve as serve_map

    try:
        graph = parse_metro_mermaid(input_file.read_text())
        compute_layout(graph)
    except (ValueError, PhaseInvariantError) as e:
        raise click.ClickException(str(e))

    theme_obj = _resolve_theme(theme, graph)
    mapped = sorted(graph.process_mapping)
    if not mapped:
        click.echo(
            "Warning: no %%metro process: directives; no station will update.",
            err=True,
        )
    if host == "0.0.0.0":  # noqa: S104 - explicit opt-in, warned
        click.echo(
            "Binding 0.0.0.0: reachable from other hosts; "
            "use --token to restrict /events.",
            err=True,
        )

    httpd = serve_map(graph, theme_obj, host=host, port=port, token=token)
    # Local subprocesses post to a concrete loopback address, not 0.0.0.0.
    run_host = "127.0.0.1" if host == "0.0.0.0" else host
    page_url = f"http://{run_host}:{port}/"
    events_url = f"{page_url}events"
    if token:
        events_url += f"?token={token}"

    click.echo("nf-metro live progress (experimental)")
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
@click.option("--theme", type=str, default="nfcore", help="Theme name (nfcore, light).")
@click.option(
    "--token",
    default=None,
    help="If set, POSTs to /maps and /r/*/events must supply ?token=... "
    "or an X-Metro-Token header.",
)
def serve_multi_cmd(port: int, host: str, theme: str, token: str | None) -> None:
    """Run a persistent live server many pipelines can report into. [experimental]

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

    if theme not in THEMES:
        raise click.ClickException(
            f"unknown theme {theme!r}; choose from {list(THEMES)}"
        )
    if host == "0.0.0.0":  # noqa: S104 - explicit opt-in, warned
        click.echo(
            "Binding 0.0.0.0: reachable from other hosts; "
            "use --token to restrict POSTs.",
            err=True,
        )
    httpd = serve_multi(THEMES[theme], host=host, port=port, token=token)
    display_host = "localhost" if host == "127.0.0.1" else host
    click.echo("nf-metro live progress - persistent server (experimental)")
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
    """Check a map's `%%metro process:` mapping against the processes. [experimental]

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
def validate_svg_cmd(svg_file: Path) -> None:
    """Validate an SVG's embedded manifest against the manifest JSON Schema."""
    from nf_metro.render import manifest_schema, read_manifest

    try:
        import jsonschema
    except ImportError:
        raise click.ClickException(
            "validate-svg needs the jsonschema package: pip install jsonschema"
        )

    manifest = read_manifest(svg_file.read_text())
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

    click.echo(
        f"Valid: {len(manifest.get('nodes', []))} nodes, "
        f"schema version {manifest.get('version')}"
    )
