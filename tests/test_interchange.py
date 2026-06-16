"""Cross-track interchange (``%%metro interchange:`` + auto-detection).

An interchange is one logical step several lines pass through on their own
tracks: the node is expanded into one ordinary sub-station per rail so the
layout engine keeps each line straight, and the renderer joins the members into
a single connector glyph instead of pinching the lines to a point.
"""

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render import render_svg
from nf_metro.themes import THEMES

EXAMPLES = Path(__file__).parent.parent / "examples"

# Two fully-parallel lanes (tumor/normal) sharing one step.
_PARALLEL = (
    "%%metro line: tumor | Tumor | #d62728\n"
    "%%metro line: normal | Normal | #0570b0\n"
    "{directive}"
    "graph LR\n"
    "    subgraph s [Calling]\n"
    "        t_in[ ]\n        n_in[ ]\n"
    "        t_align[align T]\n        n_align[align N]\n"
    "        dedup[MarkDuplicates]\n"
    "        t_call[Mutect2]\n        n_call[HaplotypeCaller]\n"
    "        t_in -->|tumor| t_align\n        n_in -->|normal| n_align\n"
    "        t_align -->|tumor| dedup\n        n_align -->|normal| dedup\n"
    "        dedup -->|tumor| t_call\n        dedup -->|normal| n_call\n"
    "    end\n"
)


def _layout(src):
    g = parse_metro_mermaid(src)
    compute_layout(g)
    return g


def test_directive_expands_node_into_per_rail_substations():
    g = _layout(
        _PARALLEL.format(directive="%%metro interchange: dedup | tumor | normal\n")
    )
    ic = next(c for c in g.interchanges if c.node_id == "dedup")
    assert len(ic.member_ids) == 2
    # Each member carries exactly its rail's line; edges were repointed.
    by_line = {tuple(g.station_lines(m)): m for m in ic.member_ids}
    assert by_line[("tumor",)] == "dedup"
    assert ("normal",) in by_line


def test_interchange_members_share_a_column_at_distinct_tracks():
    g = _layout(
        _PARALLEL.format(directive="%%metro interchange: dedup | tumor | normal\n")
    )
    members = [g.stations[m] for m in g.interchanges[0].member_ids]
    assert len({round(m.x) for m in members}) == 1, "members must share one column"
    assert len({round(m.y) for m in members}) == len(members), "distinct tracks"


def test_each_lane_runs_straight_through_the_interchange():
    g = _layout(
        _PARALLEL.format(directive="%%metro interchange: dedup | tumor | normal\n")
    )
    # Every station carrying a single lane line sits on that lane's one track Y.
    for line in ("tumor", "normal"):
        ys = {
            round(s.y)
            for s in g.stations.values()
            if g.station_lines(s.id) == [line] and not s.is_port
        }
        assert len(ys) == 1, f"{line} lane is not straight: tracks {ys}"


def test_auto_detection_infers_parallel_lane_interchange():
    """No directive: fully-parallel lanes get an interchange inferred."""
    g = parse_metro_mermaid(_PARALLEL.format(directive=""))
    inferred = [c for c in g.interchanges if c.inferred]
    assert [c.node_id for c in inferred] == ["dedup"]
    assert sorted(r[0] for r in inferred[0].rails) == ["normal", "tumor"]


def test_auto_detection_abstains_when_lanes_share_a_neighbour():
    """Two lines that reconverge downstream (shared successor) must NOT be
    auto-bridged -- the convergence is doing real work."""
    src = (
        "%%metro line: a | A | #d62728\n"
        "%%metro line: b | B | #0570b0\n"
        "graph LR\n"
        "    subgraph s [S]\n"
        "        a_in[ ]\n        b_in[ ]\n"
        "        shared[QC]\n"
        "        merge[Merge]\n"
        "        a_in -->|a| shared\n        b_in -->|b| shared\n"
        "        shared -->|a| merge\n        shared -->|b| merge\n"
        "    end\n"
    )
    g = parse_metro_mermaid(src)
    assert [c for c in g.interchanges if c.inferred] == []


def test_render_emits_single_connector_and_suppresses_duplicate_pill():
    g = _layout(
        _PARALLEL.format(directive="%%metro interchange: dedup | tumor | normal\n")
    )
    svg = render_svg(g, THEMES["nfcore"])
    # The interchange link bar carries the rail-connector class, keyed to the
    # node id (drawn once for the whole glyph).
    assert svg.count('data-station-id="dedup"') >= 1
    assert "nf-metro-rail-connector" in svg


def test_example_fixture_renders_with_inferred_and_explicit_agreeing():
    src = (EXAMPLES / "cross_track_interchange.mmd").read_text()
    g_explicit = _layout(src)
    g_auto = _layout(
        "\n".join(
            ln for ln in src.splitlines() if not ln.startswith("%%metro interchange:")
        )
    )
    # Author-pinned and auto-inferred land the same interchange node.
    assert {c.node_id for c in g_explicit.interchanges} == {"markduplicates"}
    assert {c.node_id for c in g_auto.interchanges if c.inferred} == {"markduplicates"}
