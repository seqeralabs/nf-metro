"""The shared :mod:`nf_metro.api` entry points must match the CLI byte-for-byte.

:func:`render_string` is the path the browser playground renders through, so it
has to resolve the option cascade identically to ``nf-metro render`` - otherwise
a map looks different in the editor than on the command line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.api import prepare_graph, render_string
from nf_metro.cli import cli
from nf_metro.parser import CyclicGraphError

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

PARITY_FIXTURES = [
    "rnaseq_auto.mmd",
    "rnaseq_sections.mmd",
    "variantbenchmarking_auto.mmd",
]


def _cli_render(src: Path, out: Path, *args: str) -> str:
    result = CliRunner().invoke(cli, ["render", str(src), "-o", str(out), *args])
    assert result.exit_code == 0, result.output
    return out.read_text()


def _as_written(content: str) -> str:
    """Mirror the CLI's file-write normalization (a single trailing newline)."""
    return content if content.endswith("\n") else content + "\n"


@pytest.mark.parametrize("name", PARITY_FIXTURES)
def test_render_string_matches_cli_svg(name: str, tmp_path: Path) -> None:
    src = EXAMPLES / name
    cli_out = _cli_render(src, tmp_path / "cli.svg")
    assert cli_out == _as_written(render_string(src.read_text()))


@pytest.mark.parametrize("name", PARITY_FIXTURES)
def test_render_string_matches_cli_html(name: str, tmp_path: Path) -> None:
    src = EXAMPLES / name
    out = tmp_path / "cli.html"
    cli_out = _cli_render(src, out, "--format", "html")
    api_out = render_string(
        src.read_text(), output_format="html", embed_basename=out.name
    )
    assert cli_out == _as_written(api_out)


def test_render_string_honours_theme_and_layout_options() -> None:
    src = (EXAMPLES / "rnaseq_auto.mmd").read_text()
    light = render_string(src, theme="light")
    dark = render_string(src, theme="nfcore")
    assert light != dark

    narrow = render_string(src, layout_options={"x_spacing": 60.0})
    wide = render_string(src, layout_options={"x_spacing": 200.0})
    assert narrow != wide


def test_prepare_graph_returns_settled_graph() -> None:
    graph = prepare_graph((EXAMPLES / "rnaseq_auto.mmd").read_text())
    assert graph.stations
    # compute_layout has run: every real station carries coordinates.
    assert all(s.x is not None and s.y is not None for s in graph.stations.values())


def test_render_string_propagates_layout_error() -> None:
    cyclic = (
        "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n  n2 -->|a| n1\n"
    )
    with pytest.raises(CyclicGraphError):
        render_string(cyclic)
