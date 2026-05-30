"""Tests for the declarative maintained-invariants mechanism (#365).

Covers the ``maintain`` priority-ordered fixpoint driver, the
``junctions_track_ports`` invariant's predicate/repair pair, and the
end-to-end property that every junction satisfies the invariant after a
full layout - parametrised across the multi-section corpus so the rule
generalises beyond a single fixture.
"""

from __future__ import annotations

import pytest
from test_layout_invariants import _FIXTURES_MULTI_SECTION, _layout

from nf_metro.layout.phases.guards import PhaseInvariantError
from nf_metro.layout.phases.junctions import _compute_junction_xy
from nf_metro.layout.phases.maintained import (
    JUNCTIONS_TRACK_PORTS,
    MaintainedInvariant,
    _junctions_track_ports_holds,
    assert_maintained,
    maintain,
)

# ---------------------------------------------------------------------------
# maintain() driver
# ---------------------------------------------------------------------------


def _counter_invariant(name, priority, log, *, breaks=None):
    """Build a toy invariant whose repair flips a flag and records its name.

    ``breaks`` is a list of state keys this repair sets back to violated,
    used to exercise the fixpoint re-application across priorities.
    """

    def predicate(state):
        return state.get(name, False)

    def repair(state):
        log.append(name)
        state[name] = True
        for k in breaks or ():
            state[k] = False

    return MaintainedInvariant(
        name=name,
        priority=priority,
        predicate=predicate,
        repair=repair,
        description=name,
    )


def test_maintain_applies_repairs_in_priority_order():
    log: list[str] = []
    state: dict = {}
    invs = [
        _counter_invariant("c", 30, log),
        _counter_invariant("a", 10, log),
        _counter_invariant("b", 20, log),
    ]
    maintain(state, invs)
    assert log == ["a", "b", "c"]


def test_maintain_skips_satisfied_invariants():
    log: list[str] = []
    state = {"a": True, "b": False}
    invs = [
        _counter_invariant("a", 10, log),
        _counter_invariant("b", 20, log),
    ]
    maintain(state, invs)
    assert log == ["b"]  # a already held, only b repaired


def test_maintain_reaches_fixpoint_when_repair_breaks_lower_priority():
    # 'a' (priority 10) breaks 'b' (priority 20) each time it repairs; the
    # loop must re-run 'b' after 'a', then settle.
    log: list[str] = []
    state: dict = {}
    invs = [
        _counter_invariant("a", 10, log, breaks=["b"]),
        _counter_invariant("b", 20, log),
    ]
    maintain(state, invs)
    # pass1: a (sets a, breaks b), b (sets b). pass2: nothing -> settle.
    assert log == ["a", "b"]
    assert state == {"a": True, "b": True}


def test_maintain_raises_on_non_convergence():
    # Two repairs that perpetually break each other never settle.
    log: list[str] = []
    state: dict = {}
    invs = [
        _counter_invariant("a", 10, log, breaks=["b"]),
        _counter_invariant("b", 20, log, breaks=["a"]),
    ]
    with pytest.raises(RuntimeError, match="did not converge"):
        maintain(state, invs, max_passes=3)


# ---------------------------------------------------------------------------
# junctions_track_ports invariant
# ---------------------------------------------------------------------------


def _first_fanout_or_merge_junction(graph):
    for jid in graph.junctions:
        if _compute_junction_xy(graph, jid) is not None:
            return jid
    return None


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_junction_invariant_holds_after_layout(fixture):
    """Every resolvable junction sits at its computed target post-layout."""
    graph = _layout(fixture)
    assert _junctions_track_ports_holds(graph)


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_junction_predicate_detects_and_repair_fixes_staleness(fixture):
    """Perturbing a junction violates the predicate; the repair restores it."""
    graph = _layout(fixture)
    jid = _first_fanout_or_merge_junction(graph)
    if jid is None:
        pytest.skip("fixture has no resolvable junction")

    graph.stations[jid].y += 137.0  # move it well past the FP epsilon
    assert not _junctions_track_ports_holds(graph)

    JUNCTIONS_TRACK_PORTS.repair(graph)
    assert _junctions_track_ports_holds(graph)


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_junction_repair_is_idempotent(fixture):
    """Re-running the repair when the invariant holds changes nothing."""
    graph = _layout(fixture)

    def snapshot():
        return {
            jid: (graph.stations[jid].x, graph.stations[jid].y)
            for jid in graph.junctions
        }

    before = snapshot()
    JUNCTIONS_TRACK_PORTS.repair(graph)
    assert snapshot() == before


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_assert_maintained_raises_on_violation(fixture):
    """The runtime guard raises (with the invariant name) when perturbed."""
    graph = _layout(fixture)
    # Holds straight after layout.
    assert_maintained(graph, [JUNCTIONS_TRACK_PORTS], "test")

    jid = _first_fanout_or_merge_junction(graph)
    if jid is None:
        pytest.skip("fixture has no resolvable junction")
    graph.stations[jid].y += 137.0
    with pytest.raises(PhaseInvariantError, match="junctions_track_ports"):
        assert_maintained(graph, [JUNCTIONS_TRACK_PORTS], "test")
