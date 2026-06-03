#!/usr/bin/env python3
"""Diff two per-phase coordinate-snapshot trees to localise a regression.

Phase snapshots are produced by ``compute_layout`` when
``NF_METRO_PHASE_SNAPSHOTS=1`` is set (see issue #363).  Each fixture's
snapshots land under ``<root>/<fixture>/<phase>.json``.  This tool compares
the same fixture across two trees (e.g. a base render and a PR render) and
reports the first phase at which station coords, port positions, or section
bboxes diverge.

Usage:
    NF_METRO_PHASE_SNAPSHOTS=1 NF_METRO_PHASE_SNAPSHOT_DIR=/tmp/base \\
        python -m nf_metro render examples/rnaseq_sections.mmd -o /tmp/base.svg
    NF_METRO_PHASE_SNAPSHOTS=1 NF_METRO_PHASE_SNAPSHOT_DIR=/tmp/pr \\
        python -m nf_metro render examples/rnaseq_sections.mmd -o /tmp/pr.svg
    python scripts/diff_phase_snapshots.py /tmp/base /tmp/pr --fixture rnaseq_sections

Without ``--fixture`` every fixture present in both trees is compared.
Exit status is 1 when any divergence is found, 0 otherwise, so the tool is
usable as a CI gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Phase ordering for "first phase to diverge".  Mirrors the call order in
# engine.compute_layout.  Phases not listed (or the sectionless "flat"
# layout) are appended afterward in lexicographic order so the tool still
# works if the pipeline gains a phase before this list is updated.
_PHASE_ORDER = [
    "flat",
    "1.1",
    "1.2",
    "1.3",
    "1.4",
    "1.5",
    "2.1",
    "3.1",
    "3.2",
    "3.3",
    "3.4",
    "3.5",
    "4.1",
    "4.2",
    "4.3",
    "4.4",
    "4.5",
    "4.6",
    "4.7",
    "4.8",
    "4.9",
    "4.10",
    "5.1",
    "5.2",
    "5.3",
    "5.4",
    "5.5",
    "6.1",
    "6.2",
    "6.3",
    "6.4",
    "6.5",
    "6.6",
    "6.7",
    "6.8",
    "6.9",
    "6.10",
    "6.11",
    "6.12",
    "6.13",
    "6.14",
    "6.15a",
    "6.15",
    "6.16",
    "final",
]


def _phase_sort_key(phase: str) -> tuple[int, str]:
    try:
        return (_PHASE_ORDER.index(phase), "")
    except ValueError:
        return (len(_PHASE_ORDER), phase)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _fixtures(root: Path) -> set[str]:
    return {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()


def _phases(fixture_dir: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted(fixture_dir.glob("*.json"))}


def _diff_payloads(base: dict[str, Any], pr: dict[str, Any]) -> list[str]:
    """Return human-readable lines describing each coord difference."""
    diffs: list[str] = []
    for kind in ("stations", "ports", "sections"):
        base_items = base.get(kind, {})
        pr_items = pr.get(kind, {})
        for key in sorted(set(base_items) | set(pr_items)):
            b = base_items.get(key)
            p = pr_items.get(key)
            if b is None:
                diffs.append(f"  {kind}/{key}: only in PR")
                continue
            if p is None:
                diffs.append(f"  {kind}/{key}: only in base")
                continue
            for field in sorted(set(b) | set(p)):
                bv = b.get(field)
                pv = p.get(field)
                if bv != pv:
                    diffs.append(f"  {kind}/{key}.{field}: {bv} -> {pv}")
    return diffs


def diff_fixture(base_dir: Path, pr_dir: Path, fixture: str) -> bool:
    """Compare one fixture's snapshot trees.  Returns True if it diverged."""
    base_fix = base_dir / fixture
    pr_fix = pr_dir / fixture
    base_phases = _phases(base_fix)
    pr_phases = _phases(pr_fix)

    if not base_phases and not pr_phases:
        print(f"[{fixture}] no snapshots found in either tree")
        return False

    only_base = set(base_phases) - set(pr_phases)
    only_pr = set(pr_phases) - set(base_phases)
    common = sorted(set(base_phases) & set(pr_phases), key=_phase_sort_key)

    diverged = False
    for phase in common:
        diffs = _diff_payloads(_load(base_phases[phase]), _load(pr_phases[phase]))
        if diffs:
            print(f"[{fixture}] FIRST DIVERGENCE at phase {phase}:")
            for line in diffs[:40]:
                print(line)
            if len(diffs) > 40:
                print(f"  ... and {len(diffs) - 40} more")
            diverged = True
            break

    if not diverged:
        if only_base or only_pr:
            print(
                f"[{fixture}] coords match on all shared phases; "
                f"phase sets differ (base-only={sorted(only_base)}, "
                f"pr-only={sorted(only_pr)})"
            )
            diverged = True
        else:
            print(f"[{fixture}] no divergence across {len(common)} phases")
    return diverged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base", type=Path, help="base snapshot tree root")
    parser.add_argument("pr", type=Path, help="PR snapshot tree root")
    parser.add_argument(
        "--fixture",
        help="fixture slug to compare; omit to compare all shared fixtures",
    )
    args = parser.parse_args(argv)

    if args.fixture:
        fixtures = [args.fixture]
    else:
        fixtures = sorted(_fixtures(args.base) & _fixtures(args.pr))
        if not fixtures:
            print("no shared fixtures between the two trees")
            return 1

    any_diverged = False
    for fixture in fixtures:
        if diff_fixture(args.base, args.pr, fixture):
            any_diverged = True
    return 1 if any_diverged else 0


if __name__ == "__main__":
    sys.exit(main())
