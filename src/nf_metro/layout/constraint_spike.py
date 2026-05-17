"""Constraint-solver spike post-pass (issue #345).

SPIKE CODE - NOT FOR PRODUCTION USE.

This module exists to drive a visual diff in the CI render preview for
issue #345.  It takes the engine's output and re-solves station / port
Ys using a kiwisolver constraint system encoding the row-Y alignment
constraints catalogued in ``docs/constraint-solver-spike.md``.

The spike branch unconditionally enables this post-pass so every
gallery render shows the solver's chosen Ys instead of the engine's.
Compare against main to see the visual impact.

If/when this lands as a real feature it must be opt-in (env var or CLI
flag) and gated on a quantitative quality rubric.  See the writeup for
the land-or-shelve verdict and the one viable narrow direction.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import kiwisolver as kiwi

from nf_metro.parser.model import MetroGraph, PortSide


def apply_constraint_solver(graph: MetroGraph, y_spacing: float) -> None:
    """Re-solve station / port / bbox Ys via kiwisolver.

    Mutates the graph in place.  See module docstring for context.
    """
    pad = y_spacing / 2.0

    solver = kiwi.Solver()
    variables: dict[str, kiwi.Variable] = {}
    bbox_y_vars: dict[str, kiwi.Variable] = {}

    for sid in graph.stations:
        if sid.startswith("__"):
            continue
        variables[sid] = kiwi.Variable(f"y_{sid}")

    for sec_id in graph.sections:
        bbox_y_vars[sec_id] = kiwi.Variable(f"by_{sec_id}")

    for sec in graph.sections.values():
        solver.addConstraint((bbox_y_vars[sec.id] == sec.bbox_y) | "weak")

    # C1 + C7: containment with variable bbox_y
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        by = bbox_y_vars[sec.id]
        h = sec.bbox_h
        for sid in sec.station_ids:
            if sid not in variables:
                continue
            st = graph.stations.get(sid)
            if st is None:
                continue
            v = variables[sid]
            if st.is_port:
                port = graph.ports.get(sid)
                side = port.side if port else None
                if side == PortSide.TOP:
                    solver.addConstraint((v == by) | "required")
                elif side == PortSide.BOTTOM:
                    solver.addConstraint((v == by + h) | "required")
                else:
                    solver.addConstraint((v >= by + pad) | "required")
                    solver.addConstraint((v <= by + h - pad) | "required")
            else:
                solver.addConstraint((v >= by + pad) | "required")
                solver.addConstraint((v <= by + h - pad) | "required")

    # C2: same-layer ordering (linearised via engine's track sort)
    for sec in graph.sections.values():
        by_layer: dict[int, list[tuple[float, str]]] = defaultdict(list)
        for sid in sec.station_ids:
            if sid.startswith("__"):
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port:
                continue
            layer = getattr(st, "layer", None)
            if layer is None:
                continue
            by_layer[layer].append((st.y, sid))
        for items in by_layer.values():
            if len(items) < 2:
                continue
            items.sort()
            for (_, a), (_, b) in zip(items, items[1:]):
                solver.addConstraint(
                    (variables[b] >= variables[a] + y_spacing) | "required"
                )

    # C3 + C5: edge straightness
    station_to_section: dict[str, str] = {}
    for sec in graph.sections.values():
        for sid in sec.station_ids:
            station_to_section[sid] = sec.id
    for edge in graph.edges:
        if edge.source not in variables or edge.target not in variables:
            continue
        same = station_to_section.get(edge.source) == station_to_section.get(
            edge.target
        )
        strength = "strong" if same else "medium"
        solver.addConstraint(
            (variables[edge.source] == variables[edge.target]) | strength
        )

    # C8: same-row bbox_y equality
    for group in _row_contig_groups(graph):
        for a, b in zip(group, group[1:]):
            solver.addConstraint((bbox_y_vars[a] == bbox_y_vars[b]) | "strong")

    # C9: row-trunk equality
    for group in _row_contig_groups(graph):
        trunks = [_section_trunk_station(graph, graph.sections[sid]) for sid in group]
        trunks = [t for t in trunks if t and t in variables]
        for a, b in zip(trunks, trunks[1:]):
            solver.addConstraint((variables[a] == variables[b]) | "medium")

    # C10: off-track input above consumer
    for inp, (cons, rank) in _off_track_consumers(graph).items():
        if inp not in variables or cons not in variables:
            continue
        solver.addConstraint(
            (variables[inp] == variables[cons] - rank * y_spacing) | "required"
        )

    # C6': unified grid snap
    pitch, origin = _unified_pitch_origin(graph, y_spacing)
    for sec in graph.sections.values():
        for sid in sec.station_ids:
            if sid not in variables:
                continue
            st = graph.stations.get(sid)
            if st is None:
                continue
            target = origin + round((st.y - origin) / pitch) * pitch
            solver.addConstraint((variables[sid] == target) | "weak")

    solver.updateVariables()

    # Write solver values back onto the graph.  Section bbox_y moves,
    # but bbox_h is held by the engine (the spike doesn't model
    # bbox-height coupling).  Junctions track the engine's ports they
    # were placed against - we leave them where the engine put them so
    # routing geometry isn't completely scrambled by the spike.
    for sec_id, v in bbox_y_vars.items():
        graph.sections[sec_id].bbox_y = v.value()

    for sid, v in variables.items():
        st = graph.stations.get(sid)
        if st is None:
            continue
        st.y = v.value()


def _row_contig_groups(g: MetroGraph) -> list[list[str]]:
    by_row: dict[int, list] = defaultdict(list)
    for s in g.sections.values():
        if s.bbox_h <= 0 or s.grid_row < 0:
            continue
        by_row[s.grid_row].append(s)
    groups: list[list[str]] = []
    for sections in by_row.values():
        sections.sort(key=lambda s: s.grid_col)
        cur = [sections[0]]
        for s in sections[1:]:
            if s.grid_col - cur[-1].grid_col <= 1:
                cur.append(s)
            else:
                if len(cur) >= 2:
                    groups.append([s.id for s in cur])
                cur = [s]
        if len(cur) >= 2:
            groups.append([s.id for s in cur])
    return groups


def _section_trunk_station(g: MetroGraph, section) -> str | None:
    if section.direction not in ("LR", "RL"):
        return None
    port_ids = set(section.entry_ports) | set(section.exit_ports)
    internal = set(section.station_ids) - port_ids
    bundle = None
    for pid in port_ids:
        for edge in g.edges:
            other = (
                edge.target
                if edge.source == pid and edge.target in internal
                else edge.source
                if edge.target == pid and edge.source in internal
                else None
            )
            if other is None:
                continue
            lines = frozenset(g.station_lines(other))
            if bundle is None or len(lines) > len(bundle):
                bundle = lines
    if not bundle:
        return None
    for sid in internal:
        if frozenset(g.station_lines(sid)) == bundle and not sid.startswith("__"):
            return sid
    return None


def _off_track_consumers(g: MetroGraph) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    consumer_inputs: dict[str, list[str]] = defaultdict(list)
    for st in g.stations.values():
        if not getattr(st, "off_track", False):
            continue
        succ = [e.target for e in g.edges if e.source == st.id]
        if len(succ) == 1:
            consumer_inputs[succ[0]].append(st.id)
    for cons, inputs in consumer_inputs.items():
        c_y = g.stations[cons].y
        inputs.sort(key=lambda i: abs(g.stations[i].y - c_y))
        for rank, inp in enumerate(inputs, start=1):
            out[inp] = (cons, rank)
    return out


def _unified_pitch_origin(g: MetroGraph, y_spacing: float) -> tuple[float, float]:
    pitch = y_spacing
    if hasattr(g, "_row_y_grid_info") and g._row_y_grid_info:
        pitch = max(
            (
                info.get("slot_spacing", y_spacing)
                for info in g._row_y_grid_info.values()
            ),
            default=y_spacing,
        )
    residues: list[float] = []
    for sec in g.sections.values():
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = g.stations.get(sid)
            if st is None or getattr(st, "off_track", False):
                continue
            residues.append(round(st.y % pitch, 3))
    origin = Counter(residues).most_common(1)[0][0] if residues else 0.0
    return pitch, origin
