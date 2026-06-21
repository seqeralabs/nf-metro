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
import ast
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROUTING_DIR = PROJECT_ROOT / "src" / "nf_metro" / "layout" / "routing"
DOC_PATH = PROJECT_ROOT / "docs" / "dev" / "routing_gate_coverage.md"
BASELINE_PATH = PROJECT_ROOT / "tests" / "data" / "routing_gate_coverage_baseline.json"
# Curated per-gate triage verdicts, keyed by ``Gate.key``: a gap that no fixture
# can ever close because the arm is a defensive guard or unreachable. Rendered as
# the matrix's Triage column so a triaged arm is not re-investigated.
TRIAGE_PATH = PROJECT_ROOT / "tests" / "data" / "routing_gate_triage.json"

# Accepted triage verdicts. ``reachable`` is absent by design: a fixture closes
# such an arm, dropping it from the gap set rather than recording a verdict.
TRIAGE_STATUSES = frozenset({"defensive", "candidate-dead", "needs-review"})

# ``coverage`` derives a module's branch arcs from CPython bytecode, whose arc
# model shifts between interpreter versions (e.g. 3.12 splits short-circuit
# ``and``/``or`` operands into separate arms).  The matrix and baseline are
# therefore a single-interpreter artifact; regenerate and ratchet under this
# version.
BASELINE_PYTHON = (3, 11)

# Operand-level arc coverage is also sensitive to the interpreter's hash seed:
# the layout engine iterates hash-ordered sets while rendering, so which operand
# of a short-circuit ``and``/``or`` decides the branch can vary run to run even
# though the rendered SVG is identical.  Pinning the seed makes the matrix and
# baseline reproducible (the sibling of the interpreter-version pin above); the
# sweep re-execs under this value when invoked without it.
PINNED_HASH_SEED = "0"

# The gate matrix scopes to routing *decision* modules: the dispatch handlers
# and the post-routing passes.  ``invariants.py`` is the validator (its branches
# only fire under ``validate=True``, a separate test surface), and ``__init__``
# is re-exports; neither holds a topology gate.
EXCLUDED_MODULES = {"__init__.py", "invariants.py"}


def ensure_pinned_hash_seed() -> None:
    """Re-exec under :data:`PINNED_HASH_SEED` unless it is already in effect.

    Hash randomization is fixed at interpreter start, so a sweep that wants
    reproducible operand-level coverage must run under a known seed.  When the
    seed differs (the default randomized seed, or any other value) this re-execs
    the same command with the seed pinned; on the second pass the early return
    lets it proceed.
    """
    if os.environ.get("PYTHONHASHSEED") == PINNED_HASH_SEED:
        return
    os.execve(
        sys.executable,
        [sys.executable, *sys.argv],
        {**os.environ, "PYTHONHASHSEED": PINNED_HASH_SEED},
    )


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

    def to_payload(self) -> dict:
        """Serialize to the ``--json`` dump's per-gate shape."""
        return {
            "module": self.module,
            "line": self.src_line,
            "code": self.code,
            "occurrence": self.occurrence,
            "fully_covered": self.fully_covered,
            "arms": [{"dst": a.dst_line, "fixtures": a.fixtures} for a in self.arms],
        }

    @classmethod
    def from_payload(cls, entry: dict) -> Gate:
        """Reconstruct a gate from one :meth:`to_payload` entry."""
        gate = cls(
            module=entry["module"],
            src_line=entry["line"],
            code=entry["code"],
            occurrence=entry["occurrence"],
        )
        gate.arms = [GateArm(a["dst"], list(a["fixtures"])) for a in entry["arms"]]
        return gate


def _collect_corpus() -> list[tuple[Path, bool]]:
    """Every ``.mmd`` in the render corpus as ``(path, is_nextflow)``, deduped by
    content (sorted order).

    Mirrors ``tests/conftest.py``'s ``content_corpus``: the ``examples/`` tree
    (``rglob`` already subsumes ``examples/topologies`` and ``examples/guide``),
    the loose ``tests/fixtures/`` fixtures, and the Nextflow-DAG fixtures under
    ``tests/fixtures/nextflow/`` (which need ``convert_nextflow_dag`` before
    parsing). Widening past the original ``examples/``-only scope lets the test
    fixtures retire gate arms the gallery never reaches. Unlike
    ``content_corpus`` this keeps the ``rails`` fixtures -- their rail router is
    a routing path the matrix should measure, not skip.
    """
    examples = PROJECT_ROOT / "examples"
    fixtures = PROJECT_ROOT / "tests" / "fixtures"
    nextflow = fixtures / "nextflow"
    sources: list[tuple[list[Path], bool]] = [
        (sorted(examples.rglob("*.mmd")), False),
        (sorted(fixtures.glob("*.mmd")), False),
        (sorted(nextflow.glob("*.mmd")), True),
    ]
    candidates = [(p, is_nextflow) for paths, is_nextflow in sources for p in paths]

    seen: set[str] = set()
    out: list[tuple[Path, bool]] = []
    for path, is_nextflow in candidates:
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append((path, is_nextflow))
    return out


def _render_under_coverage(corpus: list[tuple[Path, bool]]):
    """Render every fixture under its own coverage context; return the data."""
    import coverage

    # ``data_file=None`` keeps the arc data in memory: the script writes no
    # ``.coverage`` file, and concurrent ratchet-test workers under ``pytest -n
    # auto`` each get a private in-memory store rather than racing one on-disk
    # sqlite file.
    cov = coverage.Coverage(branch=True, source=[str(ROUTING_DIR)], data_file=None)
    cov.start()

    # Import inside the measured region so module-level branches are attributed,
    # then render each fixture on the production path (validate=False).
    from nf_metro.convert import convert_nextflow_dag
    from nf_metro.layout.engine import compute_layout
    from nf_metro.parser.mermaid import parse_metro_mermaid
    from nf_metro.render.svg import render_svg
    from nf_metro.themes import THEMES

    for path, is_nextflow in corpus:
        ctx = str(path.relative_to(PROJECT_ROOT))
        cov.switch_context(ctx)
        try:
            text = path.read_text()
            if is_nextflow:
                text = convert_nextflow_dag(text)
            graph = parse_metro_mermaid(text)
            compute_layout(graph)
            theme = THEMES[graph.style if graph.style in THEMES else "nfcore"]
            render_svg(graph, theme)
        except Exception as exc:  # noqa: BLE001 - a broken fixture must not abort the sweep
            print(f"  WARN: {ctx} failed to render: {exc}", file=sys.stderr)

    cov.stop()
    return cov


def _expandable_boolean_conditions(
    source: str,
) -> dict[int, tuple[str, list[int], int]]:
    """Map each cleanly operand-expandable multi-line ``and``/``or`` condition to
    ``(operator, operand_lines, body_line)``, keyed by the opening ``if``/``while``
    line.

    CPython emits no branch bytecode at the opening line of a wrapped boolean
    condition -- the short-circuit branches live on the operand lines -- so the
    static arc coverage attributes to the opening line is *phantom*.  This finds
    the conditions whose operand structure is simple enough to re-attribute the
    decision to its operand lines without guessing: each operand is single-line
    and not itself a ``BoolOp``, and the operands sit on strictly increasing,
    distinct lines.  Anything more tangled (a nested ``and``/``or``, an operand
    spanning lines, two operands sharing a line) is omitted, leaving the caller
    on coverage's collapsed view rather than risking a wrong operand attribution.
    """
    out: dict[int, tuple[str, list[int], int]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.If, ast.While)):
            continue
        test = node.test
        if not isinstance(test, ast.BoolOp) or test.lineno == test.end_lineno:
            continue
        operands = test.values
        lines = [o.lineno for o in operands]
        if any(isinstance(o, ast.BoolOp) for o in operands):
            continue
        if any(o.lineno != o.end_lineno for o in operands):
            continue
        if lines != sorted(set(lines)):
            continue
        op = "or" if isinstance(test.op, ast.Or) else "and"
        out[node.lineno] = (op, lines, node.body[0].lineno)
    return out


def _operand_arms(
    dsts: list[int],
    condition: tuple[str, list[int], int],
    executed_from_operands: set[tuple[int, int]],
) -> list[tuple[int, list[int]]] | None:
    """Decompose a phantom boolean gate into one ``(operand_line, [dsts])`` per
    operand, or ``None`` to fall back to the collapsed opening-line gate.

    The collapsed gate has exactly two destinations: the body (``condition``'s
    ``body_line``) and the else/loop-back line.  Each operand short-circuits to
    one of them (``or`` -> body when true, ``and`` -> else when false) or, when
    it does not decide, falls through to the next operand line; the final operand
    takes the remaining destination.  The synthesized arcs are validated against
    the arcs actually executed from the operand lines: if any executed arc is
    unaccounted for, the model of this condition is wrong and we fall back.
    """
    op, operand_lines, body_line = condition
    if len(dsts) != 2:
        return None
    else_candidates = [d for d in dsts if d != body_line]
    if len(else_candidates) != 1:
        return None
    else_line = else_candidates[0]

    synthesized: set[tuple[int, int]] = set()
    specs: list[tuple[int, list[int]]] = []
    for i, line in enumerate(operand_lines):
        is_last = i == len(operand_lines) - 1
        if op == "or":
            decided = body_line
            fallthrough = else_line if is_last else operand_lines[i + 1]
        else:
            decided = else_line
            fallthrough = body_line if is_last else operand_lines[i + 1]
        specs.append((line, sorted({decided, fallthrough})))
        synthesized.add((line, decided))
        synthesized.add((line, fallthrough))

    if not executed_from_operands <= synthesized:
        return None
    return specs


def _build_gate(
    module: str,
    src_line: int,
    dsts: list[int],
    hitter_index: dict[tuple[int, int], list[str]],
    source_lines: list[str],
    code_seen: dict[str, int],
) -> Gate:
    """Construct a :class:`Gate` at ``src_line`` with one arm per destination.

    ``code_seen`` is the module-wide occurrence counter that keys gates by their
    source text, so it must be shared across collapsed and operand-level gates.
    """
    code = (
        source_lines[src_line - 1].strip()
        if 0 < src_line <= len(source_lines)
        else "<unknown>"
    )
    occurrence = code_seen.get(code, 0) + 1
    code_seen[code] = occurrence
    gate = Gate(module=module, src_line=src_line, code=code, occurrence=occurrence)
    for dst in sorted(dsts):
        gate.arms.append(GateArm(dst, hitter_index.get((src_line, dst), [])))
    return gate


def compute_gate_coverage() -> list[Gate]:
    """Render the corpus and return one :class:`Gate` per routing branch point.

    Each gate carries its arms, and each arm the sorted list of fixtures that
    exercise it.  An arm with no fixtures is an un-exercised gate arm.
    """
    corpus = _collect_corpus()
    fixtures = [str(p.relative_to(PROJECT_ROOT)) for p, _ in corpus]
    cov = _render_under_coverage(corpus)
    data = cov.get_data()
    measured = sorted(data.measured_files())
    reporters = {f: cov._get_file_reporter(f) for f in measured}

    # Invert the per-fixture arc sets into an ``arc -> fixtures`` index per file
    # in one pass.  Iterating fixtures in sorted order yields sorted fixture
    # lists for free.
    #
    # The tracer records the executed transition from the *physical* line that
    # holds the branch bytecode -- an operand line of a wrapped ``if (`` or the
    # first element of a multi-line body -- whereas ``FileReporter.arcs()``
    # attributes the static arc to the multi-line statement's *opening* line.
    # ``translate_arcs`` collapses each physical endpoint onto that logical
    # first line, so a genuinely-taken branch lands on the same arc the static
    # view names instead of looking un-exercised.
    # ``phys_hitters`` keeps the *untranslated* physical arcs: a wrapped boolean
    # condition's operand line is the real branch source that the translated view
    # collapses onto the opening line.
    arc_hitters: dict[str, dict[tuple[int, int], list[str]]] = {f: {} for f in measured}
    phys_hitters: dict[str, dict[tuple[int, int], list[str]]] = {
        f: {} for f in measured
    }
    for fx in fixtures:
        data.set_query_context(fx)
        for f in measured:
            raw = data.arcs(f) or []
            for arc in raw:
                phys_hitters[f].setdefault(arc, []).append(fx)
            for arc in reporters[f].translate_arcs(raw):
                arc_hitters[f].setdefault(arc, []).append(fx)

    gates: list[Gate] = []
    for f in measured:
        module = Path(f).name
        if module in EXCLUDED_MODULES:
            continue
        # ``FileReporter.arcs()`` is coverage's plugin-API view of every
        # possible branch arc; pairing it with the executed-arc index above
        # reveals which arms no fixture takes.
        possible = reporters[f].arcs()
        if not possible:
            continue
        source = Path(f).read_text()
        source_lines = source.splitlines()
        boolean_conditions = _expandable_boolean_conditions(source)
        phys_src_lines = {arc[0] for arc in phys_hitters[f]}

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

            # A wrapped boolean ``if (``/``while (`` carries no branch bytecode
            # at its opening line (no physical arc originates there); its arms
            # are phantom.  Re-attribute the decision to its operand lines so a
            # genuinely un-exercised short-circuit branch surfaces as its own
            # gate instead of hiding behind the collapsed opening-line arm.
            condition = boolean_conditions.get(src_line)
            if condition is not None and src_line not in phys_src_lines:
                operand_lines = set(condition[1])
                executed = {a for a in phys_hitters[f] if a[0] in operand_lines}
                specs = _operand_arms(dsts, condition, executed)
                if specs is not None:
                    for operand_line, operand_dsts in specs:
                        gates.append(
                            _build_gate(
                                module,
                                operand_line,
                                operand_dsts,
                                phys_hitters[f],
                                source_lines,
                                code_seen,
                            )
                        )
                    continue

            gates.append(
                _build_gate(
                    module, src_line, dsts, arc_hitters[f], source_lines, code_seen
                )
            )

    return gates


def gates_from_payload(payload: list[dict]) -> list[Gate]:
    """Reconstruct :class:`Gate` objects from the ``--json`` dump.

    Lets a caller obtain gates from a seed-pinned subprocess (see
    :data:`PINNED_HASH_SEED`) rather than computing them in-process, which keeps
    operand-level coverage reproducible.
    """
    return [Gate.from_payload(entry) for entry in payload]


def gap_keys(gates: list[Gate]) -> list[str]:
    """Stable keys of gates with at least one un-exercised arm."""
    return sorted(g.key for g in gates if not g.fully_covered)


def triage_stale_keys(
    gates: list[Gate], triage: dict[str, dict[str, str]]
) -> list[str]:
    """Triage keys that do not name a current gate with an un-exercised arm.

    A verdict is valid only while its gate stays a gap; one whose gate the
    corpus fully exercises (or whose key text has diverged from the source) is
    stale and must be removed so the sidecar cannot mis-describe a closed gate.
    """
    return sorted(set(triage) - set(gap_keys(gates)))


def load_triage() -> dict[str, dict[str, str]]:
    """Curated ``{gate key -> {status, note}}`` verdicts for un-closable gaps.

    Returns an empty mapping when the sidecar is absent (the matrix renders
    without a Triage column).  Each value's ``status`` must be one of
    :data:`TRIAGE_STATUSES`.
    """
    if not TRIAGE_PATH.exists():
        return {}
    raw = json.loads(TRIAGE_PATH.read_text())
    for key, entry in raw.items():
        status = entry.get("status")
        if status not in TRIAGE_STATUSES:
            raise ValueError(
                f"Triage entry {key!r} has status {status!r}; "
                f"expected one of {sorted(TRIAGE_STATUSES)}."
            )
    return raw


def _render_markdown(gates: list[Gate], triage: dict[str, dict[str, str]]) -> str:
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
        "Auto-generated by `scripts/routing_gate_coverage.py` under CPython "
        f"{BASELINE_PYTHON[0]}.{BASELINE_PYTHON[1]} with "
        f"`PYTHONHASHSEED={PINNED_HASH_SEED}` (the arc model is "
        "interpreter-specific and operand-level coverage is hash-seed "
        "sensitive). Do not edit by hand; run the script to regenerate."
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
    triaged = sum(1 for g in gates if not g.fully_covered and g.key in triage)
    if triage:
        out.append(
            "The Triage column carries a curated verdict for gaps no fixture "
            "can close: **defensive** (a guard arm a valid topology never "
            "violates), **candidate-dead** (no constructible topology reaches "
            "it; left in place pending a separate deletion review), or "
            "**needs-review** (not yet classified). A blank cell means the gap "
            "is still open for a fixture. "
            f"**{triaged}** gaps carry a triage verdict."
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
        out.append("| Line | Gate | Un-exercised arm(s) | Triage |")
        out.append("| ---: | --- | --- | --- |")
        for g in sorted(mod_gaps, key=lambda g: g.src_line):
            dead = ", ".join(f"`->L{a.dst_line}`" for a in g.arms if not a.covered)
            code = g.code.replace("|", "\\|")
            if len(code) > 90:
                code = code[:87] + "..."
            entry = triage.get(g.key)
            if entry:
                note = entry["note"].replace("|", "\\|")
                verdict = f"**{entry['status']}** -- {note}"
            else:
                verdict = ""
            out.append(f"| {g.src_line} | `{code}` | {dead} | {verdict} |")
        out.append("")

    return "\n".join(out) + "\n"


def main() -> int:
    ensure_pinned_hash_seed()

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

    if args.write and sys.version_info[:2] != BASELINE_PYTHON:
        sys.exit(
            f"Refusing to regenerate: the baseline is pinned to CPython "
            f"{BASELINE_PYTHON[0]}.{BASELINE_PYTHON[1]} but this is "
            f"{sys.version_info[0]}.{sys.version_info[1]}; the arc model differs."
        )

    gates = compute_gate_coverage()
    triage = load_triage()

    # ``--json`` is the machine-readable dump the ratchet test consumes; it must
    # emit even when the triage sidecar is stale so the dedicated stale-triage
    # test can report the specific offenders rather than the whole run aborting.
    if args.json:
        payload = [{**g.to_payload(), "triage": triage.get(g.key)} for g in gates]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    stale = triage_stale_keys(gates, triage)
    if stale:
        sys.exit(
            "Triage sidecar references gate(s) the corpus now fully exercises "
            "(or whose key has shifted). Remove the stale entr(y/ies) from "
            f"{TRIAGE_PATH.relative_to(PROJECT_ROOT)}:\n  " + "\n  ".join(stale)
        )

    markdown = _render_markdown(gates, triage)

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
