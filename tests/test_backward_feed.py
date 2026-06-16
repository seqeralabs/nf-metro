"""A same-row backward feed must be rejected with a naming error.

A routed metro line is an undirected polyline: within a single row its flow
direction is read purely from horizontal position.  When the grid places a
producer at a column past a consumer it feeds, the line reads as flowing the
wrong way and the only route to the consumer crosses the producer's own box.
``compute_layout`` rejects this on every render (not just under ``validate``),
naming both sections so the author can fix the grid.

Cross-row backward feeds and runways whose exit is explicitly redirected
toward the target are NOT rejected -- the reader can see those as distinct
branches, and the router carries them around cleanly.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.layout import BackwardFlowError, compute_layout
from nf_metro.parser import parse_metro_mermaid

INVALID = Path(__file__).parent / "fixtures" / "invalid"
TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"

# (fixture, source section, target section) for the rejected backward feeds.
_BACKWARD = [
    pytest.param(
        INVALID / "merge_trunk_rightward_source.mmd", "sec_b", "sec_c", id="lr"
    ),
    pytest.param(INVALID / "backward_feed_rl.mmd", "rl_a", "rl_b", id="rl"),
]

# Same-row-or-near backward layouts that are legitimate and must NOT be rejected.
_ALLOWED = [
    pytest.param(TOPOLOGIES / "rl_entry_runway.mmd", id="explicit-exit-runway"),
    pytest.param(TOPOLOGIES / "around_section_below.mmd", id="cross-row-around"),
]


@pytest.mark.parametrize("path,src,tgt", _BACKWARD)
def test_compute_layout_rejects_same_row_backward_feed(path: Path, src: str, tgt: str):
    graph = parse_metro_mermaid(path.read_text())

    with pytest.raises(BackwardFlowError) as excinfo:
        compute_layout(graph)

    message = str(excinfo.value)
    assert src in message
    assert tgt in message


@pytest.mark.parametrize("path,src,tgt", _BACKWARD)
def test_backward_feed_rejected_without_validate(path: Path, src: str, tgt: str):
    """The rejection fires at the default ``validate=False`` -- the render
    path the CLI uses -- not only under the heavier validation plane."""
    graph = parse_metro_mermaid(path.read_text())

    with pytest.raises(BackwardFlowError):
        compute_layout(graph, validate=False)


@pytest.mark.parametrize("path", _ALLOWED)
def test_legitimate_backward_layouts_not_rejected(path: Path):
    graph = parse_metro_mermaid(path.read_text())

    compute_layout(graph)  # must not raise


@pytest.mark.parametrize("path,src,tgt", _BACKWARD)
@pytest.mark.parametrize("command", ["render", "validate"])
def test_cli_rejects_backward_feed(
    path: Path, src: str, tgt: str, command: str, tmp_path: Path
):
    args = [command, str(path)]
    if command == "render":
        args += ["-o", str(tmp_path / "out.svg")]

    result = CliRunner().invoke(cli, args)

    assert result.exit_code != 0
    assert src in result.output
    assert tgt in result.output
