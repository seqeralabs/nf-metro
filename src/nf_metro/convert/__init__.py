"""Convert workflow-manager DAG exports to nf-metro ``.mmd`` format.

Supported inputs, keyed by the ``input_format`` name the CLI and API take:

``nextflow``
    ``nextflow run ... -preview -with-dag dag.mmd`` (mermaid).
``snakemake`` (experimental)
    ``snakemake --rulegraph`` or ``--dag``, as DOT or ``mermaid-js``.
"""

from __future__ import annotations

from collections.abc import Callable

from nf_metro.convert._core import (
    LINE_COLORS,
    ExperimentalInputWarning,
    FeedbackEdgesDroppedWarning,
)
from nf_metro.convert.nextflow import (
    convert_nextflow_dag,
    is_nextflow_dag,
    process_node_labels,
)
from nf_metro.convert.snakemake import convert_snakemake_dag, is_snakemake_dag

_CONVERTERS: dict[str, Callable[[str, str], str]] = {
    "nextflow": convert_nextflow_dag,
    "snakemake": convert_snakemake_dag,
}

INPUT_FORMATS: tuple[str, ...] = tuple(_CONVERTERS)
"""Names accepted as ``input_format``."""

_DETECTORS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    # Snakemake's mermaid-js export is also a flowchart, so it is tried first.
    ("snakemake", is_snakemake_dag),
    ("nextflow", is_nextflow_dag),
)


def detect_input_format(text: str) -> str | None:
    """Name the workflow manager whose DAG export *text* is, or ``None``."""
    for name, matches in _DETECTORS:
        if matches(text):
            return name
    return None


def convert_dag(text: str, input_format: str, title: str = "") -> str:
    """Convert a DAG export in *input_format* to nf-metro ``.mmd`` text."""
    try:
        converter = _CONVERTERS[input_format]
    except KeyError:
        raise ValueError(
            f"unknown input format {input_format!r}; "
            f"expected one of {', '.join(INPUT_FORMATS)}"
        ) from None
    return converter(text, title)


__all__ = [
    "INPUT_FORMATS",
    "LINE_COLORS",
    "ExperimentalInputWarning",
    "FeedbackEdgesDroppedWarning",
    "convert_dag",
    "convert_nextflow_dag",
    "convert_snakemake_dag",
    "detect_input_format",
    "is_nextflow_dag",
    "is_snakemake_dag",
    "process_node_labels",
]
