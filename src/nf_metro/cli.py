"""CLI for nf-metro."""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal, TypeVar

import click

from nf_metro import __version__
from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.explain import build_explain, format_explain_json, format_explain_text
from nf_metro.introspect import build_info, format_info_json, format_info_text
from nf_metro.layout import (
    BackwardFlowError,
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
from nf_metro.parser.model import LineSpread
from nf_metro.render import render_svg, validate_render
from nf_metro.render.html import render_html
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


def _echo_issues(
    label: str, issues: Iterable[ValidationIssue], path: Path | str
) -> None:
    """Print a labelled, bulleted block of validation issues to stderr."""
    click.echo(f"{label}:", err=True)
    for issue in issues:
        click.echo(f"  - {issue.format(path)}", err=True)


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
    help="Visual theme brand "
    "(default: from the %%metro style: directive, else nfcore).",
)
@click.option(
    "--mode",
    type=click.Choice(["light", "dark"]),
    default=None,
    help="Light/dark mode, independent of the theme brand "
    "(default: from the %%metro mode: directive, else the theme's built-in default).",
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
        "Loses selectable text. Requires fonttools[woff]."
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
        "host recoloring is dropped. Use for raster export: cairosvg and "
        "similar rasterizers cannot parse var() and fail without this."
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
        "After rendering, run the render-geometry guards on the produced SVG "
        "(the picture as drawn, including render-time offsets and label "
        "lifts) and fail if any defect is found. SVG output only."
    ),
)
@layout_cli_options
def render(
    input_file: Path,
    output: Path | None,
    format_: str,
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
    no_dark_mode_css: bool,
    no_chrome_css: bool,
    bare: bool,
    validate_geometry: bool,
    **layout_opts: object,
) -> None:
    """Render a Mermaid metro map definition to SVG or interactive HTML."""
    text = input_file.read_text()

    try:
        graph = prepare_graph(
            text,
            from_nextflow=from_nextflow,
            title=title,
            line_spread=line_spread,
            logo=str(logo) if logo is not None else None,
            legend=legend,
            layout_options=layout_opts,
        )
    except (
        ValueError,
        CyclicGraphError,
        BackwardFlowError,
        MixedEntryDirectionError,
        PhaseInvariantError,
    ) as e:
        raise click.ClickException(str(e))

    theme_obj = resolve_theme(theme, graph, mode=mode)

    if output is None:
        output = input_file.with_suffix(f".{format_}")

    font_portability: Literal["embed", "paths"] | None = (
        "paths" if text_to_paths else "embed" if embed_font else None
    )

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
    # --strict they are warnings the default handler prints to stderr.
    try:
        if format_ == "html":
            content = render_html(
                graph,
                theme_obj,
                debug=debug,
                embed_basename=output.name,
                font_portability=font_portability,
                inject_dark_mode_css=not no_dark_mode_css,
            )
        else:
            content = render_svg(
                graph,
                theme_obj,
                debug=debug,
                responsive=responsive,
                font_portability=font_portability,
                svg_class_prefix=svg_class_prefix,
                inject_dark_mode_css=not no_dark_mode_css,
                chrome_css=not no_chrome_css,
                bare=bare,
            )
    except PhaseInvariantError as e:
        raise click.ClickException(str(e))

    if validate_geometry:
        if format_ == "html":
            raise click.ClickException("--validate applies to --format svg only.")
        findings = validate_render(content, graph=graph)
        if findings:
            detail = "\n".join(f"  - {f.message}" for f in findings)
            raise click.ClickException(
                f"render-geometry validation found {len(findings)} "
                f"defect(s) in the drawn SVG:\n{detail}"
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

    issues: list[ValidationIssue] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            graph = parse_metro_mermaid(text)
        except ValueError as e:
            raise click.ClickException(str(e))

        issues.extend(validate_graph(graph))

        if with_layout:
            try:
                compute_layout(graph, validate=True)
            except (
                CyclicGraphError,
                BackwardFlowError,
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
@click.option(
    "--theme", type=str, default=None, help="Theme brand name (nfcore, seqera)."
)
@click.option(
    "--mode",
    type=click.Choice(["light", "dark"]),
    default=None,
    help="Light/dark mode, independent of the theme brand.",
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
    mode: str | None,
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

        theme_obj = resolve_theme(theme, graph, mode=mode)
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
@click.option("--theme", type=str, default="nfcore", help="Theme name (nfcore, light).")
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
    httpd = serve_multi(
        THEMES[theme], host=host, port=port, token=token, overlay=overlay
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
            "validate-svg needs the jsonschema package: pip install jsonschema"
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
