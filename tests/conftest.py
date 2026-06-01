"""Shared test fixtures and helpers for nf-metro test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    """
    items: list[tuple[str, Path, bool]] = []
    for d, tag in [
        (_EXAMPLES, "examples"),
        (_EXAMPLES / "topologies", "topologies"),
        (_EXAMPLES / "guide", "guide"),
        (_ROOT / "tests" / "fixtures", "tests"),
    ]:
        for p in sorted(d.glob("*.mmd")):
            items.append((f"{tag}/{p.stem}", p, False))
    for p in sorted(_NEXTFLOW.glob("*.mmd")):
        items.append((f"nextflow/{p.stem}", p, True))
    return items


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
