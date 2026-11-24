"""An off-track input never lands on an on-track station sharing its column.

An off-track input sits beside the station it feeds, offset across the flow
and bumped further out past the lanes of stations downstream of it.  A station
in the input's own column is just as much in the way: the input must clear it
too, rather than land on its marker, in every orientation.
"""

from __future__ import annotations

import warnings

import pytest
from orientation_transform import Orientation, all_orientations, transform_source

from nf_metro.api import RenderConfig, prepare_graph, render_graph
from nf_metro.layout.phases.guards import PermissiveGuardWarning
from nf_metro.themes import THEMES

_SOURCE_ABOVE_ITS_ANCHOR = """\
%%metro line_spread: centered
%%metro line: c | C | #f5c542
%%metro line: main | Main | #2db572
%%metro line: e | E | #984ea3
%%metro off_track: aux
graph LR
    subgraph p [P]
        a -->|main| s
        s -->|main| d
        d -->|main| m
        aux -->|main| d
        f -->|c| m
        h -->|e| m
    end
"""

_PEEL = """\
%%metro line: main | Main | #2db572
%%metro line: b | B | #0570b0
%%metro line: c | C | #f5c542
%%metro off_track: aux
graph LR
    subgraph p [P]
        a -->|main| s
        s -->|main| d
        d -->|main| j
        j -->|main| m
        s -->|b| g
        g -->|b| j
{feed}
        f -->|c| m
    end
"""

SHAPES = {
    "source_above_its_anchor": _SOURCE_ABOVE_ITS_ANCHOR,
    "contender_beside_input_to_trunk": _PEEL.format(feed="        aux -->|c| d"),
    "contender_beside_input_to_peel": _PEEL.format(feed="        aux -->|b| g"),
}


@pytest.mark.parametrize("orientation", list(all_orientations()), ids=lambda o: o.name)
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_off_track_input_clears_stations_in_its_column(
    shape: str, orientation: Orientation
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = prepare_graph(transform_source(SHAPES[shape], orientation))
        render_graph(graph, THEMES["nfcore"], RenderConfig())
    downgrades = [
        str(w.message) for w in caught if issubclass(w.category, PermissiveGuardWarning)
    ]
    assert not downgrades, downgrades
    aux = graph.stations["aux"]
    landed_on = [
        sid
        for sid, st in graph.stations.items()
        if sid != "aux"
        and not st.is_port
        and abs(st.x - aux.x) < 1.0
        and abs(st.y - aux.y) < 1.0
    ]
    assert not landed_on, f"'aux' lands on {landed_on}"
