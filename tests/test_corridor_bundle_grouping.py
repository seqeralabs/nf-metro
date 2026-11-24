"""Legs that co-travel are grouped, however far apart the ordered scan looks.

:func:`~nf_metro.layout.routing.normalize._corridor_bundles` walks one axis's
legs in coordinate order and stops testing a leg once no later one can reach it.
That cutoff and the predicate deciding whether a pair co-travels are the same
distance, so a pair the predicate would group is never skipped by the scan.  When
the two disagreed, a pair further apart than the cutoff but closer than the
predicate's reach was left ungrouped and free to move independently, which
destroys the spacing that draws them as separate strokes.

Being close laterally is only half of it: the pair also has to share a stretch
of corridor, read the same way a peer's claim on the gap is read, so two legs
handing over across an elbow stay free of each other.
"""

from __future__ import annotations

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    OFFSET_STEP,
)
from nf_metro.layout.pass_metrics import (
    min_corridor_y_overlap,
)
from nf_metro.layout.routing.normalize import (
    _corridor_bundles,
    _CorridorRun,
    _legs_co_travel,
)


def _run(coordinate: float, run: tuple[float, float] = (0.0, 100.0)) -> _CorridorRun:
    """A movable horizontal leg at *coordinate*, spanning *run* along its corridor."""
    return _CorridorRun(
        route=None,  # type: ignore[arg-type] - grouping reads only geometry
        idx=1,
        axis=1,
        coordinate=coordinate,
        run_lo=run[0],
        run_hi=run[1],
        lo=coordinate - 50.0,
        hi=coordinate + 50.0,
        forward=True,
    )


def test_a_pair_past_the_offset_step_still_groups() -> None:
    """The gap that exposed the disagreement: wider than a step, inside the reach.

    ``BUNDLE_TO_BUNDLE_CLEARANCE`` is what separates two bundles, so a pair
    closer than that is one bundle no matter that it is more than one
    ``OFFSET_STEP`` apart.
    """
    separation = BUNDLE_TO_BUNDLE_CLEARANCE - OFFSET_STEP
    assert separation > OFFSET_STEP, "the case needs a gap wider than one step"
    first, second = _run(0.0), _run(separation)
    assert _legs_co_travel(first, second, OFFSET_STEP)
    assert [
        len(bundle) for bundle in _corridor_bundles([first, second], OFFSET_STEP)
    ] == [2]


def test_a_pair_beyond_the_reach_stays_apart() -> None:
    """Two bundles a full clearance apart are separate and move independently."""
    first, second = _run(0.0), _run(BUNDLE_TO_BUNDLE_CLEARANCE * 2)
    assert not _legs_co_travel(first, second, OFFSET_STEP)
    assert [
        len(bundle) for bundle in _corridor_bundles([first, second], OFFSET_STEP)
    ] == [1, 1]


def test_legs_meeting_only_across_an_elbow_stay_apart() -> None:
    """A pair inside the reach that hands over at a corner is two bundles.

    Legs whose runs meet only across the elbow band that joins them occupy
    different stretches of the gap and owe each other no clearance, so each is
    free to travel to the coordinate its own claim was widened for.
    """
    first = _run(0.0)
    second = _run(
        BUNDLE_TO_BUNDLE_CLEARANCE - OFFSET_STEP,
        (100.0 - min_corridor_y_overlap(), 200.0),
    )
    assert not _legs_co_travel(first, second, OFFSET_STEP)
    assert [
        len(bundle) for bundle in _corridor_bundles([first, second], OFFSET_STEP)
    ] == [1, 1]


def test_grouping_is_transitive_across_the_scan() -> None:
    """A chain of pairs each inside the reach is one bundle end to end.

    The middle leg is beyond the first's reach only by way of the third, so a
    scan that stopped early would split the chain rather than shorten it.
    """
    step = OFFSET_STEP
    coordinates = [
        0.0,
        BUNDLE_TO_BUNDLE_CLEARANCE - step,
        2 * (BUNDLE_TO_BUNDLE_CLEARANCE - step),
    ]
    bundles = _corridor_bundles([_run(item) for item in coordinates], step)
    assert [len(bundle) for bundle in bundles] == [3]
