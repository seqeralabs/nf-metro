"""Pick the fold_threshold whose layout best matches a target aspect ratio.

A metro map's aspect ratio is emergent: it falls out of the section topology
and how the auto-layout folds long section rows onto serpentine bands. There
is no continuous dial, only the discrete set of layouts the folder can produce.
This module brackets that set - given the candidate fold thresholds (from
:func:`nf_metro.layout.auto_layout.candidate_fold_thresholds`) and a way to
measure each one's rendered dimensions - and returns the layout closest to a
requested width/height ratio. The caller does the rendering; the search itself
is pure and side-effect free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import log


@dataclass(frozen=True)
class AspectSolution:
    """Outcome of an aspect-ratio search.

    ``adjustable`` is False when fold_threshold cannot reshape the layout - a
    single section, no inter-section edges, an author-pinned grid, or a
    topology whose shape is the same at every candidate fold. The other fields
    are then None and the caller should render with its default fold_threshold.
    """

    adjustable: bool
    target: float
    fold_threshold: int | None = None
    achieved_aspect: float | None = None
    achieved_dims: tuple[int, int] | None = None


def solve_aspect(
    target: float,
    candidates: list[int],
    measure: Callable[[int], tuple[float, float]],
) -> AspectSolution:
    """Find the candidate fold_threshold whose aspect ratio is closest to *target*.

    *measure* maps a fold_threshold to the resulting ``(width, height)``.
    Closeness is judged in log space so that being twice too wide and twice
    too tall are penalised equally. Candidates that render to identical
    dimensions collapse to the smallest fold_threshold among them, and the
    smallest fold_threshold also breaks exact-distance ties (a deterministic,
    less-folded-wins rule).

    When every candidate renders to the same dimensions the fold_threshold has
    no effect on this topology (its shape is driven by convergence or
    tall-anchor placement, not folding); the result is ``adjustable=False`` so
    the caller can say the target cannot be honoured.
    """
    log_target = log(target)
    seen: set[tuple[int, int]] = set()
    best_distance: float | None = None
    best_fold: int | None = None
    best_aspect = 0.0
    best_dims = (0, 0)

    for fold in sorted(candidates):
        width, height = measure(fold)
        if width <= 0 or height <= 0:
            continue
        dims = (round(width), round(height))
        if dims in seen:
            continue
        seen.add(dims)
        aspect = dims[0] / dims[1]
        distance = abs(log(aspect) - log_target)
        if best_distance is None or distance < best_distance:
            best_distance, best_fold, best_aspect, best_dims = (
                distance,
                fold,
                aspect,
                dims,
            )

    if best_fold is None or len(seen) < 2:
        return AspectSolution(adjustable=False, target=target)

    return AspectSolution(
        adjustable=True,
        target=target,
        fold_threshold=best_fold,
        achieved_aspect=best_aspect,
        achieved_dims=best_dims,
    )
