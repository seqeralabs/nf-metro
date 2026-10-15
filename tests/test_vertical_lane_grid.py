"""A vertical-flow section shares the lane grid a horizontal one snaps to.

Every station, bypass helper and flow-side port of an LR/RL section sits on
one grid of lanes, so a line passing a station it does not stop at clears it
by whole lanes or runs on its lane and is bowed around it.  A TB/BT section is
the same layout turned through 90 degrees, so the same must hold along X: the
BT fixture below is the LR fixture rotated counter-clockwise, lane for lane.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from nf_metro.layout.constants import X_SPACING
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import AxisFrame, flow_port_sides, lanes_run_along_x
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide, is_bypass_v

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "examples" / "topologies"


def _layout(path: Path) -> MetroGraph:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    return graph


def _lane_roles(graph: MetroGraph, section_id: str) -> dict[str, float]:
    """Lane coordinate of each station, bypass helper and flow-side port."""
    section = graph.sections[section_id]
    lane = AxisFrame.for_direction(section.direction, 1.0, 1.0).secondary
    start, end = flow_port_sides(section.direction)
    roles: dict[str, float] = {}
    for sid in section.station_ids:
        st = graph.stations[sid]
        port = graph.ports.get(sid)
        if port is not None:
            if port.side in (start, end):
                roles["entry" if port.is_entry else "exit"] = lane.get(st)
        elif is_bypass_v(sid):
            roles[f"bypass:{st.bypasses_station_id}"] = lane.get(st)
        else:
            roles[sid] = lane.get(st)
    return roles


def _lane_indices(roles: dict[str, float]) -> dict[str, float]:
    coords = sorted(set(roles.values()))
    pitch = min(b - a for a, b in zip(coords, coords[1:]))
    return {k: (v - coords[0]) / pitch for k, v in roles.items()}


def test_bt_in_section_bypass_is_the_lr_rotation() -> None:
    bt = _lane_indices(
        _lane_roles(_layout(TOPOLOGIES / "bt_in_section_bypass_lane_grid.mmd"), "align")
    )
    lr = _lane_indices(
        _lane_roles(_layout(TOPOLOGIES / "lr_in_section_bypass_lane_grid.mmd"), "align")
    )
    assert (
        set(bt)
        == set(lr)
        == {
            "index",
            "star",
            "dedup",
            "bypass:dedup",
            "entry",
            "exit",
        }
    )
    assert bt == pytest.approx(lr), f"BT lanes {bt} are not LR's {lr} rotated"
    assert all(abs(v - round(v)) < 1e-6 for v in bt.values()), bt
    assert abs(bt["bypass:dedup"] - bt["dedup"]) >= 1


def _fixtures() -> list[Path]:
    return sorted((REPO_ROOT / "examples").glob("*.mmd")) + sorted(
        TOPOLOGIES.glob("*.mmd")
    )


def _feeder_lanes(graph: MetroGraph, port_id: str, section_ids: set[str]) -> set[float]:
    sids = {e.target for e in graph.edges_from(port_id)} | {
        e.source for e in graph.edges_to(port_id)
    }
    return {
        round(graph.stations[s].x, 3)
        for s in sids
        if s in section_ids and not graph.stations[s].is_port and not is_bypass_v(s)
    }


@pytest.mark.parametrize(
    "path", _fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_vertical_flow_ports_sit_on_the_lane_grid(path: Path) -> None:
    try:
        graph = _layout(path)
    except Exception:  # noqa: BLE001 - deliberately defective fixtures abort
        pytest.skip("fixture does not lay out")
    pitch = graph._resolved_x_spacing or X_SPACING
    for section in graph.sections.values():
        if not lanes_run_along_x(section.direction):
            continue
        ids = set(section.station_ids)
        real = [
            graph.stations[s]
            for s in section.station_ids
            if not graph.stations[s].is_port
            and not graph.stations[s].is_hidden
            and not graph.stations[s].off_track
        ]
        if not real:
            continue
        origin = Counter(round(s.x % pitch, 3) for s in real).most_common(1)[0][0]
        for pid in section.entry_ports + section.exit_ports:
            port = graph.ports[pid]
            if port.side not in (PortSide.TOP, PortSide.BOTTOM):
                continue
            x = graph.stations[pid].x
            feeders = sorted(_feeder_lanes(graph, pid, ids))
            if len(feeders) >= 2 and abs(x - (feeders[0] + feeders[-1]) / 2) < 1.0:
                continue
            residue = (x - origin) % pitch
            assert min(residue, pitch - residue) < 0.5, (
                f"{path.name}: {section.direction} section {section.id!r} port "
                f"{pid!r} at x={x:.1f} sits {residue:.1f}px off its {pitch:.0f}px "
                "lane grid"
            )
