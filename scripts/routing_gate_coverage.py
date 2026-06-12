#!/usr/bin/env python3
"""Map every topology-conditional gate in ``layout/routing/`` to the fixtures
that exercise each of its arms.

The routing subpackage dispatches each edge through priority-ordered handlers
and sequential post-passes.  Every ``if``/``while`` in those modules is a
*gate* with two arms; a gate written for the topologies in hand can fire (or
fail to fire) on a novel topology and produce a visual defect.  This tool
renders the whole ``examples/`` corpus under per-fixture branch coverage,
restricted to the routing modules, and reports for each gate arm which
fixtures reach it -- turning "every new pipeline stress-tests every implicit
assumption" into a finite, enumerated checklist.

Usage::

    python scripts/routing_gate_coverage.py            # print markdown matrix
    python scripts/routing_gate_coverage.py --write     # regenerate doc + baseline
    python scripts/routing_gate_coverage.py --json      # machine-readable dump

The reusable entry point is :func:`compute_gate_coverage`, imported by
``tests/test_routing_gate_coverage.py`` to ratchet the gap set: a new gate
must ship with a fixture hitting both arms, and closing a gap tightens the
baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROUTING_DIR = PROJECT_ROOT / "src" / "nf_metro" / "layout" / "routing"
DOC_PATH = PROJECT_ROOT / "docs" / "dev" / "routing_gate_coverage.md"
BASELINE_PATH = PROJECT_ROOT / "tests" / "data" / "routing_gate_coverage_baseline.json"

# The gate matrix scopes to routing *decision* modules: the dispatch handlers
# and the post-routing passes.  ``invariants.py`` is the validator (its branches
# only fire under ``validate=True``, a separate test surface), and ``__init__``
# is re-exports; neither holds a topology gate.
EXCLUDED_MODULES = {"__init__.py", "invariants.py"}


@dataclass
class GateArm:
    """One arm of a gate: the branch to ``dst_line`` and the fixtures taking it."""

    dst_line: int
    fixtures: list[str] = field(default_factory=list)

    @property
    def covered(self) -> bool:
        return bool(self.fixtures)


@dataclass
class Gate:
    """A single branch point (``if``/``while``/comprehension filter)."""

    module: str
    src_line: int
    code: str
    occurrence: int  # 1-based index of this code text within the module
    arms: list[GateArm] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Line-shift-stable identity: module + source text + occurrence."""
        return f"{self.module}::{self.code}::#{self.occurrence}"

    @property
    def fully_covered(self) -> bool:
        return all(a.covered for a in self.arms)


def _collect_corpus() -> list[Path]:
    """All ``examples/`` ``.mmd`` fixtures, deduped by content (sorted order)."""
    paths = sorted(p for p in (PROJECT_ROOT / "examples").rglob("*.mmd"))
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(p)
    return out


def _render_under_coverage(corpus: list[Path]):
    """Render every fixture under its own coverage context; return the data."""
    import coverage

    cov = coverage.Coverage(branch=True, source=[str(ROUTING_DIR)])
    cov.start()

    # Import inside the measured region so module-level branches are attributed,
    # then render each fixture on the production path (validate=False).
    from nf_metro.layout.engine import compute_layout
    from nf_metro.parser.mermaid import parse_metro_mermaid
    from nf_metro.render.svg import render_svg
    from nf_metro.themes import THEMES

    for path in corpus:
        ctx = str(path.relative_to(PROJECT_ROOT))
        cov.switch_context(ctx)
        try:
            graph = parse_metro_mermaid(path.read_text())
            compute_layout(graph)
            theme = THEMES[graph.style if graph.style in THEMES else "nfcore"]
            render_svg(graph, theme)
        except Exception as exc:  # noqa: BLE001 - a broken fixture must not abort the sweep
            print(f"  WARN: {ctx} failed to render: {exc}", file=sys.stderr)

    cov.stop()
    return cov


def compute_gate_coverage() -> list[Gate]:
    """Render the corpus and return one :class:`Gate` per routing branch point.

    Each gate carries its arms, and each arm the sorted list of fixtures that
    exercise it.  An arm with no fixtures is an un-exercised gate arm.
    """
    corpus = _collect_corpus()
    fixtures = [str(p.relative_to(PROJECT_ROOT)) for p in corpus]
    cov = _render_under_coverage(corpus)
    data = cov.get_data()
    measured = sorted(data.measured_files())

    # Invert the per-fixture arc sets into an ``arc -> fixtures`` index per file
    # in one pass.  Iterating fixtures in sorted order yields sorted fixture
    # lists for free.
    arc_hitters: dict[str, dict[tuple[int, int], list[str]]] = {f: {} for f in measured}
    for fx in fixtures:
        data.set_query_context(fx)
        for f in measured:
            for arc in data.arcs(f) or []:
                arc_hitters[f].setdefault(arc, []).append(fx)

    gates: list[Gate] = []
    for f in measured:
        module = Path(f).name
        if module in EXCLUDED_MODULES:
            continue
        # ``FileReporter.arcs()`` is coverage's plugin-API view of every
        # possible branch arc; pairing it with the executed-arc index above
        # reveals which arms no fixture takes.
        possible = cov._get_file_reporter(f).arcs()
        if not possible:
            continue
        source_lines = Path(f).read_text().splitlines()

        # Group possible arcs by source line; a gate is a line with >=2 arms.
        arcs_by_src: dict[int, list[int]] = {}
        for src, dst in possible:
            if src < 0:
                continue
            arcs_by_src.setdefault(src, []).append(dst)

        code_seen: dict[str, int] = {}
        for src_line in sorted(arcs_by_src):
            dsts = arcs_by_src[src_line]
            if len(dsts) < 2:
                continue  # not a branch point
            code = (
                source_lines[src_line - 1].strip()
                if 0 < src_line <= len(source_lines)
                else "<unknown>"
            )
            occurrence = code_seen.get(code, 0) + 1
            code_seen[code] = occurrence

            gate = Gate(
                module=module, src_line=src_line, code=code, occurrence=occurrence
            )
            for dst in sorted(dsts):
                hitters = arc_hitters[f].get((src_line, dst), [])
                gate.arms.append(GateArm(dst, hitters))
            gates.append(gate)

    return gates


def gap_keys(gates: list[Gate]) -> list[str]:
    """Stable keys of gates with at least one un-exercised arm."""
    return sorted(g.key for g in gates if not g.fully_covered)


def _render_markdown(gates: list[Gate]) -> str:
    by_module: dict[str, list[Gate]] = {}
    for g in gates:
        by_module.setdefault(g.module, []).append(g)

    total = len(gates)
    covered = sum(1 for g in gates if g.fully_covered)
    gaps = total - covered

    out: list[str] = []
    out.append("# Routing gate coverage matrix")
    out.append("")
    out.append(
        "Auto-generated by `scripts/routing_gate_coverage.py`. Do not edit by "
        "hand; run the script to regenerate."
    )
    out.append("")
    out.append(
        "Each row is a branch point (a *gate*) in a `layout/routing/` dispatch "
        "handler or post-pass. A gate has two or more arms; the cells list how "
        "many corpus fixtures exercise each arm. An arm reached by **0 "
        "fixtures** is an un-exercised gate arm: either no shipped topology "
        "takes that path (author a fixture, or confirm it is defensive/dead)."
    )
    out.append("")
    out.append(
        f"**{covered}/{total}** gates fully exercised (both/all arms hit by some "
        f"fixture); **{gaps}** gates have at least one un-exercised arm."
    )
    out.append("")
    out.append(
        "Modules scoped to routing decision gates; `invariants.py` (the "
        "`validate=True` checker) and `__init__.py` are excluded."
    )
    out.append("")

    for module in sorted(by_module):
        mod_gates = by_module[module]
        mod_gaps = [g for g in mod_gates if not g.fully_covered]
        out.append(f"## `{module}`")
        out.append("")
        out.append(
            f"{len(mod_gates) - len(mod_gaps)}/{len(mod_gates)} gates fully exercised."
        )
        out.append("")
        if not mod_gaps:
            out.append("All gates have every arm exercised by the corpus.")
            out.append("")
            continue
        out.append("Gates with an un-exercised arm:")
        out.append("")
        out.append("| Line | Gate | Un-exercised arm(s) |")
        out.append("| ---: | --- | --- |")
        for g in sorted(mod_gaps, key=lambda g: g.src_line):
            dead = ", ".join(f"`->L{a.dst_line}`" for a in g.arms if not a.covered)
            code = g.code.replace("|", "\\|")
            if len(code) > 90:
                code = code[:87] + "..."
            out.append(f"| {g.src_line} | `{code}` | {dead} |")
        out.append("")

    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate the committed matrix doc and ratchet baseline.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the full matrix as JSON to stdout."
    )
    args = parser.parse_args()

    gates = compute_gate_coverage()

    if args.json:
        payload = [
            {
                "module": g.module,
                "line": g.src_line,
                "code": g.code,
                "occurrence": g.occurrence,
                "fully_covered": g.fully_covered,
                "arms": [{"dst": a.dst_line, "fixtures": a.fixtures} for a in g.arms],
            }
            for g in gates
        ]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    markdown = _render_markdown(gates)

    if args.write:
        DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOC_PATH.write_text(markdown)
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(gap_keys(gates), indent=2) + "\n")
        print(f"Wrote {DOC_PATH.relative_to(PROJECT_ROOT)}")
        print(f"Wrote {BASELINE_PATH.relative_to(PROJECT_ROOT)}")
    else:
        sys.stdout.write(markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
