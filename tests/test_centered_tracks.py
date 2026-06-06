"""Tests for the ``centered`` line_spread mode.

When ``%%metro line_spread: centered`` is set, line base tracks become
symmetric about zero and shared (multi-line) stations are centred on the
mean of their lines' base tracks, so a section's weave balances above and
below the shared trunk instead of cascading downward.
"""

from __future__ import annotations

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.layers import assign_layers
from nf_metro.layout.ordering import assign_tracks
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import LineSpread

# A compact 3-line weave: a shared trunk (in -> mid -> out carried by all
# three lines) plus a pair of line-exclusive callers per line.
_WEAVE_SRC = """\
%%metro line: a | A | #4CAF50
%%metro line: b | B | #2196F3
%%metro line: c | C | #E91E63

graph LR
    in[In]
    mid[Mid]
    out[Out]
    a1[A1]
    a2[A2]
    b1[B1]
    b2[B2]
    c1[C1]
    c2[C2]

    in -->|a,b,c| mid

    mid -->|a| a1
    mid -->|a| a2
    a1 -->|a| out
    a2 -->|a| out

    mid -->|b| b1
    mid -->|b| b2
    b1 -->|b| out
    b2 -->|b| out

    mid -->|c| c1
    mid -->|c| c2
    c1 -->|c| out
    c2 -->|c| out
"""


def _weave_graph(centered: bool):
    src = _WEAVE_SRC
    if centered:
        src = "%%metro line_spread: centered\n" + src
    return parse_metro_mermaid(src)


def _trunk_ids() -> set[str]:
    return {"in", "mid", "out"}


# A fork weave where the middle line runs straight through the shared
# trunk while the top and bottom lines each split off a short exclusive run
# at the same layer, then rejoin.  The two exclusive runs are on distinct
# lines, so they form a 2-member cross-line fork group -- the case the
# fork-equalize pass repacks into consecutive tracks, collapsing the bottom
# run onto the trunk instead of leaving it on the bottom rail.  Each
# exclusive run carries two stations so we assert the whole run (not just
# the leaf) rides its line's symmetric rail.
_FORK_WEAVE_SRC = """\
%%metro line: top | Top | #4CAF50
%%metro line: mid | Mid | #2196F3
%%metro line: bot | Bot | #E91E63

graph LR
    cram[Cram]
    fb[Freebayes]
    st[Strelka]
    out[Out]

    tA[TopA]
    tB[TopB]
    bA[BotA]
    bB[BotB]

    cram -->|top,mid,bot| fb
    fb -->|top,mid,bot| st

    st -->|mid| out

    st -->|top| tA
    tA -->|top| tB
    tB -->|top| out

    st -->|bot| bA
    bA -->|bot| bB
    bB -->|bot| out
"""


def _fork_weave_graph(centered: bool):
    src = _FORK_WEAVE_SRC
    if centered:
        src = "%%metro line_spread: centered\n" + src
    return parse_metro_mermaid(src)


def test_flag_off_default():
    """Absent the directive the graph defaults to uncentered tracks."""
    graph = _weave_graph(centered=False)
    assert graph.line_spread is LineSpread.BUNDLE


def test_flag_on_parsed():
    graph = _weave_graph(centered=True)
    assert graph.line_spread is LineSpread.CENTERED


def test_flag_off_tracks_unchanged():
    """With the flag OFF, tracks match the legacy top-anchored assignment.

    The trunk stations sit on the top line's base track (0.0) and every
    exclusive caller is at or below it -- no station goes above the trunk.
    """
    graph = _weave_graph(centered=False)
    layers = assign_layers(graph)
    tracks = assign_tracks(graph, layers)

    trunk_tracks = {tracks[t] for t in _trunk_ids()}
    assert trunk_tracks == {0.0}, trunk_tracks
    # Legacy: everything stacks downward from the trunk, nothing above it.
    assert min(tracks.values()) >= 0.0


def test_flag_on_trunk_centred_and_exclusives_straddle():
    """With the flag ON, the shared trunk is centred and exclusive callers
    distribute both above AND below it."""
    graph = _weave_graph(centered=True)
    layers = assign_layers(graph)
    tracks = assign_tracks(graph, layers)

    trunk_tracks = {tracks[t] for t in _trunk_ids()}
    # The whole shared trunk shares a single (central) track.
    assert len(trunk_tracks) == 1, trunk_tracks
    trunk = next(iter(trunk_tracks))

    all_tracks = list(tracks.values())
    # Exclusive callers must straddle the trunk: at least one strictly
    # above and at least one strictly below.
    assert min(all_tracks) < trunk, (min(all_tracks), trunk)
    assert max(all_tracks) > trunk, (max(all_tracks), trunk)

    # The trunk sits near the centre of the vertical spread (within one
    # line gap of the midpoint of min/max).
    midpoint = (min(all_tracks) + max(all_tracks)) / 2
    assert abs(trunk - midpoint) < 1.0, (trunk, midpoint)


def test_flag_on_layout_validates_and_centres_y():
    """End-to-end: centred layout passes validation and the trunk's final
    Y straddles the exclusive callers."""
    graph = _weave_graph(centered=True)
    compute_layout(graph, validate=True)

    real = {
        sid: s.y
        for sid, s in graph.stations.items()
        if not s.is_port and not s.is_hidden
    }
    trunk_y = {real[t] for t in _trunk_ids()}
    # Trunk shares one Y row.
    assert len(trunk_y) == 1, trunk_y
    ty = next(iter(trunk_y))

    ys = list(real.values())
    assert min(ys) < ty, (min(ys), ty)
    assert max(ys) > ty, (max(ys), ty)


def test_guard_balanced_runs_and_no_ops():
    """The centred-tracks balance guard runs without error when on and is a
    no-op when off or under-determined."""
    from nf_metro.layout.engine import _guard_centered_line_spread_balanced

    on = _weave_graph(centered=True)
    _guard_centered_line_spread_balanced(on, "test")  # symmetric bases -> passes

    off = _weave_graph(centered=False)
    _guard_centered_line_spread_balanced(off, "test")  # flag off -> no-op

    single = parse_metro_mermaid(
        "%%metro line_spread: centered\n"
        "%%metro line: a | A | #fff\n"
        "graph LR\n x[X]\n y[Y]\n x -->|a| y\n"
    )
    _guard_centered_line_spread_balanced(single, "test")  # <2 lines -> no-op


def _line_base_sign(graph, lid: str) -> float:
    """Sign of line ``lid``'s symmetric base track (above/centre/below)."""
    order = list(graph.lines.keys())
    n = len(order)
    base = order.index(lid) - (n - 1) / 2
    return 0.0 if abs(base) < 1e-9 else (1.0 if base > 0 else -1.0)


def test_fork_weave_exclusive_runs_ride_their_line_rail():
    """A line's exclusive run must sit on its line's symmetric base rail.

    The cross-line fork (top/mid/bot exclusive runs all diverging from the
    same trunk station) used to be repacked into consecutive tracks by the
    fork-equalize pass, collapsing the bottom line's run onto the trunk.
    Each exclusive station must instead match its own line's base track, so
    the top run rides above the trunk, the bottom run below, and the middle
    run on the centre.
    """
    graph = _fork_weave_graph(centered=True)
    layers = assign_layers(graph)
    tracks = assign_tracks(graph, layers)

    line_base = {
        lid: (i - (len(graph.lines) - 1) / 2)
        for i, lid in enumerate(graph.lines.keys())
    }
    exclusive = {
        "tA": "top",
        "tB": "top",
        "bA": "bot",
        "bB": "bot",
    }
    for sid, lid in exclusive.items():
        assert tracks[sid] == line_base[lid], (
            sid,
            lid,
            tracks[sid],
            line_base[lid],
        )


def test_fork_weave_layout_each_line_run_on_correct_side():
    """End-to-end: each exclusive run's final Y sits on the correct side of
    the trunk (top above, bottom below, middle on the centre)."""
    graph = _fork_weave_graph(centered=True)
    compute_layout(graph, validate=True)

    trunk_y = {graph.stations[t].y for t in ("cram", "fb", "st", "out")}
    assert len(trunk_y) == 1, trunk_y
    ty = next(iter(trunk_y))

    runs = {"top": ("tA", "tB"), "bot": ("bA", "bB")}
    for lid, members in runs.items():
        sign = _line_base_sign(graph, lid)
        for sid in members:
            dy = graph.stations[sid].y - ty
            if sign < 0:
                assert dy < -1.0, (sid, lid, graph.stations[sid].y, ty)
            elif sign > 0:
                assert dy > 1.0, (sid, lid, graph.stations[sid].y, ty)
            else:
                assert abs(dy) < 1.0, (sid, lid, graph.stations[sid].y, ty)


def test_guard_flags_collapsed_exclusive_run():
    """The balance guard must fire when an exclusive run has collapsed onto
    the trunk midline instead of riding its line's rail."""
    import pytest

    from nf_metro.layout.engine import _guard_centered_line_spread_balanced
    from nf_metro.layout.phases.guards import PhaseInvariantError

    graph = _fork_weave_graph(centered=True)
    compute_layout(graph, validate=False)

    # Force the bottom exclusive run onto the trunk Y (the regression).
    trunk_y = graph.stations["st"].y
    for sid in ("bA", "bB"):
        graph.stations[sid].y = trunk_y

    with pytest.raises(PhaseInvariantError, match="not offset to its side"):
        _guard_centered_line_spread_balanced(graph, "test")


def test_demo_fixture_balanced_and_valid():
    """The shipped demo fixture renders, validates, and shows a centred trunk
    with callers both above and below."""
    from pathlib import Path

    src = Path(__file__).parent.parent / "examples" / "centered_tracks.mmd"
    graph = parse_metro_mermaid(src.read_text())
    assert graph.line_spread is LineSpread.CENTERED
    compute_layout(graph, validate=True)

    trunk = {"fastqc", "align", "markdup", "summary"}
    trunk_y = {graph.stations[t].y for t in trunk}
    assert len(trunk_y) == 1, trunk_y
    ty = next(iter(trunk_y))

    real_ys = [
        s.y for s in graph.stations.values() if not s.is_port and not s.is_hidden
    ]
    assert min(real_ys) < ty
    assert max(real_ys) > ty


_SECTION_OVERRIDE_SRC = """\
%%metro line: a | A | #4CAF50
%%metro line: b | B | #2196F3
%%metro line: c | C | #E91E63
%%metro line_spread: centered | weave

graph LR
    subgraph weave [Weave]
        win[In]
        wmid[Mid]
        wout[Out]
        wa[A1]
        wb[B1]
        wc[C1]

        win -->|a,b,c| wmid
        wmid -->|a| wa
        wmid -->|b| wb
        wmid -->|c| wc
        wa -->|a| wout
        wb -->|b| wout
        wc -->|c| wout
    end
"""


def test_per_section_centered_override_parses():
    """A piped directive records a per-section override, not the graph default."""
    graph = parse_metro_mermaid(_SECTION_OVERRIDE_SRC)
    assert graph.line_spread is LineSpread.BUNDLE
    assert graph.line_spread_overrides == {"weave": LineSpread.CENTERED}
    assert graph.section_line_spread("weave") is LineSpread.CENTERED
    assert graph.section_line_spread("other") is LineSpread.BUNDLE


def test_per_section_centered_override_balances_and_validates():
    """A section with a centered override balances about its trunk and passes
    the (now per-section-aware) balance guard under validate=True."""
    graph = parse_metro_mermaid(_SECTION_OVERRIDE_SRC)
    compute_layout(graph, validate=True)

    trunk_y = {graph.stations[t].y for t in ("win", "wmid", "wout")}
    assert len(trunk_y) == 1, trunk_y
    ty = next(iter(trunk_y))

    caller_ys = [graph.stations[s].y for s in ("wa", "wb", "wc")]
    assert min(caller_ys) < ty, (min(caller_ys), ty)
    assert max(caller_ys) > ty, (max(caller_ys), ty)
