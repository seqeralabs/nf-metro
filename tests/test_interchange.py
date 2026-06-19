"""Cross-track interchange (``%%metro interchange:`` + auto-detection).

An interchange is one logical step several lines pass through on their own
tracks: the node is expanded into one ordinary sub-station per rail so the
layout engine keeps each line straight, and the renderer joins the members into
a single connector glyph instead of pinching the lines to a point.
"""

from pathlib import Path

import pytest

from nf_metro.layout.constants import SAME_COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render import render_svg
from nf_metro.themes import THEMES

EXAMPLES = Path(__file__).parent.parent / "examples"
ALL_FIXTURES = sorted(EXAMPLES.glob("*.mmd")) + sorted(
    (EXAMPLES / "topologies").glob("*.mmd")
)

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


# Three independent lanes through one node, for multi-line-per-rail tests.
_THREE = (
    "%%metro line: a | A | #d62728\n"
    "%%metro line: b | B | #2db572\n"
    "%%metro line: c | C | #0570b0\n"
    "{directive}"
    "graph LR\n"
    "    subgraph s [S]\n"
    "        a_in[ ]\n        b_in[ ]\n        c_in[ ]\n"
    "        dedup[Dedup]\n"
    "        a_out[ ]\n        b_out[ ]\n        c_out[ ]\n"
    "        a_in -->|a| dedup\n        b_in -->|b| dedup\n        c_in -->|c| dedup\n"
    "        dedup -->|a| a_out\n        dedup -->|b| b_out\n"
    "        dedup -->|c| c_out\n"
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


def test_auto_detection_abstains_when_bar_would_span_a_third_lane():
    """longread's samtools_merge carries ubam+bam, but the fastq lane's track
    sits between them; a bridge there would draw its bar over cat_fastq.  The
    clearance rule must keep auto-detection from inferring it."""
    g = parse_metro_mermaid((EXAMPLES / "longread_variant_calling.mmd").read_text())
    assert "samtools_merge" not in {c.node_id for c in g.interchanges}


def test_directive_bundles_multiple_lines_on_one_rail():
    """A rail naming several lines becomes one sub-station carrying that bundle;
    the other rail is its own member."""
    g = _layout(_THREE.format(directive="%%metro interchange: dedup | a,b | c\n"))
    ic = next(c for c in g.interchanges if c.node_id == "dedup")
    assert len(ic.member_ids) == 2
    members = {m: set(g.station_lines(m)) for m in ic.member_ids}
    assert members["dedup"] == {"a", "b"}
    assert set().union(*members.values()) == {"a", "b", "c"}
    # One knob per (member, line) pair, so all three lines are knobbed.
    svg = render_svg(g, THEMES["nfcore"])
    assert svg.count('class="nf-metro-rail-knob"') >= 3


def test_directive_skipped_when_under_two_live_rails():
    """A directive whose rails resolve to fewer than two lines the node carries
    is warned about and left unexpanded (the node renders normally)."""
    src = _THREE.format(directive="%%metro interchange: dedup | a,b,c | missing\n")
    with pytest.warns(UserWarning, match="fewer than two rails"):
        g = parse_metro_mermaid(src)
    assert g.stations["dedup"].interchange_id is None
    assert all(not c.member_ids for c in g.interchanges)


def test_marked_interchange_renders_as_a_tinted_interchange():
    """A %%metro marker on the interchange node tints the glyph rather than
    suppressing it; the connector renders and carries the marker fill."""
    src = _PARALLEL.format(
        directive=(
            "%%metro interchange: dedup | tumor | normal\n"
            "%%metro marker: dedup | square, #4CAF50\n"
        )
    )
    g = _layout(src)
    svg = render_svg(g, THEMES["nfcore"])
    assert "nf-metro-rail-connector" in svg
    assert "#4CAF50" in svg


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda p: p.name)
def test_interchange_bar_never_spans_a_non_member_station(fixture):
    """An interchange connector bar runs between its top and bottom member
    rails; no other station may sit within that span at the bar's column, or
    the bar would visibly cut through that station's marker."""
    g = parse_metro_mermaid(fixture.read_text())
    compute_layout(g)
    for ic in g.interchanges:
        members = [g.stations[m] for m in ic.member_ids if m in g.stations]
        if len(members) < 2:
            continue
        x = members[0].x
        ys = [m.y for m in members]
        lo, hi = min(ys), max(ys)
        for s in g.stations.values():
            if s.is_port or s.id in ic.member_ids:
                continue
            spans = (
                abs(s.x - x) < SAME_COORD_TOLERANCE
                and lo - SAME_COORD_TOLERANCE < s.y < hi + SAME_COORD_TOLERANCE
            )
            assert not spans, (
                f"{fixture.name}: interchange {ic.node_id!r} bar (x={x:.0f}, "
                f"y {lo:.0f}..{hi:.0f}) spans station {s.id!r} at "
                f"({s.x:.0f}, {s.y:.0f})"
            )
