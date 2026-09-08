"""A flat (sectionless) graph hosts a bypass detour for a skip-line (#1953).

A sectionless ``.mmd`` whose skip-line passes over intermediate stations it
does not consume routes a detour around their markers. The detour is
section-gated, so a flat graph needs an implicit section to host it; lacking
one, the skip-line is painted straight through the non-consumer markers and
``_guard_no_line_crosses_non_consumer`` downgrades to a warning.
"""

from __future__ import annotations

import warnings

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render import render_svg
from nf_metro.themes import THEMES

# The #1953 repro: a plain ``a->hub->m0->m1->m2->z`` trunk on the ``generic``
# line plus a ``hub->m2`` skip on ``align`` that jumps m0 and m1. No subgraph -
# the graph stays flat, which is the shape under test.
FLAT_SKIP_REPRO = """\
%%metro line: generic | Analysis | #79706E
%%metro line: align | Alignment | #54A24B
graph LR
    a["a"] -->|generic| hub["hub"]
    hub -->|generic| m0["m0"]
    m0 -->|generic| m1["m1"]
    m1 -->|generic| m2["m2"]
    m2 -->|generic| z["z"]
    hub -->|align| m2
"""


def _tier_a_warnings(source: str) -> list[str]:
    graph = parse_metro_mermaid(source)
    compute_layout(graph)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        render_svg(graph, THEMES["nfcore"])
    return [str(w.message) for w in caught if "Tier-A invariants" in str(w.message)]


def test_flat_skip_line_does_not_cross_non_consumer_marker() -> None:
    """The flat repro renders without any Tier-A layout-invariant downgrade.

    In particular ``_guard_no_line_crosses_non_consumer`` must not fire: the
    ``align`` skip-line has to detour around the ``m0``/``m1`` markers it does
    not consume rather than paint straight through them.
    """
    offenders = _tier_a_warnings(FLAT_SKIP_REPRO)
    assert not offenders, f"flat skip-line warned: {offenders}"


def test_flat_graph_gets_a_section_to_host_the_detour() -> None:
    """The sectionless repro is given an implicit section, the host the
    bypass-detour routing path is gated behind."""
    graph = parse_metro_mermaid(FLAT_SKIP_REPRO)
    compute_layout(graph)
    assert graph.sections, "flat graph never received an implicit section"
