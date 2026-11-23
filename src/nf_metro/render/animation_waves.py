"""Which routes the animated dots of one metro line take, wave by wave.

A *route* is a full path from a root of the line to one of its sinks, and a
*wave* is the set of routes dots are sent down together.  Where a fork's
branches rejoin downstream the dots take turns: each wave sends one dot down
one alternative and the next wave moves on to the next, so every alternative
gets an equal share instead of one dot per path.  Where branches never rejoin
there is nothing to share and every branch gets a dot in every wave.

Branches are grouped by where they first meet again (ranked by a stable
topological order).  Branches meeting at the same node form one turn-taking
level with its own rotation counter; a group that meets another only further
downstream becomes one alternative of that later level, so levels nest.
Groups that never meet are cloned across.  A virtual root joins the real roots
of the line, so roots that merge take turns and roots that never merge each
start a dot.
"""

from __future__ import annotations

__all__ = ["LineWaves", "Route", "plan_line_waves"]

from dataclasses import dataclass, replace
from functools import reduce
from operator import or_

import networkx as nx

from nf_metro.parser.model import Edge

Route = tuple[Edge, ...]

# Waves simulated looking for the point where every rotation counter is back at
# zero together.  A line whose rotations only realign later is covered by the
# number of waves it takes to use every edge instead.
_REALIGN_LIMIT = 12

# Backstop for the cover search; every edge is used well within a round of the
# largest rotations, so reaching it means a malformed plan.
_COVER_LIMIT = 100_000

_VIRTUAL_ROOT = "\0root"


@dataclass(frozen=True)
class _Leg:
    """A step to *target*; ``edge`` is ``None`` for the virtual root's steps."""

    target: str
    edge: Edge | None


@dataclass(frozen=True)
class _Turn:
    """Alternatives that rejoin, taken in rotation by counter ``counter``."""

    counter: int
    options: tuple[_Leg | _Turn, ...]


@dataclass(frozen=True, eq=False)
class LineWaves:
    """The wave plan of one line.

    ``round_waves`` is the number of waves after which the pattern repeats
    (when every rotation realigns within a dozen waves) or, failing that, the
    number that uses every edge of the line at least once.
    """

    forks: dict[str, tuple[_Leg | _Turn, ...]]
    turn_sizes: tuple[int, ...]
    edge_keys: frozenset[tuple[str, str]]
    round_waves: int

    def waves(self, count: int) -> list[list[Route]]:
        """Return the first *count* waves, every rotation starting at its first
        alternative."""
        counters = [0] * len(self.turn_sizes)
        return [self._wave(counters) for _ in range(count)]

    def _wave(self, counters: list[int]) -> list[Route]:
        routes: list[Route] = []

        def walk(node: str, prefix: Route) -> None:
            branches = self.forks.get(node)
            if not branches:
                if prefix:
                    routes.append(prefix)
                return
            for branch in branches:
                leg = branch
                while isinstance(leg, _Turn):
                    taken = counters[leg.counter]
                    counters[leg.counter] = (taken + 1) % len(leg.options)
                    leg = leg.options[taken]
                step = (leg.edge,) if leg.edge is not None else ()
                walk(leg.target, prefix + step)

        walk(_VIRTUAL_ROOT, ())
        return routes


def plan_line_waves(edges: list[Edge]) -> LineWaves | None:
    """Plan the dot waves of one line's *edges*; ``None`` if it has no root."""
    unique: dict[tuple[str, str], Edge] = {}
    for edge in edges:
        unique.setdefault((edge.source, edge.target), edge)
    targets = {target for _, target in unique}
    roots = sorted({source for source, _ in unique} - targets)
    if not roots:
        return None

    children: dict[str, list[_Leg]] = {_VIRTUAL_ROOT: [_Leg(r, None) for r in roots]}
    for (source, target), edge in unique.items():
        children.setdefault(source, []).append(_Leg(target, edge))
    children = _without_back_edges(children)

    graph: nx.DiGraph[str] = nx.DiGraph()
    for node, legs in children.items():
        graph.add_node(node)
        graph.add_edges_from((node, leg.target) for leg in legs)
    order = list(nx.lexicographical_topological_sort(graph, key=str))
    rank = {node: i for i, node in enumerate(order)}
    reach: dict[str, int] = {}
    for node in reversed(order):
        mask = 1 << rank[node]
        for leg in children.get(node, ()):
            mask |= reach[leg.target]
        reach[node] = mask

    turn_sizes: list[int] = []
    forks = {
        node: _group_branches(children[node], reach, turn_sizes)
        for node in order
        if children.get(node)
    }
    kept = frozenset(
        (leg.edge.source, leg.edge.target)
        for steps in children.values()
        for leg in steps
        if leg.edge is not None
    )
    plan = LineWaves(forks, tuple(turn_sizes), kept, 1)
    return replace(plan, round_waves=_round_waves(plan))


def _without_back_edges(
    children: dict[str, list[_Leg]],
) -> dict[str, list[_Leg]]:
    """Drop the edges that close a cycle, so every line is a DAG."""
    done: set[str] = set()
    on_path = [_VIRTUAL_ROOT]
    on_path_set = {_VIRTUAL_ROOT}
    resume = [0]
    kept: dict[str, list[_Leg]] = {}

    while on_path:
        node = on_path[-1]
        legs = children.get(node, ())
        if resume[-1] == len(legs):
            on_path.pop()
            on_path_set.discard(node)
            resume.pop()
            done.add(node)
            continue
        leg = legs[resume[-1]]
        resume[-1] += 1
        if leg.target in on_path_set:
            continue
        kept.setdefault(node, []).append(leg)
        if leg.target not in done:
            on_path.append(leg.target)
            on_path_set.add(leg.target)
            resume.append(0)
    return kept


def _group_branches(
    legs: list[_Leg], reach: dict[str, int], turn_sizes: list[int]
) -> tuple[_Leg | _Turn, ...]:
    """Group a fork's *legs* by where they rejoin; return the branches to clone.

    Groups meeting at the lowest-ranked shared node merge first, each merge
    being one rotation, until the remaining groups share no node at all.
    """
    groups: list[tuple[_Leg | _Turn, int]] = [(leg, reach[leg.target]) for leg in legs]
    while len(groups) > 1:
        shared = {
            (i, j): groups[i][1] & groups[j][1]
            for i in range(len(groups))
            for j in range(i + 1, len(groups))
        }
        # The lowest set bit of a mask is the lowest-ranked node it holds.
        meets = {
            pair: (mask & -mask).bit_length() - 1
            for pair, mask in shared.items()
            if mask
        }
        if not meets:
            break
        lowest = min(meets.values())
        joined: nx.Graph[int] = nx.Graph()
        joined.add_nodes_from(range(len(groups)))
        joined.add_edges_from(pair for pair, at in meets.items() if at == lowest)

        merged: list[tuple[_Leg | _Turn, int]] = []
        for component in nx.connected_components(joined):
            indices = sorted(component)
            if len(indices) == 1:
                merged.append(groups[indices[0]])
                continue
            options = tuple(groups[i][0] for i in indices)
            turn = _Turn(len(turn_sizes), options)
            turn_sizes.append(len(options))
            merged.append((turn, reduce(or_, (groups[i][1] for i in indices))))
        groups = merged
    return tuple(branch for branch, _ in groups)


def _round_waves(plan: LineWaves) -> int:
    """Return how many waves make one round of *plan*."""
    counters = [0] * len(plan.turn_sizes)
    for count in range(1, _REALIGN_LIMIT + 1):
        plan._wave(counters)
        if not any(counters):
            return count

    counters = [0] * len(plan.turn_sizes)
    unused = set(plan.edge_keys)
    for count in range(1, _COVER_LIMIT + 1):
        for route in plan._wave(counters):
            unused.difference_update((e.source, e.target) for e in route)
        if not unused:
            return count
    return _COVER_LIMIT
