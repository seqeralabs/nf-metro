"""Shared test fixtures and helpers for nf-metro test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

# --- Graph text constants ---

SIMPLE_LINEAR_TEXT = (
    "%%metro line: main | Main | #ff0000\n"
    "graph LR\n"
    "    a[A]\n"
    "    b[B]\n"
    "    c[C]\n"
    "    a -->|main| b\n"
    "    b -->|main| c\n"
)

DIAMOND_TEXT = (
    "%%metro line: main | Main | #ff0000\n"
    "%%metro line: alt | Alt | #0000ff\n"
    "graph LR\n"
    "    a[A]\n"
    "    b[B]\n"
    "    c[C]\n"
    "    d[D]\n"
    "    a -->|main| b\n"
    "    b -->|main| d\n"
    "    a -->|alt| c\n"
    "    c -->|alt| d\n"
)

TWO_SECTION_TEXT = (
    "%%metro line: main | Main | #ff0000\n"
    "graph LR\n"
    "    subgraph sec1 [Section One]\n"
    "        a[A]\n"
    "        b[B]\n"
    "        a -->|main| b\n"
    "    end\n"
    "    subgraph sec2 [Section Two]\n"
    "        c[C]\n"
    "        d[D]\n"
    "        c -->|main| d\n"
    "    end\n"
    "    b -->|main| c\n"
)


# --- Shared constants ---

# The content-placement phases wrapped by _run_placement / _run_placement_per_row
# in _compute_section_layout (the set guarded by _guard_anchors_frozen). Shared so
# the anchor-frozen test and the idempotence test enumerate the same set and a new
# phase can't be added to one without the other.
CONTENT_PLACEMENT_PHASES = (
    "_redistribute_fanout_siblings",  # Stage 4.9
    "_redistribute_full_bundle_columns",  # Stage 4.10
    "_fan_free_content_upward",  # Stage 6.1
    "_fan_source_inputs_upward",  # Stage 6.2
    "_apply_half_grid_2branch_symfan",  # Stage 6.3
    "_recenter_full_bundle_columns",  # Stage 6.7
    "_balance_section_content_around_trunk",  # Stage 6.11
    "_recenter_loop_side_stations",  # Stage 6.12
)

# Compensation-pass call sites in engine.py, keyed by (stage label,
# attribute name(s) invoked at that stage).  Each corrects a specific
# earlier stage's side effect rather than being derived from graph structure
# directly, so idempotence at end-of-layout is not guaranteed by
# construction and must be checked.  5.3/6.9 name the same underlying helper
# under two labels because each label identifies the distinct disturber its
# own engine.py call site corrects; removing one call site should retire only
# its own entry here, independent of the other label's.  6.6 and 6.8 also
# share a helper but are not equivalent: 6.6 runs
# unconditionally while 6.8 (like 6.9) only runs when
# ``graph.center_ports or graph.diamond_style == "symmetric"`` -- see the
# test's ``_CONDITIONAL_STAGES`` gate, which keeps a fixture that never
# reaches the 6.8/6.9 block from having a finding misattributed to it.
# 6.16 is a composite: ``_position_junctions`` reads the entry-port Ys
# ``_align_entry_ports`` just settled, so the pair is applied together, in
# order, as a single unit.
COMPENSATION_PASSES = (
    ("4.7", ("_top_align_row_sections",)),
    ("5.3", ("_top_align_row_bboxes_only",)),
    ("6.6", ("_reanchor_off_track_to_consumer",)),
    ("6.8", ("_reanchor_off_track_to_consumer",)),
    ("6.9", ("_top_align_row_bboxes_only",)),
    ("6.16", ("_align_entry_ports", "_position_junctions")),
)


# --- Render corpus ---

_ROOT = Path(__file__).parent.parent
_EXAMPLES = _ROOT / "examples"
_NEXTFLOW = _ROOT / "tests" / "fixtures" / "nextflow"


def content_corpus() -> list[tuple[str, Path, bool]]:
    """``(fixture_id, path, is_nextflow)`` for every ``.mmd`` in the render
    corpus -- the gallery examples, topology/guide fixtures, test fixtures and
    the Nextflow-DAG fixtures (which need ``convert_nextflow_dag`` first).

    Shared by the declarative-property tests (idempotence and purity) so they
    exercise the same fixtures.

    Fixtures with a ``rails`` line-spread section are excluded: a rail section's
    internal geometry is produced by the self-contained rail pipeline (which
    overwrites the normal content-placement phases), so the per-phase
    idempotence/purity contract those tests assert does not apply to it.
    """

    def _uses_rails(path: Path) -> bool:
        return "line_spread: rails" in path.read_text()

    items: list[tuple[str, Path, bool]] = []
    for d, tag in [
        (_EXAMPLES, "examples"),
        (_EXAMPLES / "topologies", "topologies"),
        (_EXAMPLES / "guide", "guide"),
        (_ROOT / "tests" / "fixtures", "tests"),
    ]:
        for p in sorted(d.glob("*.mmd")):
            if _uses_rails(p):
                continue
            items.append((f"{tag}/{p.stem}", p, False))
    for p in sorted(_NEXTFLOW.glob("*.mmd")):
        items.append((f"nextflow/{p.stem}", p, True))
    return items


def compute_corpus_layout(path: Path, is_nextflow: bool) -> MetroGraph:
    """Parse a corpus ``.mmd`` (converting from a Nextflow DAG first when
    ``is_nextflow``) and run the validated layout, returning the graph."""
    text = path.read_text()
    if is_nextflow:
        text = convert_nextflow_dag(text)
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=True)
    return graph


# --- Mutable-geometry snapshot/restore ---

Coords = dict[str, tuple[float, float]]
Diff = tuple[str, tuple[float, float] | None, tuple[float, float] | None]


def snapshot_stations(graph: MetroGraph) -> Coords:
    """Capture every station ``(x, y)`` only.

    Cheaper than :func:`snapshot_graph_state` for a read-only diff that
    never needs to restore bboxes or ports afterwards.
    """
    return {sid: (s.x, s.y) for sid, s in graph.stations.items()}


def snapshot_graph_state(graph: MetroGraph) -> tuple[Coords, Coords, Coords]:
    """Capture every station ``(x, y)``, section ``(bbox_y, bbox_h)``, and
    port ``(x, y)``.

    Every port also exists as a station with the same id (``add_port``), so
    the station dict alone already reflects a port's current position; the
    separate ``Port`` object (``graph.ports``) is snapshotted too because a
    probe that calls a real phase a second time and then restores only the
    station side would leave ``graph.ports`` at the second call's position,
    desyncing the two id-aliased objects for the rest of the pipeline.
    """
    bboxes = {sec.id: (sec.bbox_y, sec.bbox_h) for sec in graph.sections.values()}
    ports = {pid: (p.x, p.y) for pid, p in graph.ports.items()}
    return snapshot_stations(graph), bboxes, ports


def _write_xy(objects: dict, coords: Coords) -> None:
    """Write each ``coords`` entry's ``(x, y)`` onto the same-id object in
    ``objects``, skipping any id absent from ``objects``."""
    for oid, (x, y) in coords.items():
        obj = objects.get(oid)
        if obj is not None:
            obj.x, obj.y = x, y


def restore_graph_state(graph: MetroGraph, snap: tuple[Coords, Coords, Coords]) -> None:
    """Write a :func:`snapshot_graph_state` result back onto ``graph``."""
    stations, bboxes, ports = snap
    _write_xy(graph.stations, stations)
    for sid, (y, h) in bboxes.items():
        sec = graph.sections.get(sid)
        if sec is not None:
            sec.bbox_y, sec.bbox_h = y, h
    _write_xy(graph.ports, ports)


def diff_station_coords(before: Coords, after: Coords, tol: float = 1e-6) -> list[Diff]:
    """Return per-station ``(id, before, after)`` diffs beyond ``tol``.

    Shared by the content-placement idempotence probe and the
    compensation-pass end-of-layout replay so both compare snapshots the
    same way.
    """
    diffs: list[Diff] = []
    for sid, (x1, y1) in before.items():
        if sid not in after:
            diffs.append((sid, (x1, y1), None))
        elif abs(after[sid][0] - x1) > tol or abs(after[sid][1] - y1) > tol:
            diffs.append((sid, (x1, y1), after[sid]))
    for sid in after.keys() - before.keys():
        diffs.append((sid, None, after[sid]))
    return diffs


# --- Parse/layout helpers ---


def parse_and_layout(text: str, **kwargs) -> MetroGraph:
    """Parse Mermaid text and run the full layout pipeline.

    Accepts keyword arguments passed to compute_layout (e.g. x_spacing, y_spacing).
    """
    graph = parse_metro_mermaid(text)
    compute_layout(graph, **kwargs)
    return graph


# --- Pytest fixtures ---


@pytest.fixture
def simple_linear_graph() -> MetroGraph:
    """A 3-node linear chain: a -> b -> c on one line."""
    return parse_metro_mermaid(SIMPLE_LINEAR_TEXT)


@pytest.fixture
def diamond_graph() -> MetroGraph:
    """A 4-node diamond: a -> {b, c} -> d on two lines."""
    return parse_metro_mermaid(DIAMOND_TEXT)


@pytest.fixture
def two_section_graph() -> MetroGraph:
    """Two sections with one inter-section edge, laid out."""
    return parse_and_layout(TWO_SECTION_TEXT)
