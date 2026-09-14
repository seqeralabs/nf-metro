"""Section headers must never be drawn across a routed metro line (issue #774).

A line entering a section through an edge under its top-left header would cross
the title text.  The placement chain relocates the header (below, rotated onto a
side, or shifted along the band above the box) instead of routing the line around
the title.

Covers:

* Happy-path: every shipped example and topology fixture places every section
  header clear of every route.
* Meaningfulness: with header relocation disabled (the resolver pinned to its
  default above-left position) the new fixtures clash, proving the chain - not
  coincidence - is what keeps them clear.
* Ranking: a caption held in the band above its box keeps at least the room from
  route ink that the bottom edge it declined would have given it, measured off
  the drawn map rather than by re-running the resolver's own search.
* Accounting: whichever side a caption takes, the room the placed boxes leave on
  that side holds its ink.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.render.section_header as section_header
from nf_metro.api import resolve_theme
from nf_metro.layout.constants import SECTION_HEADER_PROTRUSION
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, Section
from nf_metro.render.constants import (
    SECTION_HEADER_ROUTE_PAD,
    SECTION_NUM_CIRCLE_R_LARGE,
)
from nf_metro.render.section_header import (
    check_section_headers_clear_routes,
    check_section_headers_fit_box_width,
    check_section_headers_hold_the_reserved_band,
    resolve_all_section_headers,
    resolve_section_header_placement,
)
from nf_metro.render.svg import apply_route_offsets

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
FIXTURE_TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"

RELOCATION_FIXTURES = [
    EXAMPLE_TOPOLOGIES / "top_entry_header_clash.mmd",
    EXAMPLE_TOPOLOGIES / "header_side_rotated.mmd",
    EXAMPLE_TOPOLOGIES / "header_nudge.mmd",
]


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLE_TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURE_TOPOLOGIES.glob("*.mmd")))
    return paths


def _polylines_and_font(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    polylines = [apply_route_offsets(route, offsets) for route in routes]
    theme = resolve_theme(None, graph)
    return graph, polylines, theme.section_label_font_size, theme.title_font_size


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_section_header_route_clashes_in_gallery(path: Path) -> None:
    """Every section header clears every route across the shipped corpus."""
    graph, polylines, font_size, title_font_size = _polylines_and_font(path)
    placements = resolve_all_section_headers(
        graph, font_size, polylines, title_font_size
    )
    clashes = check_section_headers_clear_routes(placements, polylines)
    assert not clashes, "\n".join(c.message() for c in clashes)


@pytest.mark.parametrize("path", RELOCATION_FIXTURES, ids=lambda p: p.stem)
def test_default_above_placement_would_clash(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinning every header to its default above-left position reintroduces the
    clash on the relocation fixtures, so the chain is doing real work."""
    monkeypatch.setattr(section_header, "_placement_clear", lambda *a, **k: True)
    graph, polylines, font_size, title_font_size = _polylines_and_font(path)
    placements = resolve_all_section_headers(
        graph, font_size, polylines, title_font_size
    )
    clashes = check_section_headers_clear_routes(placements, polylines)
    assert clashes, "expected an above-left header to clash with the route"


def test_nudge_clears_a_route_to_the_right_of_the_box() -> None:
    """The nudge fallback must clear routes crossing to the right of the box.

    A header nudged right occupies ``[start, start + length]``; a route crossing
    its vertical band anywhere in that span must be stepped past, so the nudge
    consults the full width to its right rather than only the box-width extent.
    Leaving a route inside the nudged keepout hard-aborts a slightly crowded map
    on the render-time guard.

    ``above``/``below`` are blocked by a full-height trunk and the side columns
    do not fit the short box, so the resolver falls through to ``nudge``.  The
    trunk fixes the nudge origin; a second route sits to the right of the box,
    past the un-nudged header's right edge, inside the nudged header's span."""
    graph = MetroGraph()
    section = Section(id="s", name="Alignment")
    section.bbox_x, section.bbox_y = 0.0, 100.0
    section.bbox_w, section.bbox_h = 97.0, 18.0
    graph.sections["s"] = section

    trunk = [(90.0, 70.0), (90.0, 150.0)]
    right_route = [(150.0, 70.0), (150.0, 105.0)]
    polylines = [trunk, right_route]

    placement = resolve_section_header_placement(
        graph, section, label_font_size=13.0, polylines=polylines, title_font_size=13.0
    )
    assert placement.mode == "nudge"
    clashes = check_section_headers_clear_routes({"s": placement}, polylines)
    assert not clashes, "\n".join(c.message() for c in clashes)


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_section_header_fits_box_width_in_gallery(path: Path) -> None:
    """Every horizontal section header stays within its box width.

    A title wider than its box must wrap onto extra lines rather than
    overhang the box's right edge."""
    graph, polylines, font_size, title_font_size = _polylines_and_font(path)
    placements = resolve_all_section_headers(
        graph, font_size, polylines, title_font_size
    )
    overflowing = check_section_headers_fit_box_width(graph, placements)
    assert not overflowing, f"headers overhanging their box: {overflowing}"


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_section_header_never_crosses_box_border_in_gallery(path: Path) -> None:
    """A horizontal header's extra wrapped lines never draw across its own
    section box's border - they grow away from the box, not into it."""
    graph, polylines, font_size, title_font_size = _polylines_and_font(path)
    placements = resolve_all_section_headers(
        graph, font_size, polylines, title_font_size
    )
    crossings = []
    for section_id, placement in placements.items():
        section = graph.sections.get(section_id)
        if section is None or placement.label_rotation:
            continue
        if placement.mode in ("above", "nudge"):
            if placement.keepout[3] > section.bbox_y + 0.01:
                crossings.append(section_id)
        elif placement.mode == "below":
            box_bottom = section.bbox_y + section.bbox_h
            if placement.keepout[1] < box_bottom - 0.01:
                crossings.append(section_id)
    assert not crossings, f"headers crossing their box border: {crossings}"


def _min_route_distance(keepout, polylines) -> float:
    """Least distance from any routed segment to ``keepout``, sampled densely.

    Sampling rather than a closed form keeps this independent of the resolver's
    own distance function, so a bug shared with it cannot hide here.
    """
    x0, y0, x1, y1 = keepout
    best = float("inf")
    for poly in polylines:
        for (ax, ay), (bx, by) in zip(poly, poly[1:]):
            for i in range(129):
                t = i / 128
                px, py = ax + t * (bx - ax), ay + t * (by - ay)
                dx = max(x0 - px, 0.0, px - x1)
                dy = max(y0 - py, 0.0, py - y1)
                best = min(best, (dx * dx + dy * dy) ** 0.5)
    return best


def _side_room(graph: MetroGraph, section: Section, mode: str) -> float:
    """Room the placed boxes leave on the side ``mode`` hangs off.

    Re-derived here from the section rectangles alone: down to the nearest box
    above (never below the reserved protrusion, which the layout guarantees and
    the badge occupies whatever else stands there), up to the nearest box below
    less the badge protrusion that box reserves for its own header, or out to the
    nearest box beside.  Unbounded where nothing stands that way and the canvas
    grows to fit.
    """
    left, top = section.bbox_x, section.bbox_y
    right, bottom = left + section.bbox_w, top + section.bbox_h
    others = [
        o
        for o in graph.sections.values()
        if o.id != section.id and o.bbox_w > 0 and o.bbox_h > 0
    ]
    cols = [o for o in others if o.bbox_x < right - 0.5 and left < o.bbox_x + o.bbox_w]
    rows = [o for o in others if o.bbox_y < bottom - 0.5 and top < o.bbox_y + o.bbox_h]
    if mode in ("above", "nudge"):
        ceilings = [o.bbox_y + o.bbox_h for o in cols if o.bbox_y + o.bbox_h <= top]
        return max(SECTION_HEADER_PROTRUSION, top - max(ceilings, default=0.0))
    if mode == "below":
        floors = [o.bbox_y for o in cols if o.bbox_y >= bottom]
        if not floors:
            return float("inf")
        return min(floors) - SECTION_HEADER_PROTRUSION - bottom
    if mode == "left":
        walls = [o.bbox_x + o.bbox_w for o in rows if o.bbox_x + o.bbox_w <= left]
        return left - max(walls, default=0.0)
    walls = [o.bbox_x for o in rows if o.bbox_x >= right]
    return min(walls) - right if walls else float("inf")


def _protrusion(section: Section, placement) -> float:
    """How far ``placement``'s ink reaches past the box edge it hangs off."""
    x0, y0, x1, y1 = placement.keepout
    if placement.mode in ("above", "nudge"):
        return section.bbox_y - y0
    if placement.mode == "below":
        return y1 - (section.bbox_y + section.bbox_h)
    if placement.mode == "left":
        return section.bbox_x - x0
    return x1 - (section.bbox_x + section.bbox_w)


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_every_caption_fits_the_band_its_own_side_leaves(path: Path) -> None:
    """Whichever side a caption takes, the room on that side holds it.

    The clearance claim follows the caption: a caption below or beside its box is
    accounted for by the gap it actually occupies, so what has to hold is that
    the gap is deep enough - no other box, and no badge protrusion another box
    reserves for its own header, standing in the caption's ink.
    """
    graph, polylines, font_size, title_font_size = _polylines_and_font(path)
    placements = resolve_all_section_headers(
        graph, font_size, polylines, title_font_size
    )
    for section_id, placement in placements.items():
        section = graph.sections[section_id]
        room = _side_room(graph, section, placement.mode)
        reach = _protrusion(section, placement)
        assert reach <= room + 0.5, (
            f"{section_id} caption reaches {reach:.2f}px past its "
            f"{placement.mode} edge into {room:.2f}px of room"
        )


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_a_band_caption_is_never_tighter_than_the_edge_it_declined(path: Path) -> None:
    """A caption kept in the band above its box keeps at least as much room from
    route ink as the bottom edge it passed over would have given it.

    This is the ranking read back off the drawn map.  A caption squeezed into a
    contested band beside a descending stroke fails it whenever the bottom edge
    stands clear, which is the whole of the top-entry family.
    """
    graph, polylines, font_size, title_font_size = _polylines_and_font(path)
    placements = resolve_all_section_headers(
        graph, font_size, polylines, title_font_size
    )
    for section_id, placement in placements.items():
        if placement.mode != "nudge":
            continue
        section = graph.sections[section_id]
        taken = _min_route_distance(placement.keepout, polylines)
        length = placement.keepout[2] - placement.keepout[0]
        depth = placement.keepout[3] - placement.keepout[1]
        box_bottom = section.bbox_y + section.bbox_h
        below = (
            section.bbox_x,
            box_bottom,
            section.bbox_x + length,
            box_bottom + depth,
        )
        if depth > _side_room(graph, section, "below") + 0.5:
            continue
        rival = _min_route_distance(below, polylines)
        if rival < SECTION_HEADER_ROUTE_PAD:
            continue
        assert taken >= rival - 0.5, (
            f"{section_id} was held in its band with {taken:.2f}px of route "
            f"clearance while the bottom edge offered {rival:.2f}px"
        )


def test_a_contested_band_loses_to_the_roomier_bottom_edge() -> None:
    """A stroke descending through the default position and leaving only a
    pinched slot in the band drops the caption to the clear bottom edge."""
    graph = MetroGraph()
    section = Section(id="s", name="Work")
    section.bbox_x, section.bbox_y = 0.0, 100.0
    section.bbox_w, section.bbox_h = 200.0, 80.0
    graph.sections["s"] = section

    # Two risers straddling the box width: each crosses the band, and the gap
    # between them is only just wide enough for the header.
    header_width = section_header._header_length("Work", 13.0)
    left_riser = [(4.0, 60.0), (4.0, 140.0)]
    right_riser = [(header_width + 14.0, 60.0), (header_width + 14.0, 140.0)]
    placement = resolve_section_header_placement(
        graph,
        section,
        label_font_size=13.0,
        polylines=[left_riser, right_riser],
        title_font_size=13.0,
    )

    assert placement.mode == "below"
    assert placement.keepout[1] >= section.bbox_y + section.bbox_h - 0.01
    below_clearance = _min_route_distance(placement.keepout, [left_riser, right_riser])
    assert below_clearance > 3.0 * SECTION_HEADER_ROUTE_PAD


def test_a_roomy_band_slot_keeps_the_caption_above_its_box() -> None:
    """A band with a wide clear stretch keeps the caption above its box, centred
    in that stretch rather than hugging the stroke it stepped past."""
    graph = MetroGraph()
    section = Section(id="s", name="Work")
    section.bbox_x, section.bbox_y = 0.0, 100.0
    section.bbox_w, section.bbox_h = 600.0, 80.0
    graph.sections["s"] = section

    riser = [(10.0, 60.0), (10.0, 140.0)]
    placement = resolve_section_header_placement(
        graph, section, label_font_size=13.0, polylines=[riser], title_font_size=13.0
    )

    assert placement.mode == "nudge"
    assert placement.badge_cy - SECTION_NUM_CIRCLE_R_LARGE >= (
        section.bbox_y - SECTION_HEADER_PROTRUSION - 0.01
    )
    assert placement.keepout[3] <= section.bbox_y + 0.01
    assert placement.keepout[2] <= section.bbox_x + section.bbox_w + 0.5
    # Centred in the gap the riser leaves, not parked one pad's width past it.
    clearance = _min_route_distance(placement.keepout, [riser])
    assert clearance > 10.0 * SECTION_HEADER_ROUTE_PAD


def test_the_band_guard_reports_a_caption_deeper_than_its_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning the resolver to the bottom edge of a box whose gap below is too
    shallow strands a caption the guard names, so the guard is what holds the
    accounting rather than the ranking happening to agree with it."""
    graph = MetroGraph()
    upper = Section(id="u", name="Upper work")
    upper.bbox_x, upper.bbox_y, upper.bbox_w, upper.bbox_h = 0.0, 100.0, 300.0, 80.0
    lower = Section(id="l", name="Lower work")
    lower.bbox_x, lower.bbox_y, lower.bbox_w, lower.bbox_h = 0.0, 210.0, 300.0, 80.0
    graph.sections["u"] = upper
    graph.sections["l"] = lower

    placements = {
        "u": section_header._below(
            upper.bbox_x,
            upper.bbox_y + upper.bbox_h,
            section_header._BandBlock(
                circle_r=SECTION_NUM_CIRCLE_R_LARGE,
                num_y=4.0,
                length=120.0,
                half_text=10.4,
                lines=["Upper work"],
                extra_height=0.0,
                height_capped=False,
            ),
        )
    }
    assert check_section_headers_hold_the_reserved_band(graph, placements, 13.0) == [
        "u"
    ]


def test_narrow_section_header_wraps_onto_multiple_lines() -> None:
    """A title wider than its box splits onto multiple lines."""
    path = EXAMPLE_TOPOLOGIES / "narrow_section_header_wrap.mmd"
    graph, polylines, font_size, title_font_size = _polylines_and_font(path)
    placements = resolve_all_section_headers(
        graph, font_size, polylines, title_font_size
    )
    placement = placements["wide_name"]
    assert len(placement.label_lines) > 1
    for line in placement.label_lines:
        assert section_header.estimate_section_label_width(line, font_size) <= (
            graph.sections["wide_name"].bbox_w
        )


def _horizontal_placement(
    label_lines: tuple[str, ...], font_size: float
) -> section_header.SectionHeaderPlacement:
    """A horizontal ``above`` header whose keepout width matches its widest line.

    Mirrors :func:`section_header._wrapped_header_geometry`: the header spans the
    badge plus the widest wrapped line, so the keepout width is what the
    box-width guard measures against ``bbox_w``.
    """
    text_width = max(
        section_header.estimate_section_label_width(line, font_size)
        for line in label_lines
    )
    width = section_header._badge_span() + text_width
    return section_header.SectionHeaderPlacement(
        mode="above",
        badge_cx=0.0,
        badge_cy=0.0,
        label_x=0.0,
        label_y=0.0,
        label_rotation=0.0,
        label_lines=label_lines,
        keepout=(0.0, -10.0, width, 0.0),
    )


def _single_station_section(section_id: str, bbox_w: float) -> Section:
    section = Section(id=section_id, name="Novel transcripts")
    section.bbox_x, section.bbox_y = 0.0, 100.0
    section.bbox_w, section.bbox_h = bbox_w, 40.0
    return section


def test_wrapped_header_with_unbreakable_widest_line_is_exempt() -> None:
    """A multi-line header whose widest line is a single unbreakable token is not
    reported: no wrap could narrow that line, so the overhang is unavoidable.

    "Novel transcripts" over a single-station box too narrow for "transcripts"
    on its own line is such a shape.
    """
    font_size = 13.0
    label_lines = ("Novel", "transcripts")
    placement = _horizontal_placement(label_lines, font_size)
    section = _single_station_section("novel_transcripts", bbox_w=100.0)
    assert placement.keepout[2] - placement.keepout[0] > section.bbox_w, (
        "test premise: the widest line must overhang the box"
    )

    graph = MetroGraph()
    graph.sections["novel_transcripts"] = section
    assert (
        check_section_headers_fit_box_width(graph, {"novel_transcripts": placement})
        == []
    )


def test_wrapped_header_with_breakable_widest_line_still_reported() -> None:
    """A multi-line header whose widest line holds an unused break point is
    reported: a correct wrap would have split it, so the overhang is fixable and
    the exemption must not swallow it."""
    font_size = 13.0
    widest = "transcript variants"
    assert len(section_header._header_wrap_tokens(widest)) > 1
    placement = _horizontal_placement(("Novel", widest), font_size)
    text_width = section_header.estimate_section_label_width(widest, font_size)
    section = _single_station_section("wide_break", bbox_w=text_width * 0.6)
    assert placement.keepout[2] - placement.keepout[0] > section.bbox_w

    graph = MetroGraph()
    graph.sections["wide_break"] = section
    assert check_section_headers_fit_box_width(graph, {"wide_break": placement}) == [
        "wide_break"
    ]
