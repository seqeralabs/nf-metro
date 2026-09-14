"""The ``validate`` and ``render`` commands accept the same set of maps.

``validate`` exists to tell an author whether a map is good before rendering
it, so a map it rejects must not render and a map it accepts must not fail to
parse.  Each case below goes through both commands: their acceptance is
compared, then pinned against the expected verdict so the pair cannot agree on
the wrong answer.
"""

import subprocess
import warnings
from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.parser import parse_metro_mermaid

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Raw Nextflow DAG output, the input side of ``nf-metro convert``: flowchart
# syntax with no ``%%metro`` directives, which the parser rejects by design.
_CONVERTER_INPUT = "tests/fixtures/nextflow"

_DECLARED = "%%metro line: main | Main | #0570b0\n"

_GRAPH = """graph LR
    subgraph sec [Section]
        a[A]
        b[B]
        {edge}
    end
"""


def _map(edge: str, prologue: str = "") -> str:
    return prologue + _GRAPH.format(edge=edge)


# ``offender`` is the line id both commands must name in their diagnostic, or
# ``None`` when the case carries no undeclared line.
CASES = [
    pytest.param(_map("a -->|main| b", _DECLARED), True, None, id="clean"),
    # No ``%%metro line:`` directive at all, so the annotation names nothing.
    pytest.param(_map("a -->|main| b"), False, "main", id="no-declarations"),
    pytest.param(
        _map("a -->|other| b", _DECLARED),
        False,
        "other",
        id="undeclared-alongside-declared",
    ),
    # A ``%%metro line:`` directive too short to carry a name and a colour, so
    # its id never becomes usable.
    pytest.param(
        _map("a -->|main| b", "%%metro line: main\n"),
        False,
        "main",
        id="rejected-declaration",
    ),
    pytest.param(_map("a --> b", _DECLARED), False, None, id="unannotated-edge"),
]


@pytest.mark.parametrize(("source", "accepted", "offender"), CASES)
def test_validate_and_render_agree(
    tmp_path: Path, source: str, accepted: bool, offender: str | None
) -> None:
    path = tmp_path / "map.mmd"
    path.write_text(source)
    runner = CliRunner()

    validated = runner.invoke(cli, ["validate", str(path)])
    rendered = runner.invoke(cli, ["render", str(path), "-o", str(tmp_path / "o.svg")])

    assert (validated.exit_code == 0) == (rendered.exit_code == 0), (
        f"validate exited {validated.exit_code} and render exited "
        f"{rendered.exit_code}\nvalidate: {validated.output}\n"
        f"render: {rendered.output}"
    )
    assert (validated.exit_code == 0) is accepted, validated.output
    if offender is not None:
        assert offender in validated.output
        assert offender in rendered.output


def _tracked_maps() -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "*.mmd"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        rel
        for rel in listing.stdout.split()
        if not rel.startswith(_CONVERTER_INPUT) and (PROJECT_ROOT / rel).is_file()
    ]


def test_every_shipped_map_parses() -> None:
    """No shipped map may sit on the wrong side of a parse-time rejection.

    A map that stops parsing goes dark rather than red wherever a harness
    records the exception as data instead of failing, so the corpus is checked
    against the parser directly.
    """
    failures = {}
    for rel in _tracked_maps():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                parse_metro_mermaid((PROJECT_ROOT / rel).read_text())
        except Exception as exc:
            failures[rel] = f"{type(exc).__name__}: {exc}"

    assert not failures, "shipped map(s) fail to parse:\n" + "\n".join(
        f"  {rel}: {msg}" for rel, msg in sorted(failures.items())
    )


def test_tracked_map_listing_is_nonempty() -> None:
    """The corpus check is vacuous if the listing comes back empty."""
    assert len(_tracked_maps()) > 300
