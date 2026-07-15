"""A destination section fed from *conflicting* directions must be rejected.

A routed metro line is an undirected polyline with no arrowheads: the reader
infers flow direction from how a line enters a section.  Two entry
configurations make that unreadable and are rejected on every render (not just
under ``validate``): a single line entering through ports on more than one
side, and two lines entering through *opposing* sides (LEFT with RIGHT, or TOP
with BOTTOM), whose implied flows contradict.

Perpendicular approaches by *different* lines (e.g. one line entering LEFT and
another entering TOP) read cleanly and are permitted -- e.g.
nf-core/genomeassembly's scaffolding section, entered by ``assemblies`` on the
LEFT and ``hic_reads`` on the BOTTOM.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.layout import MixedEntryDirectionError, compute_layout
from nf_metro.parser import parse_metro_mermaid

INVALID = Path(__file__).parent / "fixtures" / "invalid"
EXAMPLES = Path(__file__).parent.parent / "examples"
TOPOLOGIES = EXAMPLES / "topologies"

# (fixture, destination section, conflicting sides) for the rejected feeds:
# opposing sides read as contradictory flow through the same box.
_MIXED = [
    pytest.param(
        INVALID / "mixed_entry_opposing.mmd", "dest", ("left", "right"), id="opposing"
    ),
]

# Diagrams whose destination sections read with coherent approach directions --
# a single side, same-line alternatives that collapse to one side, or distinct
# lines entering from perpendicular sides -- must all keep rendering.
_ALLOWED = [
    pytest.param(EXAMPLES / "genomeassembly.mmd", id="genomeassembly-perp"),
    pytest.param(
        EXAMPLES / "variantbenchmarking.mmd", id="variantbenchmarking-collapse"
    ),
    pytest.param(EXAMPLES / "rnaseq_sections.mmd", id="rnaseq"),
    pytest.param(TOPOLOGIES / "around_section_below.mmd", id="around-below"),
    pytest.param(TOPOLOGIES / "mixed_entry_perpendicular.mmd", id="perpendicular"),
]

# Opposing-axis pairs: their co-presence on one section is the rejected case.
_OPPOSING_AXES = [("left", "right"), ("top", "bottom")]


@pytest.mark.parametrize("path,section,sides", _MIXED)
def test_compute_layout_rejects_mixed_entry_directions(
    path: Path, section: str, sides: tuple[str, ...]
):
    graph = parse_metro_mermaid(path.read_text())

    with pytest.raises(MixedEntryDirectionError) as excinfo:
        compute_layout(graph)

    message = str(excinfo.value)
    assert section in message
    for side in sides:
        assert side in message


@pytest.mark.parametrize("path,section,sides", _MIXED)
def test_mixed_entry_rejected_without_validate(
    path: Path, section: str, sides: tuple[str, ...]
):
    """The rejection fires at the default ``validate=False`` -- the render
    path the CLI uses -- not only under the heavier validation plane."""
    graph = parse_metro_mermaid(path.read_text())

    with pytest.raises(MixedEntryDirectionError):
        compute_layout(graph, validate=False)


@pytest.mark.parametrize("path", _ALLOWED)
def test_permitted_entries_not_rejected(path: Path):
    graph = parse_metro_mermaid(path.read_text())

    compute_layout(graph)  # must not raise

    for section in graph.sections.values():
        line_sides: dict[str, set] = {}
        for pid in section.entry_ports:
            if pid not in graph.ports:
                continue
            for line in graph.station_lines(pid):
                line_sides.setdefault(line, set()).add(graph.ports[pid].side.value)
        for line, sides in line_sides.items():
            assert len(sides) == 1, (
                f"section '{section.id}' line '{line}' enters from multiple "
                f"sides {sorted(sides)} yet was not rejected"
            )
        present = {s for sides in line_sides.values() for s in sides}
        for lo, hi in _OPPOSING_AXES:
            assert not (lo in present and hi in present), (
                f"section '{section.id}' has opposing entries {lo}/{hi} "
                f"yet was not rejected"
            )


@pytest.mark.parametrize("path,section,sides", _MIXED)
@pytest.mark.parametrize("command", ["render", "validate"])
def test_cli_rejects_mixed_entry_directions(
    path: Path,
    section: str,
    sides: tuple[str, ...],
    command: str,
    tmp_path: Path,
):
    args = [command, str(path)]
    if command == "render":
        args += ["-o", str(tmp_path / "out.svg")]
    elif command == "validate":
        args += ["--with-layout"]

    result = CliRunner().invoke(cli, args)

    assert result.exit_code != 0
    assert section in result.output


def test_error_names_lines_per_side():
    """The message attributes each conflicting side to its lines so the
    author can see which feed to redirect."""
    graph = parse_metro_mermaid((INVALID / "mixed_entry_opposing.mmd").read_text())

    with pytest.raises(MixedEntryDirectionError) as excinfo:
        compute_layout(graph)

    message = str(excinfo.value)
    assert "a" in message and "b" in message
