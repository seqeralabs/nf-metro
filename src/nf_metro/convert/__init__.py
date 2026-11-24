"""Convert workflow-manager DAG exports to nf-metro ``.mmd`` format.

Supported inputs, keyed by the ``input_format`` name the CLI and API take:

``nextflow``
    ``nextflow run ... -preview -with-dag dag.mmd`` (mermaid).
``snakemake`` (experimental)
    ``snakemake --rulegraph`` or ``--dag``, as DOT or ``mermaid-js``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, get_args

from nf_metro.convert._core import (
    LINE_COLORS,
    LINE_MODES,
    ExperimentalInputWarning,
    FeedbackEdgesDroppedWarning,
    LineMode,
)
from nf_metro.convert.nextflow import (
    convert_nextflow_dag,
    is_nextflow_dag,
    process_node_labels,
)
from nf_metro.convert.snakemake import convert_snakemake_dag, is_snakemake_dag

InputFormat = Literal["nextflow", "snakemake"]
"""A workflow manager whose DAG export nf-metro can convert."""

INPUT_FORMATS: tuple[InputFormat, ...] = get_args(InputFormat)

_CONVERTERS: dict[InputFormat, Callable[..., str]] = {
    "nextflow": convert_nextflow_dag,
    "snakemake": convert_snakemake_dag,
}

_DETECTORS: tuple[tuple[InputFormat, Callable[[str], bool]], ...] = (
    # Snakemake's mermaid-js export is also a flowchart, so it is tried first.
    ("snakemake", is_snakemake_dag),
    ("nextflow", is_nextflow_dag),
)


def detect_input_format(text: str) -> InputFormat | None:
    """Name the workflow manager whose DAG export *text* is, or ``None``."""
    for name, matches in _DETECTORS:
        if matches(text):
            return name
    return None


def convert_dag(
    text: str,
    input_format: InputFormat,
    title: str = "",
    lines: LineMode | None = None,
) -> str:
    """Convert a DAG export in *input_format* to nf-metro ``.mmd`` text.

    *lines* chooses how lines are drawn (see :data:`LineMode`); ``None`` keeps
    the format's own default.
    """
    converter = _CONVERTERS.get(input_format)
    if converter is None:
        raise ValueError(
            f"unknown input format {input_format!r}; "
            f"expected one of {', '.join(INPUT_FORMATS)}"
        )
    return converter(text, title) if lines is None else converter(text, title, lines)


__all__ = [
    "INPUT_FORMATS",
    "LINE_MODES",
    "InputFormat",
    "LineMode",
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
