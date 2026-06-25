"""BT (bottom-to-top) orientation: the vertical mirror of TB.

Locks the rotation wiring that makes BT a real flow direction: the
:class:`AxisFrame` flow/lane signs, the bottom-to-top station order, and the
property that a BT section is a TB section reflected on its flow axis.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import AxisFrame
from nf_metro.parser.mermaid import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
TOPOLOGIES_DIR = EXAMPLES_DIR / "topologies"


def _chain(direction: str) -> str:
    return (
        "%%metro title: Chain\n"
        "%%metro line: a | A | #e63946\n"
        "graph LR\n"
        "    subgraph work [Work]\n"
        f"        %%metro direction: {direction}\n"
        "        w1[First]\n"
        "        w2[Second]\n"
        "        w3[Third]\n"
        "        w1 -->|a| w2\n"
        "        w2 -->|a| w3\n"
        "    end\n"
    )


def _laid_out(text: str):
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    return graph


def test_bt_frame_is_tb_reflected_on_flow_axis() -> None:
    """BT shares TB's axes but reverses the flow sign and mirrors the lane sign."""
    tb = AxisFrame.for_direction("TB", 60.0, 40.0)
    bt = AxisFrame.for_direction("BT", 60.0, 40.0)

    assert (bt.primary.name, bt.secondary.name) == (tb.primary.name, tb.secondary.name)
    assert bt.primary_sign == -tb.primary_sign
    assert bt.secondary_sign == -tb.secondary_sign


def test_bt_chain_flows_bottom_to_top() -> None:
    """Each step along a BT chain lands above (smaller Y than) the previous one."""
    graph = _laid_out(_chain("BT"))
    ys = [graph.stations[s].y for s in ("w1", "w2", "w3")]
    assert ys[0] > ys[1] > ys[2], ys


def test_bt_chain_is_tb_chain_mirrored_on_y() -> None:
    """A BT chain's station Ys are its TB chain's reflected within the column."""
    tb = _laid_out(_chain("TB"))
    bt = _laid_out(_chain("BT"))

    ids = ("w1", "w2", "w3")
    tb_ys = [tb.stations[s].y for s in ids]
    bt_ys = [bt.stations[s].y for s in ids]
    span = min(tb_ys) + max(tb_ys)
    assert bt_ys == [span - y for y in tb_ys]
    # Same X column: BT changes only the flow sense, not the lane placement.
    assert [tb.stations[s].x for s in ids] == [bt.stations[s].x for s in ids]


def test_bt_chain_fixture_renders() -> None:
    """The shipped BT chain fixture lays out without aborting."""
    graph = _laid_out((TOPOLOGIES_DIR / "bt_chain.mmd").read_text())
    work = graph.sections["work"]
    assert work.direction == "BT"
