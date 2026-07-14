"""Seam-orientation classifier: does an inter-section seam preserve or reverse
the delivered bundle order?

A *seam* is a feeding exit port and the entry port it feeds (possibly through a
junction). :func:`seam_orientation` returns whether the bundle order a feeder
delivers arrives at the consumer **preserved** or **reversed**, derived purely
from port sides and grid topology (rows, columns, section directions,
intervening sections, junction mediation) -- no offset or coordinate state.

The verdict is a pure function of the local seam geometry: a straight run or
staircase preserves order; a U-turn or half-turn reverses it. Five geometric
idioms reverse:

* an over-the-top RIGHT entry (a same-row feeder loops over the section top and
  drops in from the right -- a U-turn),
* an around-below LEFT entry (a far-side LEFT-exit feeder drops below every box
  and rises into the outward side -- a half-turn),
* a vertical column continuation (a vertical section's BOTTOM exit feeding a TOP
  entry, whose down-flowing bundle the section already carries reversed),
* a fold RIGHT entry (a BOTTOM exit turned into a RIGHT entry through a fold
  junction -- a down-then-turn concentric corner), and
* a fold turn into a RIGHT entry across rows or through a junction (a LEFT exit
  wrapping rightward into the next row).

Two kinds of reversal are out of scope for this seam-local, coordinate-free
primitive, and the oracle test pins them as documented residuals:

* **Propagated / conditional reversal** -- section-absolute reversal that the
  legacy machinery smears *along a row* (``_propagate_reversal_along_rows``) and
  the conditional TB LEFT/RIGHT-exit case (``_is_tb_lr_exit_nonreversed``). The
  same seam geometry preserves or reverses depending on whether an upstream
  section was itself reversed, so it cannot be a function of one seam; the
  transposition already rides in the delivered bundle order.
* **Near-vertical junction RIGHT entry** -- a multi-line fan-out junction that
  overhangs a RIGHT entry's outward edge and drops near-vertically into it
  (``is_near_vertical_junction_right_entry``). Whether the drop transposes turns
  on the junction's pixel overhang, not on sides or grid rows/columns, so it is
  deferred rather than reproduced coordinate-free here.
"""

from __future__ import annotations

from enum import Enum

from nf_metro.layout.geometry import AxisFrame
from nf_metro.layout.routing.context import _has_intervening_sections
from nf_metro.parser.model import MetroGraph, Port, PortSide, Section


def _flow_runs_vertically(direction: str) -> bool:
    """Whether a section's flow axis is vertical (top-down or bottom-up)."""
    return AxisFrame.axes_for_direction(direction)[0] == "y"


class SeamOrientation(Enum):
    """Whether a seam keeps or transposes the delivered bundle order."""

    PRESERVE = "preserve"
    REVERSE = "reverse"


def seam_orientation(
    graph: MetroGraph, exit_port: Port, entry_port: Port
) -> SeamOrientation:
    """Classify whether the seam ``exit_port -> entry_port`` reverses bundle order.

    ``exit_port`` is the feeding exit port (resolved through any intervening
    fold junction); ``entry_port`` is the consumer's entry port. Returns
    :attr:`SeamOrientation.REVERSE` when the seam geometry is a U-turn or
    half-turn that transposes the bundle end to end, else
    :attr:`SeamOrientation.PRESERVE`.
    """
    feeder = graph.section_for_port(exit_port)
    consumer = graph.section_for_port(entry_port)
    if _reverses(graph, exit_port, entry_port, feeder, consumer):
        return SeamOrientation.REVERSE
    return SeamOrientation.PRESERVE


def _reverses(
    graph: MetroGraph,
    exit_port: Port,
    entry_port: Port,
    feeder: Section,
    consumer: Section,
) -> bool:
    if (
        _is_over_top_right_entry(exit_port, entry_port, feeder, consumer)
        or _is_around_below_left_entry(graph, exit_port, entry_port, feeder, consumer)
        or _is_vertical_column_continuation(exit_port, entry_port, feeder, consumer)
    ):
        return True
    # Only the junction idioms need the (graph-walking) junction-mediation check,
    # so defer it past the O(1) side/grid idioms above.
    via_junction = _seam_via_junction(graph, exit_port, entry_port)
    return _is_fold_right_entry(exit_port, entry_port, via_junction) or (
        _is_fold_turn_right_entry(exit_port, entry_port, feeder, consumer, via_junction)
    )


def _seam_via_junction(graph: MetroGraph, exit_port: Port, entry_port: Port) -> bool:
    """Whether *this* feeder reaches *this* entry through a junction.

    Specific to the seam: the entry must be fed by a junction that the exit
    port itself feeds. An entry with an unrelated junction feeder does not make
    a direct exit-to-entry seam count as junction-mediated.
    """
    junction_ids = graph.junction_ids
    junction_preds = {
        edge.source
        for edge in graph.edges_to(entry_port.id)
        if edge.source in junction_ids
    }
    if not junction_preds:
        return False
    return any(edge.target in junction_preds for edge in graph.edges_from(exit_port.id))


def _is_over_top_right_entry(
    exit_port: Port, entry_port: Port, feeder: Section, consumer: Section
) -> bool:
    """U-turn: a same-row feeder loops over a TB section's top into a RIGHT entry.

    The feeder sits in the same grid row, no more than one column away and not to
    the consumer's right, so the line wraps over the top and approaches from the
    right -- transposing the bundle.
    """
    return (
        entry_port.side is PortSide.RIGHT
        and _flow_runs_vertically(consumer.direction)
        and feeder.grid_row == consumer.grid_row
        and abs(consumer.grid_col - feeder.grid_col) <= 1
        and feeder.grid_col <= consumer.grid_col
        and not exit_port.is_entry
    )


def _is_around_below_left_entry(
    graph: MetroGraph,
    exit_port: Port,
    entry_port: Port,
    feeder: Section,
    consumer: Section,
) -> bool:
    """Half-turn: a far-side LEFT exit wraps below into a LEFT entry.

    A LEFT entry fed by a LEFT exit more than one column to its right, with an
    intervening section on either row, is a reverse-flow bypass that drops below
    every box and rises into the outward side -- transposing the bundle.
    """
    if not (
        entry_port.side is PortSide.LEFT
        and exit_port.side is PortSide.LEFT
        and not exit_port.is_entry
    ):
        return False
    if feeder.grid_col - consumer.grid_col <= 1:
        return False
    return _has_intervening_sections(
        graph, feeder.grid_col, consumer.grid_col, feeder.grid_row
    ) or _has_intervening_sections(
        graph, feeder.grid_col, consumer.grid_col, consumer.grid_row
    )


def _is_vertical_column_continuation(
    exit_port: Port, entry_port: Port, feeder: Section, consumer: Section
) -> bool:
    """A vertical section's BOTTOM exit feeding a horizontal section's TOP entry.

    The vertical drop delivers lines in ``x + sign * offset`` order; a
    horizontal receiver draws its bundle along Y and needs the offset order
    mirrored to keep the perp-entry corner concentric.  A vertical receiver
    (TB/BT) shares the same flow axis and the same sign, so the two sections
    are a straight column continuation -- no mirroring needed there.
    """
    return (
        exit_port.side is PortSide.BOTTOM
        and entry_port.side is PortSide.TOP
        and _flow_runs_vertically(feeder.direction)
        and not _flow_runs_vertically(consumer.direction)
        and not exit_port.is_entry
    )


def _is_fold_right_entry(exit_port: Port, entry_port: Port, via_junction: bool) -> bool:
    """A BOTTOM exit turned into a RIGHT entry through a fold junction.

    The exit drops the bundle vertically and the fold junction turns it into the
    RIGHT entry; that down-then-turn concentric corner transposes the bundle.
    """
    return (
        via_junction
        and exit_port.side is PortSide.BOTTOM
        and entry_port.side is PortSide.RIGHT
        and not exit_port.is_entry
    )


def _is_fold_turn_right_entry(
    exit_port: Port,
    entry_port: Port,
    feeder: Section,
    consumer: Section,
    via_junction: bool,
) -> bool:
    """A LEFT exit wrapping rightward into a RIGHT entry across rows or a junction.

    A LEFT exit reaching a RIGHT entry is a reverse-flow fold; when it crosses
    rows (drops into the row below) or routes through a junction it is a turn
    that transposes the bundle. A same-row LEFT-to-RIGHT continuation is a
    straight run and is excluded (its reversal, when present, is propagated from
    an upstream fold and is a documented residual).
    """
    return (
        exit_port.side is PortSide.LEFT
        and entry_port.side is PortSide.RIGHT
        and not exit_port.is_entry
        and (via_junction or feeder.grid_row != consumer.grid_row)
    )
