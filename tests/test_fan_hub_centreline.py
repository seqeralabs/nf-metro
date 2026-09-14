"""Tests for fork-hub / join-hub centreline agreement (#1595, #1617).

Under ``diamond_style: symmetric`` the join hub of a fan is explicitly
recentred on its (post-grid-snap) branch midpoint, but the fork hub is not -
it is grid-snapped like any other station. For an odd branch count the
midpoint lands on a grid line anyway, so the two agree by coincidence. For
an even branch count the midpoint sits at a half-pitch offset, and only the
join hub gets pulled back onto it: the fork hub is left wherever the grid
snap nearest it happened to land, which can be a full pitch away from the
join hub (#1595).

The centreline the two hubs settle on must also reach the section's own
LR/RL ports and the single-line trunk stations feeding them, otherwise the
boundary run leaves the section as a diagonal and the upstream trunk sits a
row off the fan it feeds (#1617).

A fork that begins *at* a section's entry port - rather than at an in-section
hub the port feeds - surfaces an overlapping but distinct defect (#1272:
``diamond_style: symmetric`` never applies to that fork's branch placement)
and is out of scope here.

Rail-laid sections are the one place where the two hubs legitimately differ,
because a rail station's Y is the centre of the rail span it carries rather
than a marker centreline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_fork_join_hub_centreline_agree,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph


def _bare_fan(n: int) -> str:
    stations = "\n".join(f"        b{i}[B{i}]" for i in range(n))
    forks = "\n".join(f"        hub -->|a| b{i}" for i in range(n))
    joins = "\n".join(f"        b{i} -->|a| j" for i in range(n))
    return f"""%%metro title: fan
%%metro diamond_style: symmetric
%%metro line: a | A | #24b064
graph LR
    subgraph s [S]
        hub[Hub]
{stations}
        j[Join]
{forks}
{joins}
    end
"""


def _ported_fan(n: int) -> str:
    stations = "\n".join(f"        b{i}[B{i}]" for i in range(n))
    forks = "\n".join(f"        hub -->|a| b{i}" for i in range(n))
    joins = "\n".join(f"        b{i} -->|a| j" for i in range(n))
    return f"""%%metro title: ported fan
%%metro diamond_style: symmetric
%%metro line: a | A | #24b064
graph LR
    subgraph up [Up]
        feed[Feed]
    end
    subgraph s [S]
        hub[ ]
{stations}
        j[ ]
{forks}
{joins}
    end
    subgraph down [Down]
        sink[Sink]
    end
    feed -->|a| hub
    j -->|a| sink
"""


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8])
def test_fork_and_join_hub_share_centreline(n: int) -> None:
    graph = parse_metro_mermaid(_bare_fan(n))
    compute_layout(graph)
    hub = graph.stations["hub"]
    join = graph.stations["j"]
    branch_ys = sorted(graph.stations[f"b{i}"].y for i in range(n))
    mean = (branch_ys[0] + branch_ys[-1]) / 2.0
    assert join.y == pytest.approx(mean, abs=1.0), "join hub off the branch mean"
    assert hub.y == pytest.approx(mean, abs=1.0), "fork hub off the branch mean"
    assert hub.y == pytest.approx(join.y, abs=1.0), "fork/join hubs disagree"


@pytest.mark.parametrize("n", [3, 4, 5, 6, 7, 8])
def test_ported_fan_centreline_reaches_ports_and_trunk(n: int) -> None:
    """The fan section's LR ports, and the trunk stations either side of it,
    sit on the same centreline the fork and join hubs settle on.

    ``feed -> up exit -> s entry -> hub`` and ``j -> s exit -> down entry ->
    sink`` are single-line pass-through chains carrying nothing but the trunk,
    so every member has to share the hubs' Y.

    Two branches take the older 2-branch half-grid compaction path instead,
    whose port-fed placement is the separate gap #1272 tracks, so they are out
    of scope here.
    """
    graph = parse_metro_mermaid(_ported_fan(n))
    compute_layout(graph, validate=True)
    centre = graph.stations["hub"].y
    followers = ["feed", "sink"] + [
        pid
        for sec in graph.sections.values()
        for pid in (*sec.entry_ports, *sec.exit_ports)
    ]
    off = {
        sid: graph.stations[sid].y
        for sid in followers
        if abs(graph.stations[sid].y - centre) > 1.0
    }
    assert not off, f"off the hub centreline y={centre}: {off}"


def _two_overlapping_fans() -> str:
    return """%%metro title: overlapping fans
%%metro diamond_style: symmetric
%%metro line: x | X | #24b064
graph LR
    subgraph s [S]
        wide[Wide fork]
        narrow[Narrow fork]
        a[A]
        b[B]
        c[C]
        j[Join]
        wide -->|x| a
        wide -->|x| b
        wide -->|x| c
        narrow -->|x| a
        narrow -->|x| b
        a -->|x| j
        b -->|x| j
        c -->|x| j
    end
"""


def _seat_diamond_hub_off_centre(
    graph: MetroGraph,
    hub_id: str,
    branch_ids: tuple[str, str, str] = ("a", "b", "c"),
    join_id: str = "j",
) -> None:
    """Seat the three branches at 0/40/80 with the join on their 40 midpoint and
    ``hub_id`` a full 10px off it.

    This is the exact eligibility the guard screens for - a divergence anchor
    sitting strictly between evenly-spaced branches whose set matches the join's
    sources - with the hub deliberately off the centreline so the guard reports
    it unless the pair is exempt.
    """
    for sid, y in zip(branch_ids, (0.0, 40.0, 80.0), strict=True):
        graph.stations[sid].y = y
    graph.stations[join_id].y = 40.0
    graph.stations[hub_id].y = 30.0


def test_two_overlapping_fans_do_not_trip_centreline_guard() -> None:
    """A join shared by two overlapping fans is not one diamond (issue #1874).

    ``wide`` reaches every branch and ``narrow`` reaches a subset, so their
    target and source sets coincide with the join without ``wide`` being the
    sole apex: ``a`` and ``b`` carry ``narrow`` as a second fork.  The guard
    must not demand ``wide`` sit on the shared join's centreline, because
    ``wide`` is legitimately seated by the fan that actually owns it.
    """
    graph = parse_metro_mermaid(_two_overlapping_fans())
    compute_layout(graph, validate=False)
    _seat_diamond_hub_off_centre(graph, "wide")
    _guard_fork_join_hub_centreline_agree(graph, "test")


def test_centreline_guard_flags_broken_single_fork_diamond() -> None:
    """The exemption is narrow: a genuine single-fork diamond whose fork hub
    sits off the join centreline is still reported (issue #1595).

    Every branch here has ``hub`` as its only fork, so the pair is a real
    diamond; moving the hub off the join midpoint must still raise.
    """
    graph = parse_metro_mermaid(_bare_fan(3))
    compute_layout(graph, validate=False)
    _seat_diamond_hub_off_centre(graph, "hub", branch_ids=("b0", "b1", "b2"))
    with pytest.raises(PhaseInvariantError, match="disagree on centreline"):
        _guard_fork_join_hub_centreline_agree(graph, "test")


def test_centreline_guard_defers_transient_disagreement_until_final() -> None:
    """A hub caught mid-descent during a deferred pass is not reported; the same
    disagreement at the final checkpoint is (issue #1874).

    The pre-bypass and geometric-bypass passes settle a diamond's hubs onto
    their shared centreline only by the closing stages, so a checkpoint reached
    with ``_defer_final_guards`` set may see a transient disagreement that the
    final geometry resolves.  The guard raises only once the geometry is settled.
    """
    graph = parse_metro_mermaid(_bare_fan(3))
    compute_layout(graph, validate=False)
    _seat_diamond_hub_off_centre(graph, "hub", branch_ids=("b0", "b1", "b2"))

    graph._defer_final_guards = True
    _guard_fork_join_hub_centreline_agree(graph, "test")

    graph._defer_final_guards = False
    with pytest.raises(PhaseInvariantError, match="disagree on centreline"):
        _guard_fork_join_hub_centreline_agree(graph, "test")


_RAIL_FAN = (
    Path(__file__).parent.parent
    / "examples"
    / "topologies"
    / "rail_symmetric_fork_join_spans.mmd"
)


def test_rail_fork_and_join_centre_on_their_own_rail_spans() -> None:
    """A rail-laid station's Y is the centre of the rail span it carries, so a
    fork and join carrying different line sets have different centres.

    ``bqsr`` also carries ``core`` (the line arriving from the upstream
    section), spanning four rails; ``merge`` carries only the three caller
    lines. Both are drawn as pills capping their own span, and forcing them
    onto one shared Y would leave one pill off the rails it caps.
    """
    graph = parse_metro_mermaid(_RAIL_FAN.read_text())
    compute_layout(graph, validate=True)

    bqsr = graph.stations["bqsr"]
    merge = graph.stations["merge"]
    for station in (bqsr, merge):
        span = station.rail_used_ys
        assert len(span) > 1
        assert station.y == pytest.approx((min(span) + max(span)) / 2.0, abs=0.01)

    assert len(bqsr.rail_used_ys) == 4
    assert len(merge.rail_used_ys) == 3
    assert bqsr.y != pytest.approx(merge.y, abs=1.0)
