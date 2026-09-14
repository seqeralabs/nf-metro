"""A merge feeder into a LEFT entry descends on the target's own left (#1825).

Several forking sources to the right of a leftmost-column target converge on its
LEFT entry port, so the parser inserts a merge junction standing in front of that
port.  The feeders reach the junction through the generic U-bypass, which chooses
its descent column from the travel direction: a leftward hop would rise in the
gap to the RIGHT of the target and run its final leg leftward across the whole
interior to reach the merge junction sitting at the port's own (left) edge.

The descent column must instead anchor to the target's own left side, the way the
same bundle would if it targeted the LEFT entry port directly.  ``_bypass_geometry``
owns that choice; this locks it at that call site, since the surrounding
convergence topology hits an unrelated planning abort before a full render, and no
gallery fixture exercises a merge-junction LEFT entry fed from its right.
"""

from __future__ import annotations

import nf_metro.layout.routing.inter_section_handlers as ish
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.common import Direction
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

_FIXTURE = """%%metro title: left-entry merge feeder
%%metro line: rib | Ribo | #e6007e
%%metro grid: aln, nvt | 0,0
%%metro grid: orf, psi, te | 0,1
graph LR
    subgraph aln [Alignment]
        umi_dedup[UMI dedup]
    end
    subgraph nvt [Novel]
        stringtie[StringTie]
    end
    subgraph orf [ORF calling]
        star_hybrid[STAR hybrid]
        orf_merge[Merge ORF]
        star_hybrid -->|rib| orf_merge
    end
    subgraph psi [P-site]
        ribowaltz[riboWaltz]
        quantify[Quantify]
        ribowaltz -->|rib| quantify
    end
    subgraph te [TE]
        te_prep[TE prep]
        te_calc[TE calc]
        te_prep -->|rib| te_calc
    end
    umi_dedup -->|rib| star_hybrid
    umi_dedup -->|rib| ribowaltz
    stringtie -->|rib| star_hybrid
    stringtie -->|rib| te_prep
    orf_merge -->|rib| quantify
"""


def test_left_entry_merge_feeder_descends_on_target_left() -> None:
    graph = parse_metro_mermaid(_FIXTURE)
    captured: list[tuple[str, float, float]] = []
    original = ish._bypass_geometry

    def spy(facts: ish._InterFacts, shape=None):  # type: ignore[no-untyped-def]
        geometry = original(facts, shape)
        merge_ep = facts.merge_ep
        merge_port = graph.ports.get(merge_ep.id) if merge_ep is not None else None
        if (
            facts.entry_side is None
            and merge_ep is not None
            and merge_port is not None
            and merge_port.is_entry
            and merge_port.side is PortSide.LEFT
            and facts.horizontal is Direction.L
        ):
            section = graph.sections[merge_ep.section_id]
            captured.append((facts.edge.target, geometry.gap2_x, section.bbox_x))
        return geometry

    ish._bypass_geometry = spy
    unexpected: Exception | None = None
    try:
        compute_layout(graph)
        offsets = compute_station_offsets(graph)
        route_edges_centred(graph, station_offsets=offsets)
    except Exception as exc:
        # An unrelated convergence-Y planning abort can follow the descent-column
        # choice this test pins; the spy has already recorded that choice by the
        # time it raises.  Keep the exception so an abort that instead precedes the
        # feeder surfaces in the assertion below rather than masquerading as an
        # unexercised fixture.
        unexpected = exc
    finally:
        ish._bypass_geometry = original

    assert captured, "fixture did not exercise a LEFT-entry merge-junction feeder" + (
        f" (routing raised {unexpected!r})" if unexpected is not None else ""
    )
    for target, descent_x, section_left in captured:
        assert descent_x <= section_left, (
            f"merge feeder into {target!r} descends at x={descent_x:.1f}, "
            f"right of its target section's left edge x={section_left:.1f}: "
            "the bundle crosses the interior instead of anchoring on the port's "
            "own left approach"
        )
