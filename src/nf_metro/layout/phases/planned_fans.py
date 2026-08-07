"""Materialise immutable fan plans in settled section-local frames."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x, lanes_run_along_y
from nf_metro.layout.pass_metrics import icon_half_height_approx, station_radius_approx
from nf_metro.layout.phases._common import (
    grow_section_bbox_max_edge,
    grow_section_bbox_min_edge,
)
from nf_metro.parser.model import MetroGraph, PortSide, Section, Station

if TYPE_CHECKING:
    from nf_metro.layout.route_plan import FanPlan, FanPlanId


def _planned_fans(graph: MetroGraph) -> tuple[FanPlan, ...]:
    return tuple(plan for plan in graph.fan_plans if plan.owns_geometry)


def planned_fan_layout_station_ids(graph: MetroGraph) -> set[str]:
    """Return the stations whose secondary coordinate has one semantic owner."""
    return {
        station_id
        for plan in _planned_fans(graph)
        for station_id in plan.layout_station_ids
    }


def planned_fan_layout_section_ids(graph: MetroGraph) -> set[str]:
    """Return sections containing coordinates owned by a semantic fan plan."""
    return {
        station.section_id
        for station_id in planned_fan_layout_station_ids(graph)
        if (station := graph.stations.get(station_id)) is not None
        and station.section_id is not None
    }


def planned_fan_port_ids(graph: MetroGraph) -> set[str]:
    """Return ports participating in a planned fan's complete membership."""
    return {
        port_id
        for plan in _planned_fans(graph)
        for port_id in (*plan.entry_port_ids, *plan.exit_port_ids)
    }


def _centreline_coordinate(graph: MetroGraph, plan: FanPlan) -> float | None:
    frame = plan.frame
    anchor = plan.centreline_anchor
    if frame is None or anchor is None:
        return None
    station = graph.stations.get(anchor.station_id)
    if station is None:
        from nf_metro.layout.phases.guards import PhaseInvariantError

        raise PhaseInvariantError(
            f"planned fan {plan.id!r} centreline anchor "
            f"{anchor.station_id!r} is missing"
        )
    return plan.appearance_centreline_coordinate(anchor, station)


def _apply_planned_fan_port_geometry(graph: MetroGraph) -> None:
    """Continue each settled local fan frame through same-axis boundary ports."""
    for plan in _planned_fans(graph):
        frame = plan.frame
        centreline = _centreline_coordinate(graph, plan)
        if frame is None or centreline is None:
            continue
        for port_id in plan.centreline_port_ids:
            port = graph.ports.get(port_id)
            station = graph.stations.get(port_id)
            if port is None or station is None:
                continue
            frame.secondary.set(port, centreline)
            frame.secondary.set(station, centreline)


def _snapshot_planned_fan_centrelines(
    graph: MetroGraph,
) -> Mapping[FanPlanId, float]:
    """Freeze each complete plan's centreline at a structural boundary."""
    centrelines: dict[FanPlanId, float] = {}
    for plan in _planned_fans(graph):
        if not plan.layout_station_ids:
            continue
        centreline = _centreline_coordinate(graph, plan)
        if centreline is None:
            from nf_metro.layout.phases.guards import PhaseInvariantError

            raise PhaseInvariantError(
                f"planned fan {plan.id!r} has no settled centreline"
            )
        centrelines[plan.id] = centreline
    return MappingProxyType(centrelines)


def _apply_planned_fan_geometry(
    graph: MetroGraph,
    centrelines: Mapping[FanPlanId, float],
) -> None:
    """Place every plan-owned station from its one frozen relative frame."""
    for plan in _planned_fans(graph):
        frame = plan.frame
        if frame is None or not plan.layout_station_ids:
            continue
        try:
            centreline = centrelines[plan.id]
        except KeyError as error:
            from nf_metro.layout.phases.guards import PhaseInvariantError

            raise PhaseInvariantError(
                f"planned fan {plan.id!r} has no frozen placement centreline"
            ) from error
        _materialise_plan_stations(graph.stations, plan, centreline)
        _order_straight_fan_section_tracks(graph, plan)
        _align_straight_fan_join_station(graph, plan, centreline)

        if frame.secondary.name == "y":
            graph.symfan_trunk_station_ids.update(plan.centreline_station_ids)
            graph.half_grid_station_ids.update(
                station_id
                for branch in plan.branches
                if branch.lane_offset is not None
                and abs(
                    branch.lane_offset / frame.secondary.step
                    - round(branch.lane_offset / frame.secondary.step)
                )
                > 1e-9
                for station_id in branch.lane_station_ids
            )


def _order_straight_fan_section_tracks(graph: MetroGraph, plan: FanPlan) -> None:
    """Carry a straight fan's source-lane order through vertical sections."""
    from nf_metro.layout.constants import COORD_TOLERANCE
    from nf_metro.layout.route_plan import FanAppearancePolicy

    if plan.appearance_policy is not FanAppearancePolicy.STRAIGHT:
        return
    for section in graph.sections.values():
        if not lanes_run_along_x(section.direction):
            continue
        entry_port_ids = [
            port_id
            for port_id in section.entry_ports
            if port_id in graph.ports
            and graph.ports[port_id].side in (PortSide.LEFT, PortSide.RIGHT)
        ]
        if len(entry_port_ids) != 1:
            continue
        entry_port_id = entry_port_ids[0]
        branch_tracks: list[tuple[float, tuple[str, ...], float]] = []
        for branch in plan.branches:
            if not any(
                entry_port_id in (edge.source, edge.target)
                for path in branch.resolved_paths
                for edge in path
            ):
                branch_tracks = []
                break
            station_ids = tuple(
                dict.fromkeys(
                    station_id
                    for path in branch.resolved_paths
                    for edge in path
                    for station_id in (edge.source, edge.target)
                    if (station := graph.stations.get(station_id)) is not None
                    and station.section_id == section.id
                    and station_id not in graph.ports
                )
            )
            xs = [graph.stations[station_id].x for station_id in station_ids]
            if (
                branch.lane_offset is None
                or not xs
                or max(xs) - min(xs) > COORD_TOLERANCE
            ):
                branch_tracks = []
                break
            branch_tracks.append((branch.lane_offset, station_ids, xs[0]))
        if len(branch_tracks) < 2 or len({item[2] for item in branch_tracks}) < 2:
            continue
        tracks = sorted(item[2] for item in branch_tracks)
        run_sign = 1.0 if graph.ports[entry_port_id].side is PortSide.LEFT else -1.0
        turn_sign = AxisFrame.flow_sign(section.direction)
        lane_sign = plan.appearance_lane_sign or 1.0
        if -turn_sign * run_sign * lane_sign < 0:
            tracks.reverse()
        for (_lane_offset, station_ids, current_x), target_x in zip(
            sorted(branch_tracks), tracks, strict=True
        ):
            delta = target_x - current_x
            for station_id in station_ids:
                graph.stations[station_id].x += delta


def _align_straight_fan_join_station(
    graph: MetroGraph, plan: FanPlan, centreline: float
) -> None:
    """Align a straight fan's authored join within its settled section frame."""
    from nf_metro.layout.route_plan import FanAppearancePolicy

    if (
        plan.appearance_policy is not FanAppearancePolicy.STRAIGHT
        or plan.frame is None
        or plan.frame.secondary.name != "y"
        or plan.authored_join_station_id is None
    ):
        return
    join = graph.stations.get(plan.authored_join_station_id)
    if join is None:
        return
    section = graph.sections.get(join.section_id or "")
    if section is None or not lanes_run_along_y(section.direction):
        return
    boundary_y = None
    for port_id in plan.entry_port_ids:
        port = graph.ports.get(port_id)
        if (
            port is None
            or port.section_id != section.id
            or port.side not in (PortSide.LEFT, PortSide.RIGHT)
            or not any(edge.target == join.id for edge in graph.edges_from(port_id))
        ):
            continue
        boundary_y = graph.stations[port_id].y
        break
    join.y = boundary_y if boundary_y is not None else centreline


def _materialise_plan_stations(
    stations: Mapping[str, Station],
    plan: FanPlan,
    centreline: float,
    *,
    section_id: str | None = None,
) -> None:
    """Place one plan into a station mapping from its settled centreline."""
    frame = plan.frame
    if frame is None:
        return

    def eligible(station: Station) -> bool:
        return section_id is None or station.section_id == section_id

    for station_id in plan.centreline_station_ids:
        station = stations.get(station_id)
        if station is not None and eligible(station):
            frame.secondary.set(station, centreline)
    for branch in plan.branches:
        if branch.lane_offset is None:
            continue
        coordinate = plan.appearance_coordinate(centreline, branch.lane_offset)
        for station_id in branch.lane_station_ids:
            station = stations.get(station_id)
            if station is not None and eligible(station):
                frame.secondary.set(station, coordinate)


def _fit_planned_fan_bboxes(
    graph: MetroGraph,
    section_x_padding: float,
    section_y_padding: float,
) -> bool:
    """Fit section extents to plan-owned coordinates after frame translation."""
    x_changed = False
    for plan in _planned_fans(graph):
        frame = plan.frame
        if frame is None:
            continue
        by_section: dict[str, list[str]] = {}
        for station_id in plan.layout_station_ids:
            station = graph.stations.get(station_id)
            if station is not None and station.section_id is not None:
                by_section.setdefault(station.section_id, []).append(station_id)
        for section_id, station_ids in by_section.items():
            section = graph.sections.get(section_id)
            if section is None:
                continue
            stations = [graph.stations[station_id] for station_id in station_ids]
            if frame.secondary.name == "y":
                desired_top = min(
                    station.y
                    - max(
                        section_y_padding,
                        icon_half_height_approx()
                        if station.off_track or station.is_terminus
                        else station_radius_approx(),
                    )
                    for station in stations
                )
                desired_bottom = max(
                    station.y
                    + max(
                        section_y_padding,
                        icon_half_height_approx()
                        if station.off_track or station.is_terminus
                        else station_radius_approx(),
                    )
                    for station in stations
                )
                grow_section_bbox_min_edge(graph, section, "y", desired_top)
                grow_section_bbox_max_edge(graph, section, "y", desired_bottom)
            else:
                desired_left = (
                    min(station.x for station in stations) - section_x_padding
                )
                desired_right = (
                    max(station.x for station in stations) + section_x_padding
                )
                old_left = section.bbox_x
                old_right = section.bbox_x + section.bbox_w
                grow_section_bbox_min_edge(graph, section, "x", desired_left)
                grow_section_bbox_max_edge(graph, section, "x", desired_right)
                x_changed |= (
                    section.bbox_x != old_left
                    or section.bbox_x + section.bbox_w != old_right
                )
    return x_changed


def apply_planned_fans_to_section_subgraph(
    graph: MetroGraph, subgraph: MetroGraph, section: Section
) -> None:
    """Seat plan-owned local stations before their section bbox is measured."""
    for plan in _planned_fans(graph):
        frame = plan.frame
        if frame is None or not set(plan.layout_station_ids).intersection(
            subgraph.stations
        ):
            continue
        origin = subgraph.stations.get(plan.fork_station_id)
        if origin is None:
            origin = next(
                (
                    subgraph.stations[station_id]
                    for station_id in plan.centreline_station_ids
                    if station_id in subgraph.stations
                ),
                None,
            )
        axis = frame.secondary.name
        if origin is not None:
            centreline = getattr(origin, axis)
        else:
            local_anchor = plan.local_frame_anchor
            anchor_station = subgraph.stations.get(
                local_anchor.station_id if local_anchor is not None else ""
            )
            if anchor_station is None or local_anchor is None:
                continue
            centreline = plan.appearance_centreline_coordinate(
                local_anchor, anchor_station
            )
        _materialise_plan_stations(
            subgraph.stations, plan, centreline, section_id=section.id
        )
