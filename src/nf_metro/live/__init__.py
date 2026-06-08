"""Live-progress support: map Nextflow execution onto a metro map.

The :mod:`~nf_metro.live.mapping` module holds the pure station<->process
matching and the check-mapping linter; :mod:`~nf_metro.live.server` holds the
HTTP/SSE server that drives a status overlay from Nextflow ``-with-weblog``
events. Both are wired to the ``nf-metro serve`` and ``nf-metro check-mapping``
CLI commands.
"""

from nf_metro.live.mapping import (
    MappingReport,
    check_mapping,
    process_names_from_dag,
    stations_for_process,
)

__all__ = [
    "MappingReport",
    "check_mapping",
    "process_names_from_dag",
    "stations_for_process",
]
