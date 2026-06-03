"""Per-phase coordinate snapshots for regression localisation (issue #363).

``compute_layout`` runs 20+ mutating phases, each writing directly onto
``Station.x``/``.y``, ``Port.x``/``.y`` and ``Section`` bbox state.  When a
layout regression appears with no guard violation, "which phase caused it"
is a manual bisect through ``engine.py``.

This module captures a serialised snapshot of the mutable coordinate state
after each phase, keyed by phase id, so two layout runs (e.g. base vs PR)
can be diffed to surface the first phase at which coordinates diverge.

Capture is **off by default** and gated on the ``NF_METRO_PHASE_SNAPSHOTS``
environment variable.  When unset, :func:`capture_phase_snapshot` does no
work beyond a single cheap boolean check, so normal renders pay no cost and
behaviour is unchanged.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph

# Root of the snapshot tree.  Overridable via NF_METRO_PHASE_SNAPSHOT_DIR so
# tests and CI can redirect the dump without touching /tmp.
_DEFAULT_SNAPSHOT_ROOT = Path("/tmp/nf_metro_phase_snapshots")

_ENABLE_ENV = "NF_METRO_PHASE_SNAPSHOTS"
_DIR_ENV = "NF_METRO_PHASE_SNAPSHOT_DIR"

# Coordinates are rounded to this many decimals before serialising so a
# float-formatting wobble never masquerades as a layout divergence.
_COORD_DECIMALS = 4


def phase_snapshots_enabled() -> bool:
    """True when ``NF_METRO_PHASE_SNAPSHOTS`` requests phase dumps.

    Read once at the start of ``compute_layout`` and threaded through as a
    plain bool so the hot path never re-reads the environment.
    """
    return os.environ.get(_ENABLE_ENV) == "1"


def _snapshot_root() -> Path:
    override = os.environ.get(_DIR_ENV)
    return Path(override) if override else _DEFAULT_SNAPSHOT_ROOT


def _fixture_slug(graph: MetroGraph) -> str:
    """A filesystem-safe directory name identifying the rendered graph.

    Uses the graph title when present (the human-facing fixture name),
    falling back to ``untitled`` so a title-less graph still snapshots.
    """
    raw = (graph.title or "untitled").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return slug or "untitled"


def serialise_graph_coords(graph: MetroGraph) -> dict[str, Any]:
    """Serialise the mutable coordinate state of *graph* to plain data.

    Captures station coords (and the off-track flag, which a phase can flip),
    port positions, and section bboxes.  Per-route/per-edge state is
    deliberately excluded (out of scope per #363); stations + ports + bboxes
    are sufficient for regression localisation.
    """

    def r(value: float) -> float:
        return round(float(value), _COORD_DECIMALS)

    # Key order is normalised by json.dumps(sort_keys=True) at write time.
    stations = {
        sid: {
            "x": r(st.x),
            "y": r(st.y),
            "is_port": st.is_port,
            "off_track": st.off_track,
        }
        for sid, st in graph.stations.items()
    }
    ports = {pid: {"x": r(p.x), "y": r(p.y)} for pid, p in graph.ports.items()}
    sections = {
        sec_id: {
            "bbox_x": r(sec.bbox_x),
            "bbox_y": r(sec.bbox_y),
            "bbox_w": r(sec.bbox_w),
            "bbox_h": r(sec.bbox_h),
        }
        for sec_id, sec in graph.sections.items()
    }
    return {"stations": stations, "ports": ports, "sections": sections}


def capture_phase_snapshot(graph: MetroGraph, phase_id: str, enabled: bool) -> None:
    """Write a coordinate snapshot for *phase_id* when *enabled*.

    A no-op (single boolean test, no serialisation, no I/O) when *enabled*
    is False, which is the default.  When True, dumps
    ``<root>/<fixture>/<phase_id>.json``.
    """
    if not enabled:
        return
    out_dir = _snapshot_root() / _fixture_slug(graph)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_phase = re.sub(r"[^A-Za-z0-9._-]+", "_", phase_id)
    payload = serialise_graph_coords(graph)
    (out_dir / f"{safe_phase}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
