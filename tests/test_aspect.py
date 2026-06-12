"""Aspect-ratio targeting: candidate enumeration, the search, and the CLI flag."""

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.aspect import AspectSolution, solve_aspect
from nf_metro.cli import cli
from nf_metro.layout.auto_layout import candidate_fold_thresholds
from nf_metro.parser import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _wide_section_chain(n_sections: int = 6) -> str:
    """A linear chain of sections, four station-columns each, wide enough that
    the layout can fold into a range of distinct shapes."""
    lines = ["%%metro line: a | A | #0570b0", "graph LR"]
    for i in range(n_sections):
        lines.append(f"    subgraph s{i} [Section {i}]")
        for j in range(4):
            lines.append(f"        n{i}_{j}[N{i}{j}]")
        for j in range(3):
            lines.append(f"        n{i}_{j} -->|a| n{i}_{j + 1}")
        lines.append("    end")
    for i in range(n_sections - 1):
        lines.append(f"    n{i}_3 -->|a| n{i + 1}_0")
    return "\n".join(lines) + "\n"


# --- solve_aspect (pure search) ------------------------------------------


def test_solve_aspect_picks_closest_in_log_space():
    """The candidate whose aspect is nearest the target (log distance) wins."""
    dims = {1: (100, 400), 2: (200, 200), 3: (400, 100)}  # aspects 0.25, 1, 4
    sol = solve_aspect(1.1, [1, 2, 3], lambda f: dims[f])
    assert sol.fold_threshold == 2
    assert sol.achieved_aspect == pytest.approx(1.0)


def test_solve_aspect_log_symmetry():
    """A 2x-too-wide and a 2x-too-tall candidate are equally far from target."""
    dims = {1: (100, 200), 2: (400, 200)}  # aspects 0.5 and 2.0, target 1.0
    # Equal log distance -> the smaller fold_threshold breaks the tie.
    sol = solve_aspect(1.0, [2, 1], lambda f: dims[f])
    assert sol.fold_threshold == 1


def test_solve_aspect_unreachable_target_returns_closest():
    """A target outside the achievable range yields the nearest extreme."""
    dims = {1: (100, 100), 2: (200, 100)}  # aspects 1.0, 2.0
    sol = solve_aspect(10.0, [1, 2], lambda f: dims[f])
    assert sol.fold_threshold == 2
    assert sol.achieved_aspect == pytest.approx(2.0)


def test_solve_aspect_no_candidates_not_adjustable():
    sol = solve_aspect(4.0, [], lambda f: (0.0, 0.0))
    assert sol == AspectSolution(adjustable=False, target=4.0)


def test_solve_aspect_one_shape_not_adjustable():
    """When fold has no effect (one shape for every candidate) it is not adjustable."""
    calls = []

    def measure(fold):
        calls.append(fold)
        return (300, 100)

    sol = solve_aspect(3.0, [4, 8, 16], measure)
    assert sol.adjustable is False
    assert calls == [4, 8, 16]


def test_solve_aspect_dedupes_identical_dimensions():
    """Folds that render to the same size collapse to the smallest fold."""
    dims = {4: (300, 100), 8: (300, 100), 16: (100, 300)}
    sol = solve_aspect(3.0, [4, 8, 16], lambda f: dims[f])
    assert sol.fold_threshold == 4


# --- candidate_fold_thresholds -------------------------------------------


def test_candidate_thresholds_wide_chain():
    """A six-column chain yields one candidate per column plus 1, ascending,
    spanning maximum fold (1) up to the single-row total."""
    graph = parse_metro_mermaid(_wide_section_chain(6))
    cands = candidate_fold_thresholds(graph)
    assert cands[0] == 1
    assert len(cands) == 7  # six columns + the always-present 1
    assert cands == sorted(set(cands))
    assert cands[-1] == max(cands)


def test_candidate_thresholds_single_section_empty():
    graph = parse_metro_mermaid(
        "graph LR\n  subgraph s [S]\n    a[A] -->|x| b[B]\n  end\n"
    )
    assert candidate_fold_thresholds(graph) == []


def test_candidate_thresholds_explicit_grid_empty():
    """An author-pinned grid is never re-folded, so there is nothing to search."""
    chain = _wide_section_chain(3)
    pinned = "%%metro grid: s0 | 0,0\n" + chain
    graph = parse_metro_mermaid(pinned)
    assert graph._explicit_grid
    assert candidate_fold_thresholds(graph) == []


# --- directive + CLI wiring ----------------------------------------------


def test_aspect_directive_parsed():
    graph = parse_metro_mermaid("%%metro aspect: 4\ngraph LR\n")
    assert graph.aspect == pytest.approx(4.0)


def _svg_dims(text: str) -> tuple[int, int]:
    m = re.search(r'width="(\d+(?:\.\d+)?)"\s+height="(\d+(?:\.\d+)?)"', text)
    assert m is not None
    return int(float(m.group(1))), int(float(m.group(2)))


@pytest.mark.parametrize(
    "source_name", ["rnaseq_auto.mmd", "variantprioritization.mmd"]
)
def test_aspect_wide_beats_tall(tmp_path, source_name):
    """A wide target renders a higher width/height ratio than a tall target on
    a fold-driven pipeline (one the folder can reshape)."""
    src = EXAMPLES_DIR / source_name
    runner = CliRunner()

    wide = tmp_path / "wide.svg"
    tall = tmp_path / "tall.svg"
    r_wide = runner.invoke(cli, ["render", str(src), "-o", str(wide), "--aspect", "8"])
    r_tall = runner.invoke(
        cli, ["render", str(src), "-o", str(tall), "--aspect", "0.3"]
    )
    assert r_wide.exit_code == 0, r_wide.output
    assert r_tall.exit_code == 0, r_tall.output

    w_wide, h_wide = _svg_dims(wide.read_text())
    w_tall, h_tall = _svg_dims(tall.read_text())
    assert (w_wide / h_wide) > (w_tall / h_tall)


def test_aspect_explicit_fold_wins(tmp_path):
    """An explicit --fold-threshold suppresses the aspect search entirely."""
    src = EXAMPLES_DIR / "rnaseq_auto.mmd"
    out = tmp_path / "out.svg"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["render", str(src), "-o", str(out), "--aspect", "8", "--fold-threshold", "8"],
    )
    assert result.exit_code == 0, result.output
    assert "Aspect" not in result.output


def test_aspect_fixed_topology_reports_and_renders(tmp_path):
    """An author-pinned grid cannot be reshaped; the flag is reported, not applied."""
    src = EXAMPLES_DIR / "sarek_metro.mmd"
    out = tmp_path / "out.svg"
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(src), "-o", str(out), "--aspect", "4"])
    assert result.exit_code == 0, result.output
    assert "fixed by its topology" in result.output
    assert out.exists()
