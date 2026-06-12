"""Cyclic / self-loop graphs must fail fast with a node-naming error.

The layout engine assumes a DAG (``assign_layers`` runs a topological sort).
A cycle or self-loop in the parsed graph must be rejected on both the
``validate`` plane (a structured :class:`ValidationIssue`) and the ``render``
plane (a ``ValueError`` out of ``compute_layout``), naming at least one node
so an author can locate the offending edge.
"""

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.layout import compute_layout
from nf_metro.parser import (
    ERROR,
    CyclicGraphError,
    parse_metro_mermaid,
    validate_graph,
)

_TWO_NODE_CYCLE = """\
%%metro line: l1 | Line 1 | #ff0000 | solid
graph LR
    a[A] -->|l1| b[B]
    b -->|l1| a
"""

_SELF_LOOP = """\
%%metro line: l1 | Line 1 | #ff0000 | solid
graph LR
    a[A] -->|l1| a
"""

_CYCLIC = [
    pytest.param(_TWO_NODE_CYCLE, id="two-node-cycle"),
    pytest.param(_SELF_LOOP, id="self-loop"),
]


@pytest.mark.parametrize("mmd", _CYCLIC)
def test_validate_graph_flags_cycle(mmd: str):
    graph = parse_metro_mermaid(mmd)

    cycle_errors = [
        issue
        for issue in validate_graph(graph)
        if issue.severity == ERROR and "cycle" in issue.message.lower()
    ]

    assert len(cycle_errors) == 1
    assert "a" in cycle_errors[0].message


@pytest.mark.parametrize("mmd", _CYCLIC)
def test_compute_layout_rejects_cycle(mmd: str):
    graph = parse_metro_mermaid(mmd)

    with pytest.raises(CyclicGraphError, match="cycle") as excinfo:
        compute_layout(graph)

    assert "a" in str(excinfo.value)


@pytest.mark.parametrize("mmd", _CYCLIC)
@pytest.mark.parametrize("command", ["render", "validate"])
def test_cli_rejects_cycle(mmd: str, command: str, tmp_path):
    src = tmp_path / "cyclic.mmd"
    src.write_text(mmd)
    args = [command, str(src)]
    if command == "render":
        args += ["-o", str(tmp_path / "out.svg")]

    result = CliRunner().invoke(cli, args)

    assert result.exit_code != 0
    assert "cycle" in result.output.lower()
    assert "a" in result.output
