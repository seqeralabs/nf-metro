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
each invariant declares a *predicate* (does it currently hold?), a *repair*
(re-establish it), and a *priority* (lower repairs first).  ``maintain``
applies the repairs in priority order until a full pass makes no change
(fixpoint), so a single ``maintain(graph)`` call after each constructive
phase subsumes the scattered manual re-runs - and a new phase needs no
bookkeeping, because the invariant catches its perturbation automatically.

This is deliberately *not* a constraint solver (cf. #351 / #353): repairs
are the existing constructive helpers, applied in a declared order, not
weak numeric attractors relaxed to equilibrium.  The fixpoint loop only
re-applies a repair whose predicate a higher-priority repair just broke.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from nf_metro.layout.phases.bbox import _min_section_bbox_top
from nf_metro.layout.phases.canvas import _shift_graph_into_canvas
from nf_metro.layout.phases.guards import PhaseInvariantError
from nf_metro.layout.phases.junctions import _compute_junction_xy, _position_junctions
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

    - ``predicate(graph)`` returns ``True`` when the invariant holds.
    - ``repair(graph)`` re-establishes it; must be idempotent (a no-op
      when the predicate already holds) so repeated application converges.
    - ``priority`` orders repairs within a ``maintain`` pass: lower runs
      first, so an invariant that *feeds* another (e.g. a whole-canvas
      translate that other repairs read) gets a lower number.
    """

    name: str
    priority: int
    predicate: Callable[[MetroGraph], bool]
    repair: Callable[[MetroGraph], None]
    description: str


def maintain(
    graph: MetroGraph,
    invariants: list[MaintainedInvariant],
    *,
    max_passes: int = 8,
) -> None:
    """Apply each invariant's repair in priority order until a fixpoint.

    One pass walks the invariants by ascending ``priority``, repairing any
    whose predicate is violated.  Because a repair can break a
    lower-priority invariant that an earlier pass already satisfied, the
    walk repeats until a full pass triggers no repair.

    ``max_passes`` is a loud backstop: if the invariants don't converge it
    means two repairs are fighting (the #353 failure shape), which must
    surface as an error rather than a silently truncated layout.
    """
    ordered = sorted(invariants, key=lambda inv: inv.priority)
    for _ in range(max_passes):
        repaired = False
        for inv in ordered:
            if not inv.predicate(graph):
                inv.repair(graph)
                repaired = True
        if not repaired:
            return
    unstable = [inv.name for inv in ordered if not inv.predicate(graph)]
    raise RuntimeError(
        f"maintained invariants did not converge in {max_passes} passes; "
        f"still violated: {unstable}. Two repairs are likely fighting - "
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

    Compares each junction's stored ``(x, y)`` against the pure
    ``_compute_junction_xy`` target.  A mismatch means a port moved since
    the junction was last positioned.
    """
    for jid in graph.junctions:
        junction = graph.stations.get(jid)
        if not junction:
            continue
        target = _compute_junction_xy(graph, jid)
        if target is None:
            continue
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
