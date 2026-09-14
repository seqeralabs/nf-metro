"""Regression lock for #1967: the ``--line-spread`` CLI flag and the
``%%metro line_spread:`` directive produce the same graph for identical intent.

Parse-time readers of ``graph.line_spread`` (interchange inference skips rail
sections) must see the caller-supplied spread. On
``rail_marker_subset_interchange.mmd`` a divergence lets the flag path infer a
spurious interchange, expanding it into an extra station the directive path
never gets.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.api import _prepare_graph_state
from nf_metro.parser.model import LineSpread

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "rail_marker_subset_interchange.mmd"
)


def _interchange_ids(graph):
    return sorted(ic.node_id for ic in graph.interchanges)


def test_cli_flag_and_directive_line_spread_agree():
    directive_text = FIXTURE.read_text()
    flag_text = "".join(
        line
        for line in directive_text.splitlines(keepends=True)
        if not line.startswith("%%metro line_spread:")
    )

    directive_graph = _prepare_graph_state(directive_text)
    flag_graph = _prepare_graph_state(flag_text, line_spread="rails")

    assert directive_graph.line_spread is LineSpread.RAILS
    assert flag_graph.line_spread is LineSpread.RAILS

    assert len(flag_graph.stations) == len(directive_graph.stations)
    assert _interchange_ids(flag_graph) == _interchange_ids(directive_graph)
