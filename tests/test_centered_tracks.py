"""Tests for the opt-in balanced/centered line tracks feature.

When ``%%metro centered_tracks: true`` is set, line base tracks become
symmetric about zero and shared (multi-line) stations are centred on the
mean of their lines' base tracks, so a section's weave balances above and
below the shared trunk instead of cascading downward.
"""

from __future__ import annotations

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.layers import assign_layers
from nf_metro.layout.ordering import assign_tracks
from nf_metro.parser.mermaid import parse_metro_mermaid

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
        src = "%%metro centered_tracks: true\n" + src
    return parse_metro_mermaid(src)


def _trunk_ids() -> set[str]:
    return {"in", "mid", "out"}


def test_flag_off_default():
    """Absent the directive the graph defaults to uncentered tracks."""
    graph = _weave_graph(centered=False)
    assert graph.centered_tracks is False


def test_flag_on_parsed():
    graph = _weave_graph(centered=True)
    assert graph.centered_tracks is True


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
    from nf_metro.layout.engine import _guard_centered_tracks_balanced

    on = _weave_graph(centered=True)
    _guard_centered_tracks_balanced(on, "test")  # symmetric bases -> passes

    off = _weave_graph(centered=False)
    _guard_centered_tracks_balanced(off, "test")  # flag off -> no-op

    single = parse_metro_mermaid(
        "%%metro centered_tracks: true\n"
        "%%metro line: a | A | #fff\n"
        "graph LR\n x[X]\n y[Y]\n x -->|a| y\n"
    )
    _guard_centered_tracks_balanced(single, "test")  # <2 lines -> no-op


def test_demo_fixture_balanced_and_valid():
    """The shipped demo fixture renders, validates, and shows a centred trunk
    with callers both above and below."""
    from pathlib import Path

    src = Path(__file__).parent.parent / "examples" / "centered_tracks.mmd"
    graph = parse_metro_mermaid(src.read_text())
    assert graph.centered_tracks is True
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
