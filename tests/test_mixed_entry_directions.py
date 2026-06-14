"""A destination section fed from more than one direction must be rejected.

A routed metro line is an undirected polyline with no arrowheads: the reader
infers flow direction from how a line enters a section.  When one line enters a
section's port heading one way and another line enters a different port heading
another, the approach directions conflict and the diagram cannot show which way
flow runs.  ``compute_layout`` rejects this on every render (not just under
``validate``), naming the section and the conflicting sides so the author can
fix the grid.

Sections whose incoming routes all share a single approach side render
unambiguously and are NOT rejected -- including diagrams whose inferred
multi-side hints collapse to one natural entry side (the engine's existing
behaviour for e.g. nf-core/genomeassembly's scaffolding section).
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

# (fixture, destination section, conflicting sides) for the rejected feeds.
_MIXED = [
    pytest.param(
        INVALID / "mixed_entry_opposing.mmd", "dest", ("left", "right"), id="opposing"
    ),
    pytest.param(
        INVALID / "mixed_entry_perpendicular.mmd",
        "dest",
        ("left", "top"),
        id="perpendicular",
    ),
]

# Diagrams whose destination sections all read with one coherent approach
# direction -- they must keep rendering.  genomeassembly/variantbenchmarking
# infer multi-side entry hints that the parser collapses to one natural side.
_ALLOWED = [
    pytest.param(EXAMPLES / "genomeassembly.mmd", id="genomeassembly-collapse"),
    pytest.param(
        EXAMPLES / "variantbenchmarking.mmd", id="variantbenchmarking-collapse"
    ),
    pytest.param(EXAMPLES / "rnaseq_sections.mmd", id="rnaseq"),
    pytest.param(TOPOLOGIES / "around_section_below.mmd", id="around-below"),
]


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
def test_single_direction_entries_not_rejected(path: Path):
    graph = parse_metro_mermaid(path.read_text())

    compute_layout(graph)  # must not raise

    for section in graph.sections.values():
        sides = {
            graph.ports[pid].side for pid in section.entry_ports if pid in graph.ports
        }
        assert len(sides) <= 1, (
            f"section '{section.id}' has entry ports on multiple sides "
            f"{sorted(s.value for s in sides)} yet was not rejected"
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
