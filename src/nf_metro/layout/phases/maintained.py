"""Declarative maintained invariants for the layout pipeline.

Spike (#365): the layout pipeline keeps a handful of *invariants* alive
across the long Pass-C settling sequence by manually re-running "restore"
helpers after every constructive phase that might perturb them.  The most
visible case is junction positioning: ``_position_junctions`` is re-called
at six different stages purely because some preceding phase moved an exit
or entry port.  The order ("run the restore after the thing that breaks
it") lives implicitly in the call sequence, so adding a new port-moving
phase silently regresses any junction whose restore the author forgot to
re-trigger (cf. #386).

This module lifts that implicit "restore-after-break" ordering into data:
each invariant declares a *repair* (re-establish it), a *priority* (lower
repairs first), and a *predicate* (does it currently hold?).  ``maintain``
runs every repair in priority order, repeating until a full pass changes
nothing (a fixpoint), so one ``maintain(graph)`` call after each
constructive phase subsumes the scattered manual re-runs - and a new phase
needs no bookkeeping, because the repairs re-fire automatically.

The repairs are idempotent (a no-op once their property holds), so running
them unconditionally and detecting convergence by *change* - rather than by
re-checking a predicate first - keeps the mechanism uniform: every restore
is the same kind of object, regardless of whether a cheap exact "is it
satisfied?" predicate exists for it.  The predicate is used only by
``assert_maintained`` (the runtime guard); for restores whose exact
predicate would merely re-derive the repair (off-track stacking) it is a
loose necessary check, which is all a guard needs.

This is deliberately *not* a constraint solver (cf. #351 / #353): repairs
are the existing constructive helpers, applied in a declared priority
order, not weak numeric attractors relaxed to equilibrium.  The priority
numbers encode the dependency order that used to live implicitly in the
call sequence (a lower-priority repair feeds a higher one); see
``docs/dev/maintained_invariants_spike.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from nf_metro.layout.phases._common import _row_contiguous_column_groups
from nf_metro.layout.phases.bbox import _min_section_bbox_top
from nf_metro.layout.phases.canvas import _shift_graph_into_canvas
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_off_track_inputs_above_consumer,
)
from nf_metro.layout.phases.junctions import (
    _position_junctions,
    _resolvable_junctions,
)
from nf_metro.layout.phases.off_track import _reanchor_off_track_to_consumer
from nf_metro.layout.phases.row_align import _top_align_row_bboxes_only
from nf_metro.parser.model import MetroGraph

# Predicate comparison epsilon.  The repair (``_position_junctions``)
# snaps each junction to the *exact* ``_compute_junction_xy`` target, and
# the hand-written pipeline re-ran it unconditionally, so the invariant it
# kept is exact equality - a sub-pixel port nudge that moves the target
# still changes the rendered junction.  A wider tolerance would let that
# drift survive and diverge from the legacy layout, so this epsilon only
# absorbs floating-point noise, not real movement.
_MAINTAIN_TOL = 1e-9


@dataclass(frozen=True)
class MaintainedInvariant:
    """A property the pipeline keeps alive across constructive phases.

    - ``repair(graph)`` re-establishes the property; must be idempotent (a
      no-op once it holds) so ``maintain`` can run it unconditionally and
      detect convergence by change.
    - ``priority`` orders repairs within a ``maintain`` pass: lower runs
      first, so a repair that *feeds* another (e.g. a whole-canvas
      translate that a later repair reads) gets a lower number.
    - ``predicate(graph)`` returns ``True`` when the property holds.  Used
      only by ``assert_maintained``; it may be a loose necessary check
      where an exact one would just re-derive the repair.
    """

    name: str
    priority: int
    predicate: Callable[[MetroGraph], bool]
    repair: Callable[[MetroGraph], None]
    description: str


def _y_state_snapshot(graph: MetroGraph) -> tuple:
    """Hashable snapshot of every coordinate a repair can move.

    Covers station x/y (junctions are stations), port y, and section bbox
    top/height - the union of what the maintained repairs mutate.  Used to
    detect whether a ``maintain`` pass changed anything.
    """
    return (
        tuple((sid, st.x, st.y) for sid, st in graph.stations.items()),
        tuple((pid, port.y) for pid, port in graph.ports.items()),
        tuple((kid, sec.bbox_y, sec.bbox_h) for kid, sec in graph.sections.items()),
    )


def maintain(
    graph: MetroGraph,
    invariants: list[MaintainedInvariant],
    *,
    max_passes: int = 8,
    snapshot: Callable[[MetroGraph], object] = _y_state_snapshot,
) -> None:
    """Run every repair in priority order until a pass changes nothing.

    Each pass applies all repairs by ascending ``priority``; because the
    repairs are idempotent, a pass that mutates no coordinate means the set
    has converged.  Running unconditionally (rather than gating on a
    predicate) keeps the mechanism uniform across restores with and without
    a cheap exact "is it satisfied?" predicate.

    ``max_passes`` is a loud backstop: if a pass never stops changing state
    two repairs are fighting (the #353 failure shape), which must surface as
    an error rather than a silently truncated layout.  ``snapshot`` is
    injectable so the driver can be unit-tested against non-graph state.
    """
    ordered = sorted(invariants, key=lambda inv: inv.priority)
    for _ in range(max_passes):
        before = snapshot(graph)
        for inv in ordered:
            inv.repair(graph)
        if snapshot(graph) == before:
            return
    raise RuntimeError(
        f"maintained invariants did not converge in {max_passes} passes: "
        f"{[inv.name for inv in ordered]}. Two repairs are likely fighting - "
        "check their relative priority."
    )


def assert_maintained(
    graph: MetroGraph,
    invariants: list[MaintainedInvariant],
    phase: str,
) -> None:
    """Raise ``PhaseInvariantError`` if any maintained invariant is violated.

    The runtime counterpart to ``maintain``: ``maintain`` *re-establishes*
    the invariants after each constructive phase, this *checks* they still
    hold at a validation boundary.  Catches a future phase added after the
    last ``maintain`` call that perturbs an invariant without re-triggering
    its repair - the exact regression class (#386) this mechanism exists to
    prevent.
    """
    for inv in invariants:
        if not inv.predicate(graph):
            raise PhaseInvariantError(
                f"{phase}: maintained invariant {inv.name!r} violated - "
                f"{inv.description}"
            )


def _junctions_track_ports_holds(graph: MetroGraph) -> bool:
    """True when every junction sits where its ports place it.

    Compares each junction's stored ``(x, y)`` against its computed target.
    A mismatch means a port moved since the junction was last positioned.
    """
    for junction, target in _resolvable_junctions(graph):
        if (
            abs(junction.x - target[0]) > _MAINTAIN_TOL
            or abs(junction.y - target[1]) > _MAINTAIN_TOL
        ):
            return False
    return True


# ``_position_junctions`` overwrites every junction from its current port
# coordinates, reading no stored junction state, so it is the idempotent
# repair for this invariant: a no-op when ports haven't moved.
JUNCTIONS_TRACK_PORTS = MaintainedInvariant(
    name="junctions_track_ports",
    priority=30,
    predicate=_junctions_track_ports_holds,
    repair=_position_junctions,
    description=(
        "Every fan-out / merge junction sits at the position derived from "
        "its exit/entry ports (junction.xy == _compute_junction_xy)."
    ),
)


def canvas_top_margin(section_y_padding: float) -> MaintainedInvariant:
    """Invariant: the topmost section keeps its margin from the canvas top.

    A factory because the margin is a layout parameter, not graph state.
    The repair is a uniform whole-graph translate, so it moves junctions,
    ports, and bboxes together - it can never break a position-relative
    invariant (e.g. junctions stay on their ports).  Priority sits below
    the junction invariant so a translate settles before junctions are
    rechecked (the recheck is then a no-op).
    """

    def holds(graph: MetroGraph) -> bool:
        # Mirror ``_shift_graph_into_canvas``'s own early-return condition
        # exactly so the predicate is true iff the repair is a no-op.
        return _min_section_bbox_top(graph, section_y_padding) >= section_y_padding

    def repair(graph: MetroGraph) -> None:
        _shift_graph_into_canvas(graph, section_y_padding)

    return MaintainedInvariant(
        name="canvas_top_margin",
        priority=20,
        predicate=holds,
        repair=repair,
        description=(
            "The topmost non-empty section's bbox top sits at least "
            "section_y_padding below the canvas origin."
        ),
    )


def _row_bbox_tops_flush_holds(graph: MetroGraph) -> bool:
    """True when each row's contiguous column group has flush bbox tops.

    Mirrors ``_top_align_row_bboxes_only``'s no-op condition exactly: the
    repair grows any section whose ``bbox_y`` exceeds its group min (strict,
    no tolerance), so a group is already flush iff none exceed the min.
    """
    for group in _row_contiguous_column_groups(graph):
        min_top = min(s.bbox_y for s in group)
        if any(s.bbox_y > min_top for s in group):
            return False
    return True


# ``_top_align_row_bboxes_only`` only grows bbox tops up to the group min, so
# it is idempotent (a no-op once tops are flush).
ROW_BBOX_TOPS_FLUSH = MaintainedInvariant(
    name="row_bbox_tops_flush",
    priority=25,
    predicate=_row_bbox_tops_flush_holds,
    repair=_top_align_row_bboxes_only,
    description=(
        "Within each row's contiguous column group, all section bbox tops "
        "are flush (share the group's minimum bbox_y)."
    ),
)


def _off_track_above_consumer_holds(graph: MetroGraph) -> bool:
    """Loose guard predicate: every off-track input sits above its consumer.

    Reuses the existing ``_guard_off_track_inputs_above_consumer`` check
    (a necessary, not exact, condition).  The exact "is it already placed?"
    predicate would re-derive the repair's stacking computation, so this
    invariant relies on ``maintain``'s change-detection for convergence and
    keeps this loose check only for ``assert_maintained``.
    """
    try:
        _guard_off_track_inputs_above_consumer(graph, "maintain")
    except PhaseInvariantError:
        return False
    return True


def off_track_above_consumer(
    y_spacing: float, section_y_padding: float
) -> MaintainedInvariant:
    """Invariant: off-track inputs sit a pitch above their consumer.

    A factory because the repair needs the layout's spacing.  Lowest
    priority: its bbox-grow can breach the canvas margin and unflush a row's
    bbox tops, so ``canvas_top_margin`` (20) and ``row_bbox_tops_flush`` (25)
    must run after it within the same ``maintain`` pass.  A uniform
    whole-graph translate and a bbox-top grow never move an off-track
    station relative to its consumer, so neither feeds back here (the
    dependency order is a DAG).
    """

    def repair(graph: MetroGraph) -> None:
        _reanchor_off_track_to_consumer(graph, y_spacing, section_y_padding)

    return MaintainedInvariant(
        name="off_track_above_consumer",
        priority=10,
        predicate=_off_track_above_consumer_holds,
        repair=repair,
        description=(
            "Each off-track input sits n*y_spacing above its on-track "
            "consumer, inside the section bbox."
        ),
    )
