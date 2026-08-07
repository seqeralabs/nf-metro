"""Immutable geometry used to create SVG and HTML output."""
# ruff: noqa: ANN401

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from nf_metro.parser.model import LineSpread, MetroGraph
from nf_metro.text_metrics import MetricsFace


@dataclass(frozen=True)
class FrozenMap(Mapping[Any, Any]):
    """An immutable mapping that preserves insertion order."""

    entries: tuple[tuple[Any, Any], ...]
    _index: Mapping[Any, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_index", MappingProxyType(dict(self.entries)))

    def __getitem__(self, key: Any) -> Any:
        return self._index[key]

    def __iter__(self) -> Iterator[Any]:
        return (key for key, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class FrozenRecord:
    """An immutable copy of one parser or layout value."""

    kind: str
    values: FrozenMap

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "values")
        try:
            return values[name]
        except KeyError as exc:
            if self.kind == "Station":
                if name == "is_terminus":
                    return bool(self.terminus_labels)
                if name == "is_blank_terminus":
                    return bool(self.terminus_labels) and not self.label.strip()
                if name == "is_captioned_terminus":
                    return (
                        bool(self.terminus_labels)
                        and not self.label.strip()
                        and any(self.terminus_names)
                    )
                if name == "terminus_caption_line_count":
                    return max(
                        (
                            caption.count("\n") + 1
                            for caption in self.terminus_names
                            if caption
                        ),
                        default=0,
                    )
            if self.kind == "Section" and name == "port_ids":
                return frozenset((*self.entry_ports, *self.exit_ports))
            raise AttributeError(name) from exc


@dataclass(frozen=True)
class FrozenGraph(FrozenRecord):
    """The final, read-only graph stored in a ``RenderPlan``."""

    _station_lines: FrozenMap = field(init=False, repr=False, compare=False)
    _edges_from: FrozenMap = field(init=False, repr=False, compare=False)
    _edges_to: FrozenMap = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        station_lines: dict[str, set[str]] = {}
        edges_from: dict[str, list[FrozenRecord]] = {}
        edges_to: dict[str, list[FrozenRecord]] = {}
        for edge in self.edges:
            station_lines.setdefault(edge.source, set()).add(edge.line_id)
            station_lines.setdefault(edge.target, set()).add(edge.line_id)
            edges_from.setdefault(edge.source, []).append(edge)
            edges_to.setdefault(edge.target, []).append(edge)
        object.__setattr__(
            self,
            "_station_lines",
            FrozenMap(
                tuple(
                    (station_id, tuple(sorted(line_ids)))
                    for station_id, line_ids in station_lines.items()
                )
            ),
        )
        object.__setattr__(
            self,
            "_edges_from",
            FrozenMap(
                tuple(
                    (station_id, tuple(station_edges))
                    for station_id, station_edges in edges_from.items()
                )
            ),
        )
        object.__setattr__(
            self,
            "_edges_to",
            FrozenMap(
                tuple(
                    (station_id, tuple(station_edges))
                    for station_id, station_edges in edges_to.items()
                )
            ),
        )

    def station_lines(self, station_id: str) -> list[str]:
        return list(self._station_lines.get(station_id, ()))

    def station_lines_ordered(self, station_id: str) -> list[str]:
        served = set(self.station_lines(station_id))
        return [line_id for line_id in self.lines if line_id in served]

    def edges_from(self, station_id: str) -> list[FrozenRecord]:
        return list(self._edges_from.get(station_id, ()))

    def edges_to(self, station_id: str) -> list[FrozenRecord]:
        return list(self._edges_to.get(station_id, ()))

    def station_for_edge_source(self, edge: FrozenRecord) -> FrozenRecord:
        return self.stations[edge.source]

    def station_for_edge_target(self, edge: FrozenRecord) -> FrozenRecord:
        return self.stations[edge.target]

    def station_is_rail(self, station_id: str) -> bool:
        station = self.stations.get(station_id)
        section_id = station.section_id if station is not None else None
        return self.section_line_spread(section_id) is LineSpread.RAILS

    def section_line_spread(self, section_id: str | None) -> LineSpread:
        if section_id is not None and section_id in self.line_spread_overrides:
            return self.line_spread_overrides[section_id]
        return self.line_spread

    @property
    def has_rail_sections(self) -> bool:
        return self.line_spread is LineSpread.RAILS or any(
            mode is LineSpread.RAILS for mode in self.line_spread_overrides.values()
        )

    @property
    def real_sections(self) -> dict[str, FrozenRecord]:
        return {
            section_id: section
            for section_id, section in self.sections.items()
            if not section.is_implicit
        }


@dataclass(frozen=True)
class RenderPlan:
    """All data needed to render a map, stored in SVG pixels."""

    theme: FrozenRecord
    graph: FrozenGraph
    metrics_face: MetricsFace
    station_offsets: FrozenMap
    routes: tuple[FrozenRecord, ...]
    route_polylines: tuple[tuple[tuple[float, float], ...], ...]
    edge_route_indices: tuple[int, ...]
    bridge_breaks: tuple[tuple[FrozenRecord, ...], ...]
    labels: tuple[FrozenRecord, ...]
    header_placements: FrozenMap
    group_bands: tuple[FrozenRecord, ...]
    positive_fan_sections: frozenset[str]
    svg_width: int
    svg_height: int
    padding: float
    legend_x: float
    legend_y: float
    legend_w: float
    legend_h: float
    show_legend: bool
    show_logo: bool
    logo_in_legend: bool
    adaptive_logo: bool
    effective_logo: str
    resolved_logo_light: str
    resolved_logo_dark: str
    logo_x: float
    logo_y: float
    logo_w: float
    logo_h: float
    legend_logo_size: tuple[float, float] | None
    manifest: FrozenMap | None
    debug: bool
    chrome_css: bool
    bare: bool

    @property
    def edge_routes(self) -> tuple[FrozenRecord, ...]:
        """Return routes in SVG drawing order."""
        return tuple(self.routes[index] for index in self.edge_route_indices)

    def offset_polylines(
        self,
    ) -> tuple[tuple[str, tuple[tuple[float, float], ...]], ...]:
        """Return the polylines used to draw each metro line."""
        return tuple(
            (route.line_id, points)
            for route, points in zip(self.routes, self.route_polylines)
        )


_RENDER_GRAPH_EXCLUDED_FIELDS = {
    "layout_provenance",
    "route_resolution",
    "route_topology",
    "_route_topology_query",
    "fan_plan_execution",
    "_station_lines_cache",
    "_edges_from_cache",
    "_edges_to_cache",
    "_junction_ids_cache",
}
_RENDER_ROUTE_EXCLUDED_FIELDS = {
    "exit_turn_plan_id",
    "exit_turn_member_id",
    "exit_turn_family_id",
    "exit_turn_axis_id",
    "exit_turn_segment_rank",
    "exit_lane_transition_plan_id",
    "fan_plan_id",
    "fan_route_emitter",
    "route_system_id",
    "emission_member_id",
    "route_system_disposition",
    "route_plan_ids",
    "route_reservation_ids",
    "route_system_owned_segment_ranks",
}


def freeze_render_value(value: Any) -> Any:
    """Copy a render value into immutable containers."""
    if isinstance(value, (str, bytes, int, float, bool, type(None), Enum)):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(
            tuple(
                (freeze_render_value(key), freeze_render_value(item))
                for key, item in value.items()
            )
        )
    if isinstance(value, (list, tuple)):
        if hasattr(value, "_fields"):
            return FrozenRecord(
                type(value).__name__,
                FrozenMap(
                    tuple(
                        (name, freeze_render_value(getattr(value, name)))
                        for name in value._fields
                    )
                ),
            )
        return tuple(freeze_render_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_render_value(item) for item in value)
    if is_dataclass(value):
        from nf_metro.layout.routing.common import RoutedPath

        record_type = FrozenGraph if isinstance(value, MetroGraph) else FrozenRecord
        if isinstance(value, MetroGraph):
            excluded = _RENDER_GRAPH_EXCLUDED_FIELDS
        elif isinstance(value, RoutedPath):
            excluded = _RENDER_ROUTE_EXCLUDED_FIELDS
        else:
            excluded = set()
        return record_type(
            type(value).__name__,
            FrozenMap(
                tuple(
                    (field.name, freeze_render_value(getattr(value, field.name)))
                    for field in fields(value)
                    if field.name not in excluded
                )
            ),
        )
    raise TypeError(f"RenderPlan cannot freeze {type(value).__name__}")


def thaw_render_value(value: Any) -> Any:
    """Convert plan containers to values that JSON can serialize."""
    if isinstance(value, FrozenMap):
        return {
            thaw_render_value(key): thaw_render_value(item)
            for key, item in value.entries
        }
    if isinstance(value, FrozenRecord):
        return thaw_render_value(value.values)
    if isinstance(value, tuple | frozenset):
        return [thaw_render_value(item) for item in value]
    return value


def contains_mutable_model_reference(value: Any) -> bool:
    """Return whether a value contains a mutable parser or layout object."""
    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.parser.model import Edge, Port, Section, Station

    model_types = (MetroGraph, Station, Section, Edge, Port, RoutedPath)
    if isinstance(value, model_types):
        return True
    if isinstance(value, FrozenMap):
        return any(
            contains_mutable_model_reference(key)
            or contains_mutable_model_reference(item)
            for key, item in value.entries
        )
    if isinstance(value, FrozenRecord):
        return contains_mutable_model_reference(value.values)
    if isinstance(value, RenderPlan):
        return any(
            contains_mutable_model_reference(getattr(value, field.name))
            for field in fields(value)
        )
    if isinstance(value, tuple | frozenset):
        return any(contains_mutable_model_reference(item) for item in value)
    return False
