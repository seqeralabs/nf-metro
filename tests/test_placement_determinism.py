"""Section placement must be a deterministic function of graph structure.

Auto-layout's convergence-based row split (``_detect_convergence_split``) feeds
its companion/stacked-sibling migration loops from ``col_assign``, whose key
order in the real engine comes from a BFS over set-valued successor maps -- and
set iteration order varies with ``PYTHONHASHSEED``.  If a migration loop tests
membership against the *growing* return set, whether a section is pulled onto
the return row can cascade off the order its companion happened to be visited
in, making the whole grid placement hash-seed dependent (observed on
``genomeassembly`` under a lowered ``fold_threshold``: the same input flips
between a backward-feed abort and a route-through-box).

The result must depend only on structure, never on iteration order.
"""

from __future__ import annotations

import itertools

import pytest

from nf_metro.layout.auto_layout import _detect_convergence_split


def _reorder(d: dict, order: list[str]) -> dict:
    """A dict with the same items, re-inserted in ``order`` (rest appended)."""
    rest = [k for k in d if k not in order]
    return {k: d[k] for k in [*order, *rest]}


# A convergence sink ``C`` (preds spanning cols 0 and 2) with a tail ``G``.
# ``P`` is a genuine companion (feeds only into {C,G}, shares pred ``A``).
# ``Q`` feeds only into ``P`` -- it can be reached as a companion ONLY by
# cascading off ``P`` already being in the return set, so an order-dependent
# loop includes it for some visit orders and not others.
_CASCADE_SUCC = {
    "A": {"C", "P", "Q"},
    "M": {"C"},
    "Q": {"P"},
    "P": {"C"},
    "C": {"G"},
    "G": set(),
}
_CASCADE_COL = {"A": 0, "M": 0, "Q": 1, "P": 2, "C": 3, "G": 4}


def _predecessors(succ: dict[str, set[str]]) -> dict[str, set[str]]:
    pred: dict[str, set[str]] = {k: set() for k in succ}
    for s, tgts in succ.items():
        for t in tgts:
            pred.setdefault(t, set()).add(s)
    return pred


def _col_groups(col: dict[str, int]) -> dict[int, list[str]]:
    groups: dict[int, list[str]] = {}
    for sid, c in col.items():
        groups.setdefault(c, []).append(sid)
    return groups


_SECTIONS = sorted(_CASCADE_COL)


_PERMUTATIONS = list(itertools.islice(itertools.permutations(_SECTIONS), 24))


def test_convergence_split_excludes_cascade_only_companion():
    """A companion reachable only via another companion stays off the return row.

    ``Q`` feeds only into ``P`` (itself only a companion), so it qualifies for
    the return row solely by cascading off ``P``'s inclusion -- the membership
    test must read the frozen base, where ``P`` is absent, and leave ``Q`` out.
    """
    result = _detect_convergence_split(
        _CASCADE_COL,
        _col_groups(_CASCADE_COL),
        _CASCADE_SUCC,
        _predecessors(_CASCADE_SUCC),
    )
    assert result is not None
    assert "Q" not in result
    assert {"C", "G", "P"} <= result


@pytest.mark.parametrize("order", _PERMUTATIONS)
def test_convergence_split_is_order_independent(order):
    """``_detect_convergence_split`` returns the same set under any key order."""
    pred = _predecessors(_CASCADE_SUCC)
    groups = _col_groups(_CASCADE_COL)

    baseline = _detect_convergence_split(
        _reorder(_CASCADE_COL, _SECTIONS), groups, _CASCADE_SUCC, pred
    )
    permuted = _detect_convergence_split(
        _reorder(_CASCADE_COL, order), groups, _CASCADE_SUCC, pred
    )
    assert permuted == baseline, (
        f"return set depends on col_assign key order: {order} -> {permuted} "
        f"vs canonical {baseline}"
    )
