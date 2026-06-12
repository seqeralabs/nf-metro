"""Explains WHY nf-metro made each layout decision for a parsed graph.

This is the data behind ``nf-metro explain``: a causal answer to "why did
nf-metro do that?".  It surfaces the rule that fired for each non-trivial
inferred decision and each synthetic element the engine inserted.

:mod:`nf_metro.introspect` answers "what did nf-metro build" (structure);
this module answers "why did it choose that" (causation).

Everything reported is available immediately after
:func:`~nf_metro.parser.parse_metro_mermaid`; no full layout pass is
required.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph, Section

from nf_metro.introspect import station_kind

__all__ = ["build_explain", "format_explain_json", "format_explain_text"]

# Aspect keys — used in both the formatter's _ASPECT_ORDER and each decision dict.
_A_LAYOUT = "layout"
_A_DIRECTION = "direction"
_A_ENTRY = "entry_side"
_A_EXIT = "exit_side"
_A_JUNCTION = "junction"
_A_BYPASS = "bypass"

# Source labels
_SRC_INFERRED = "inferred"
_SRC_SYNTHETIC = "synthetic"

_ASPECT_ORDER = [_A_LAYOUT, _A_DIRECTION, _A_ENTRY, _A_EXIT, _A_JUNCTION, _A_BYPASS]
_ASPECT_HEADERS = {
    _A_LAYOUT: "Layout structure",
    _A_DIRECTION: "Section directions (inferred)",
    _A_ENTRY: "Entry port sides (inferred)",
    _A_EXIT: "Exit port sides (inferred)",
    _A_JUNCTION: "Fan-out junctions (synthetic)",
    _A_BYPASS: "Bypass stations (synthetic)",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_explain(
    graph: MetroGraph,
    warnings: list[str] | None = None,
    *,
    section_filter: str | None = None,
    station_filter: str | None = None,
) -> dict[str, Any]:
    """Build the structured explanation dict for a parsed graph.

    *warnings* are parse-time warning messages captured by the caller.

    *section_filter* restricts decisions to those involving the named section.
    *station_filter* restricts decisions to those involving the named station.
    """
    dag = graph.section_dag
    dag_succs: dict[str, set[str]] = dag.successors if dag is not None else {}
    dag_preds: dict[str, set[str]] = dag.predecessors if dag is not None else {}

    decisions: list[dict[str, Any]] = []

    _add_layout_decisions(graph, decisions)
    _add_direction_decisions(graph, dag_succs, dag_preds, decisions)
    _add_port_decisions(graph, dag_preds, decisions)
    _add_junction_decisions(graph, decisions)
    _add_bypass_decisions(graph, decisions)

    if section_filter:
        decisions = [d for d in decisions if section_filter in d.get("sections", [])]
    if station_filter:
        decisions = [d for d in decisions if d.get("subject") == station_filter]

    inferred = sum(1 for d in decisions if d["source"] == _SRC_INFERRED)
    synthetic = sum(1 for d in decisions if d["source"] == _SRC_SYNTHETIC)

    return {
        "title": graph.title or None,
        "warnings": list(warnings or []),
        "decisions": decisions,
        "summary": {"inferred": inferred, "synthetic": synthetic},
        "filter": {"section": section_filter, "station": station_filter},
    }


def format_explain_json(data: dict[str, Any]) -> str:
    """Serialise the explanation dict as indented JSON."""
    return json.dumps(data, indent=2)


def format_explain_text(data: dict[str, Any]) -> str:
    """Render the explanation dict as human-readable text."""
    out: list[str] = []
    out.append(f"Explain: {data['title'] or '(untitled)'}")

    if data["warnings"]:
        out.append("")
        out.append("Warnings:")
        for w in data["warnings"]:
            out.append(f"  ! {w}")

    decisions = data["decisions"]
    if not decisions:
        out.append("")
        filt = data.get("filter", {})
        subject = filt.get("section") or filt.get("station")
        if subject:
            out.append(f"No layout decisions found for '{subject}'.")
        else:
            out.append("No layout decisions to explain (all settings are explicit).")
        return "\n".join(out)

    summary = data["summary"]
    parts = []
    if summary["inferred"]:
        parts.append(f"{summary['inferred']} inferred")
    if summary["synthetic"]:
        parts.append(f"{summary['synthetic']} synthetic")
    if parts:
        out.append(f"({', '.join(parts)})")

    by_aspect: dict[str, list[dict[str, Any]]] = {}
    for d in decisions:
        by_aspect.setdefault(d["aspect"], []).append(d)

    for aspect in _ASPECT_ORDER:
        group = by_aspect.get(aspect, [])
        if not group:
            continue
        out.append("")
        out.append(f"{_ASPECT_HEADERS.get(aspect, aspect)}:")
        for d in group:
            subject = d["subject"]
            decision = d["decision"]
            detail = d["detail"]
            if aspect == _A_LAYOUT:
                out.append(f"  {detail}")
            elif aspect in (_A_JUNCTION, _A_BYPASS):
                out.append(f"  {subject}: {detail}")
            else:
                out.append(f"  [{subject}] -> {decision}: {detail}")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Decision builders
# ---------------------------------------------------------------------------


def _decision(
    subject: str,
    subject_type: str,
    aspect: str,
    decision: str,
    source: str,
    rule: str,
    detail: str,
    sections: list[str],
) -> dict[str, Any]:
    return {
        "subject": subject,
        "subject_type": subject_type,
        "aspect": aspect,
        "decision": decision,
        "source": source,
        "rule": rule,
        "detail": detail,
        "sections": sections,
    }


def _add_layout_decisions(
    graph: MetroGraph,
    decisions: list[dict[str, Any]],
) -> None:
    """Emit a fold/row explanation when the layout spans multiple rows."""
    real_sections = graph.real_sections
    if not real_sections:
        return

    sid_rows = {sid: _section_grid_row(graph, sid) for sid in real_sections}
    rows: dict[int, list[str]] = {}
    for sid, row in sid_rows.items():
        rows.setdefault(row, []).append(sid)

    if len(rows) <= 1:
        return

    if not any(
        row > 0 and sid not in graph._explicit_grid for sid, row in sid_rows.items()
    ):
        return

    threshold = graph.fold_threshold if graph.fold_threshold is not None else 15
    n_rows = len(rows)
    section_list = ", ".join(
        f"row {r}: [{', '.join(sorted(sids))}]" for r, sids in sorted(rows.items())
    )
    decisions.append(
        _decision(
            "layout",
            "layout",
            _A_LAYOUT,
            f"{n_rows} rows",
            _SRC_INFERRED,
            "fold-threshold",
            (
                f"Layout spans {n_rows} rows because the section chain exceeded "
                f"the fold threshold ({threshold} station-columns). "
                f"{section_list}."
            ),
            list(real_sections),
        )
    )


def _add_direction_decisions(
    graph: MetroGraph,
    dag_succs: dict[str, set[str]],
    dag_preds: dict[str, set[str]],
    decisions: list[dict[str, Any]],
) -> None:
    """Emit a direction decision for each section whose direction was inferred."""
    for sec_id, section in graph.sections.items():
        if sec_id in graph._explicit_directions:
            continue
        # Sections with explicit grids are skipped by _infer_directions;
        # their direction keeps the LR default without inference running.
        if sec_id in graph._explicit_grid:
            continue

        rule, detail = _explain_direction(graph, sec_id, section, dag_succs, dag_preds)
        decisions.append(
            _decision(
                sec_id,
                "section",
                _A_DIRECTION,
                section.direction,
                _SRC_INFERRED,
                rule,
                detail,
                [sec_id],
            )
        )


def _add_port_decisions(
    graph: MetroGraph,
    dag_preds: dict[str, set[str]],
    decisions: list[dict[str, Any]],
) -> None:
    """Emit entry/exit side decisions for sections whose port sides were inferred."""
    for sec_id, section in graph.sections.items():
        if sec_id not in graph._explicit_entry and section.entry_hints:
            sides = sorted({s.value for s, _ in section.entry_hints})
            rule, detail = _explain_entry_side(graph, sec_id, section, dag_preds, sides)
            decisions.append(
                _decision(
                    sec_id,
                    "section",
                    _A_ENTRY,
                    ", ".join(sides),
                    _SRC_INFERRED,
                    rule,
                    detail,
                    [sec_id],
                )
            )

        if sec_id not in graph._explicit_exit and section.exit_hints:
            sides = sorted({s.value for s, _ in section.exit_hints})
            rule, detail = _explain_exit_side(section, sides)
            decisions.append(
                _decision(
                    sec_id,
                    "section",
                    _A_EXIT,
                    ", ".join(sides),
                    _SRC_INFERRED,
                    rule,
                    detail,
                    [sec_id],
                )
            )


def _add_junction_decisions(graph: MetroGraph, decisions: list[dict[str, Any]]) -> None:
    """Emit an explanation for each junction inserted by _resolve_sections."""
    for jid in graph.junctions:
        out_edges = graph.edges_from(jid)
        in_edges = graph.edges_to(jid)

        if jid.startswith("__merge_"):
            src_sections = sorted(
                {
                    graph.section_for_station(e.source) or "?"
                    for e in in_edges
                    if e.source != jid
                }
            )
            lines = sorted({e.line_id for e in in_edges})
            tgt_port = out_edges[0].target if out_edges else "?"
            tgt_sec = (
                graph.ports[tgt_port].section_id if tgt_port in graph.ports else "?"
            )
            all_secs = sorted(set(src_sections) | {tgt_sec})
            decisions.append(
                _decision(
                    jid,
                    "station",
                    _A_JUNCTION,
                    "merge-junction",
                    _SRC_SYNTHETIC,
                    "merge-junction",
                    (
                        f"Merge junction: {len(in_edges)} same-line paths "
                        f"({', '.join(lines)}) from sections "
                        f"[{', '.join(src_sections)}] "
                        f"converge on a single entry port in '{tgt_sec}'."
                    ),
                    all_secs,
                )
            )
        else:
            tgt_ports = {e.target for e in out_edges if e.target in graph.ports}
            tgt_sections = sorted(
                {graph.ports[pid].section_id for pid in tgt_ports if pid in graph.ports}
            )
            in_ports = {e.source for e in in_edges if e.source in graph.ports}
            src_section = "?"
            for pid in in_ports:
                port = graph.ports.get(pid)
                if port and not port.is_entry:
                    src_section = port.section_id
                    break

            all_lines = sorted({e.line_id for e in out_edges})
            all_secs = sorted({src_section} | set(tgt_sections))
            decisions.append(
                _decision(
                    jid,
                    "station",
                    _A_JUNCTION,
                    "fan-out-junction",
                    _SRC_SYNTHETIC,
                    "fan-out-junction",
                    (
                        f"Fan-out junction: lines [{', '.join(all_lines)}] leave "
                        f"'{src_section}' and diverge to "
                        f"{len(tgt_sections)} section(s): [{', '.join(tgt_sections)}]."
                    ),
                    all_secs,
                )
            )


def _add_bypass_decisions(graph: MetroGraph, decisions: list[dict[str, Any]]) -> None:
    """Emit an explanation for each bypass-V station."""
    for sid, station in graph.stations.items():
        if station_kind(graph, sid) != "bypass":
            continue
        bypassed = graph.stations.get(station.bypasses_station_id)  # type: ignore[arg-type]
        bypassed_label = (
            f"'{bypassed.label}'"
            if bypassed and bypassed.label
            else f"station {station.bypasses_station_id!r}"
        )
        lines = sorted(graph.station_lines(sid))
        sec_id = station.section_id or "?"
        decisions.append(
            _decision(
                sid,
                "station",
                _A_BYPASS,
                "bypass-v",
                _SRC_SYNTHETIC,
                "bypass-v",
                (
                    f"Hidden bypass-V station routes line(s) [{', '.join(lines)}] "
                    f"clear of {bypassed_label}'s label marker in "
                    f"section '{sec_id}'."
                ),
                [sec_id],
            )
        )


# ---------------------------------------------------------------------------
# Rule derivation helpers
# ---------------------------------------------------------------------------


def _section_grid_col(graph: MetroGraph, sec_id: str) -> int:
    """Return the effective grid column, consulting grid_overrides first."""
    if sec_id in graph.grid_overrides:
        return graph.grid_overrides[sec_id][0]
    return graph.sections[sec_id].grid_col


def _section_grid_row(graph: MetroGraph, sec_id: str) -> int:
    """Return the effective grid row for *sec_id*, consulting grid_overrides first."""
    if sec_id in graph.grid_overrides:
        return graph.grid_overrides[sec_id][1]
    return graph.sections[sec_id].grid_row


def _explain_direction(
    graph: MetroGraph,
    sec_id: str,
    section: Section,
    dag_succs: dict[str, set[str]],
    dag_preds: dict[str, set[str]],
) -> tuple[str, str]:
    """Return (rule, detail) explaining why *section* got its direction."""
    direction = section.direction
    succs = dag_succs.get(sec_id, set())
    preds = dag_preds.get(sec_id, set())
    my_col = _section_grid_col(graph, sec_id)
    my_row = _section_grid_row(graph, sec_id)

    if direction == "TB":
        for tgt in succs:
            tgt_row = _section_grid_row(graph, tgt) if tgt in graph.sections else -1
            if tgt_row != my_row and tgt_row >= 0:
                return (
                    "fold-bridge",
                    f"Section acts as a vertical bridge between row {my_row} and "
                    f"row {tgt_row}; TB direction routes lines downward "
                    f"at the fold point.",
                )
        below_succs = [
            tgt
            for tgt in succs
            if tgt in graph.sections and _section_grid_row(graph, tgt) == my_row + 1
        ]
        if below_succs:
            names = [graph.sections[t].name for t in below_succs]
            return (
                "successors-below",
                f"All successors are in the row directly below "
                f"(row {my_row + 1}): {', '.join(names)}.",
            )
        return (
            "successors-below",
            f"All successors are positioned directly below section '{section.name}'.",
        )

    if direction == "RL":
        succ_cols = [
            (tgt, _section_grid_col(graph, tgt))
            for tgt in succs
            if tgt in graph.sections
        ]
        if succ_cols and all(c < my_col for _, c in succ_cols):
            succ_desc = ", ".join(
                f"'{graph.sections[t].name}' (col {c})" for t, c in succ_cols
            )
            return (
                "successors-to-left",
                f"All successors are to the left (col {my_col}): {succ_desc}; "
                f"RL direction reads right-to-left.",
            )
        if not succs and preds:
            pred_cols = [
                (src, _section_grid_col(graph, src))
                for src in preds
                if src in graph.sections
            ]
            if pred_cols and any(c >= my_col for _, c in pred_cols):
                pred_desc = ", ".join(
                    f"'{graph.sections[src].name}' (col {c})" for src, c in pred_cols
                )
                return (
                    "return-row-leaf",
                    f"Terminal section on a return row; predecessor(s) "
                    f"{pred_desc} are to the right.",
                )
        return (
            "reverse-flow",
            "Right-to-left flow: predecessors are positioned to the right.",
        )

    # LR (default)
    if not preds:
        return (
            "source-section",
            f"Section '{section.name}' has no upstream predecessors; LR is the "
            f"default flow direction for pipeline heads.",
        )
    pred_descs = [
        f"'{graph.sections[src].name}' (col {_section_grid_col(graph, src)})"
        for src in preds
        if src in graph.sections
    ]
    if pred_descs:
        return (
            "flow-aligned-default",
            f"Predecessor(s) {', '.join(pred_descs)} are to the left; LR is "
            f"the default downstream flow direction.",
        )
    return (
        "flow-aligned-default",
        "Default left-to-right flow direction.",
    )


def _explain_entry_side(
    graph: MetroGraph,
    sec_id: str,
    section: Section,
    dag_preds: dict[str, set[str]],
    sides: list[str],
) -> tuple[str, str]:
    """Return (rule, detail) explaining the inferred entry port side."""
    if not sides:
        return ("no-predecessors", "No upstream predecessors; no entry port created.")

    side_str = ", ".join(sides)
    direction = section.direction

    preds = dag_preds.get(sec_id, set())
    for src in preds:
        src_sec = graph.sections.get(src)
        if not src_sec:
            continue
        if (
            src_sec.direction == "TB"
            and _section_grid_row(graph, src) < _section_grid_row(graph, sec_id)
            and "top" in sides
        ):
            return (
                "vertical-drop",
                f"Predecessor '{src_sec.name}' is a vertical (TB) section "
                f"in the row above; entry placed on TOP to receive the "
                f"downward flow.",
            )

    if direction in ("LR", "RL"):
        expected = "left" if direction == "LR" else "right"
        if expected in sides:
            return (
                "flow-aligned-entry",
                f"Section flows {direction}; entry placed on {side_str.upper()} "
                f"(the leading edge) to receive the incoming flow.",
            )

    pred_descs = []
    for src in preds:
        src_sec = graph.sections.get(src)
        if src_sec:
            pred_descs.append(
                f"'{src_sec.name}' "
                f"(col {_section_grid_col(graph, src)}, "
                f"row {_section_grid_row(graph, src)})"
            )
    pred_part = (
        f"predecessor(s) {', '.join(pred_descs)}"
        if pred_descs
        else "predecessor position"
    )
    return (
        "relative-position",
        f"Entry placed on {side_str.upper()} based on the grid position of "
        f"{pred_part} relative to this section.",
    )


def _explain_exit_side(
    section: Section,
    sides: list[str],
) -> tuple[str, str]:
    """Return (rule, detail) explaining the inferred exit port side."""
    if not sides:
        return ("no-successors", "No downstream successors; no exit port created.")

    side_str = ", ".join(sides)
    direction = section.direction

    if direction == "TB" and any(s in sides for s in ("left", "bottom")):
        return (
            "fold-exit",
            f"Fold bridge section; exit placed on {side_str.upper()} toward "
            f"the successor(s) on the return row.",
        )

    if direction in ("LR", "RL"):
        expected = "right" if direction == "LR" else "left"
        if expected in sides:
            return (
                "flow-aligned-exit",
                f"Section flows {direction}; exit placed on {side_str.upper()} "
                f"(the trailing edge) toward downstream sections.",
            )

    return (
        "relative-position",
        f"Exit placed on {side_str.upper()} based on successor grid position.",
    )
