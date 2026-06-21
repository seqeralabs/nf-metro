#!/usr/bin/env python3
"""Time every ``validate=True`` guard and routing check over the corpus.

The guard suite is gated behind ``validate=True``, which the ``render`` path
never sets, so end users get no protection from the ``_guard_*`` functions in
``phases/guards.py`` or the ``check_*`` invariants in ``routing/invariants.py``.
Promoting a cheap subset to always-on (see ``docs/dev/guard_tiers.md``) needs a
measured per-guard cost so the always-on tier can be picked on evidence rather
than guesswork.

This tool lays out each fixture once (``validate=False``), computes the shared
``offsets`` / ``routes`` a guard run inspects, then times each registered guard
and each routing check against that final state.  It reports a per-guard table
(total + mean-per-fixture microseconds, raise count) sorted by cost and a tier
proposal: guards cheaper than ``--tier-a-threshold`` mean microseconds are
flagged as Tier-A (always-on) candidates.

Usage::

    python scripts/guard_cost_audit.py                 # examples/ + topologies
    python scripts/guard_cost_audit.py --json out.json
    python scripts/guard_cost_audit.py --repeats 5     # average noisy timings
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nf_metro.layout.engine import compute_layout  # noqa: E402
from nf_metro.layout.phases.guards import GUARD_REGISTRY  # noqa: E402
from nf_metro.layout.routing import (  # noqa: E402
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.invariants import CHECK_REGISTRY  # noqa: E402
from nf_metro.parser.mermaid import parse_metro_mermaid  # noqa: E402


def discover_fixtures() -> list[Path]:
    """Every ``%%metro``-format ``.mmd`` under ``examples/`` + ``topologies``.

    Mirrors ``tests/test_engine_guards_perf._discover_fixtures`` but restricted
    to the shipping examples corpus the audit targets.
    """
    roots = [REPO_ROOT / "examples", REPO_ROOT / "examples" / "topologies"]
    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.glob("*.mmd")):
            if p in seen:
                continue
            if "%%metro" not in p.read_text(errors="ignore"):
                continue
            seen.add(p)
            out.append(p)
    return out


def _time_call(fn: Callable[..., Any], repeats: int) -> tuple[float, bool]:
    """Return ``(min_seconds, raised)`` over ``repeats`` calls of ``fn``."""
    best = float("inf")
    raised = False
    for _ in range(repeats):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:  # noqa: BLE001 - a raising guard is a valid outcome
            raised = True
        best = min(best, time.perf_counter() - t0)
    return best, raised


def audit(fixtures: list[Path], repeats: int) -> dict[str, dict[str, Any]]:
    """Accumulate per-guard / per-check timings across the fixture corpus."""
    stats: dict[str, dict[str, Any]] = {}

    def record(kind: str, name: str, seconds: float, raised: bool) -> None:
        s = stats.setdefault(
            name,
            {"kind": kind, "total_s": 0.0, "fixtures": 0, "raises": 0},
        )
        s["total_s"] += seconds
        s["fixtures"] += 1
        s["raises"] += int(raised)

    tier_by_name = {spec.name: spec.tier for spec in (*GUARD_REGISTRY, *CHECK_REGISTRY)}

    for path in fixtures:
        graph = parse_metro_mermaid(path.read_text())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compute_layout(graph, validate=False)
        offsets = compute_station_offsets(graph)
        try:
            routes = route_edges(graph, station_offsets=offsets)
        except Exception:  # noqa: BLE001
            routes = None
        pool: dict[str, Any] = {
            "graph": graph,
            "routes": routes,
            "offsets": offsets,
        }

        for spec in GUARD_REGISTRY:
            if "routes" in spec.needs and routes is None:
                continue
            kwargs = {n: pool[n] for n in spec.needs if n in pool}
            seconds, raised = _time_call(
                lambda s=spec, k=kwargs: s.fn(graph, "audit", **k), repeats
            )
            record("guard", spec.name, seconds, raised)

        for spec in CHECK_REGISTRY:
            if "routes" in spec.needs and routes is None:
                continue
            kwargs = {n: pool[n] for n in spec.needs}
            seconds, raised = _time_call(lambda f=spec.fn, k=kwargs: f(**k), repeats)
            record("check", spec.name, seconds, raised)

    for name, s in stats.items():
        s["mean_us"] = (s["total_s"] / s["fixtures"]) * 1e6 if s["fixtures"] else 0.0
        s["total_us"] = s["total_s"] * 1e6
        s["tier"] = tier_by_name.get(name)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, help="write the raw table to this path")
    ap.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="time each guard N times per fixture and keep the min (default 3)",
    )
    ap.add_argument(
        "--tier-a-threshold",
        type=float,
        default=50.0,
        help="mean microseconds below which a guard is a Tier-A candidate",
    )
    args = ap.parse_args()

    fixtures = discover_fixtures()
    if not fixtures:
        print("no fixtures found", file=sys.stderr)
        return 1
    stats = audit(fixtures, args.repeats)

    rows = sorted(stats.items(), key=lambda kv: kv[1]["mean_us"], reverse=True)
    print(
        f"# guard cost audit over {len(fixtures)} fixtures (repeats={args.repeats})\n"
    )
    print(
        f"{'guard / check':<52}{'kind':<7}{'mean us':>10}{'total us':>11}"
        f"{'raises':>8}{'tier':>6}"
    )
    print("-" * 94)
    for name, s in rows:
        print(
            f"{name:<52}{s['kind']:<7}{s['mean_us']:>10.1f}{s['total_us']:>11.0f}"
            f"{s['raises']:>8}{(s['tier'] or '-'):>6}"
        )

    cheap = [n for n, s in rows if s["mean_us"] < args.tier_a_threshold]
    print(
        f"\nTier-A candidates (<{args.tier_a_threshold:.0f} us mean): "
        f"{len(cheap)} of {len(stats)}"
    )
    for n in cheap:
        print(f"  {n}")

    if args.json:
        args.json.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
