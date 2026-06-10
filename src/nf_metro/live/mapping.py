"""Station<->process matching and the check-mapping fidelity linter.

A metro station is a curated abstraction that usually stands for several
Nextflow processes (often a whole subworkflow), so the mapping declared by
``%%metro process:`` directives is many-to-one and matched by regular
expression against the fully-qualified process name.

The linter's job is to make *drift* loud: a process the map can't show, or a
station pattern that matches nothing, is a stale map that would silently
mislead a live view. ``check_mapping`` reports both so CI can gate on them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nf_metro.manifest import matching_node_ids


def stations_for_process(process: str, mapping: dict[str, list[str]]) -> list[str]:
    """Station ids whose patterns match ``process`` (case-insensitive)."""
    return matching_node_ids(process, mapping)


def process_names_from_dag(text: str) -> list[str]:
    """Process names from a Nextflow ``-with-dag`` Mermaid flowchart.

    Thin wrapper over :func:`nf_metro.convert.process_node_labels`.
    """
    from nf_metro.convert import process_node_labels

    return process_node_labels(text)


@dataclass
class MappingReport:
    """Outcome of :func:`check_mapping`.

    ``unmapped_processes``, ``dead_patterns`` and ``ambiguous_processes`` are
    failures (drift); they drive the linter's non-zero exit. ``ambiguous_processes``
    maps a process to the >1 stations it matched: the mapping is meant to be
    many-to-one (a station stands for several processes, not the reverse), so a
    process lighting up more than one station duplicates that task's progress on
    the map. ``unmapped_stations`` is informational - a station with no pattern
    simply never lights up, which may be intentional.
    """

    unmapped_processes: list[str] = field(default_factory=list)
    dead_patterns: list[tuple[str, str]] = field(default_factory=list)
    ambiguous_processes: dict[str, list[str]] = field(default_factory=dict)
    unmapped_stations: list[str] = field(default_factory=list)
    matched: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when there is no drift (failures), ignoring warnings."""
        return not (
            self.unmapped_processes or self.dead_patterns or self.ambiguous_processes
        )


def check_mapping(
    mapping: dict[str, list[str]],
    station_ids: list[str],
    process_names: list[str],
    ignore: list[str] | None = None,
) -> MappingReport:
    """Diff a station->process mapping against the pipeline's real processes.

    Args:
        mapping: ``station_id -> [regex, ...]`` from the map's ``process:``
            directives (``MetroGraph.process_mapping``).
        station_ids: every real (non-port) station id in the map.
        process_names: process names the pipeline actually runs (from a DAG
            export or a captured run).
        ignore: regexes for processes deliberately left unmapped (plumbing such
            as ``.*:DUMPSOFTWAREVERSIONS``); matches are excluded from
            ``unmapped_processes``.

    Returns:
        A :class:`MappingReport`.
    """
    ignore_res = [re.compile(p, re.IGNORECASE) for p in (ignore or [])]
    report = MappingReport(unmapped_stations=sorted(set(station_ids) - set(mapping)))

    matched_patterns: set[tuple[str, str]] = set()
    for process in process_names:
        hits = stations_for_process(process, mapping)
        if hits:
            report.matched.setdefault(process, []).extend(hits)
            for station_id in hits:
                for pattern in mapping[station_id]:
                    if re.search(pattern, process, re.IGNORECASE):
                        matched_patterns.add((station_id, pattern))
        elif not any(r.search(process) for r in ignore_res):
            report.unmapped_processes.append(process)

    for station_id, patterns in mapping.items():
        for pattern in patterns:
            if (station_id, pattern) not in matched_patterns:
                report.dead_patterns.append((station_id, pattern))

    report.ambiguous_processes = {
        process: stations
        for process in sorted(report.matched)
        if len(stations := sorted(set(report.matched[process]))) > 1
    }
    report.unmapped_processes.sort()
    report.dead_patterns.sort()
    return report
