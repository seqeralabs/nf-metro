"""Comparison + rubric runner for the constraint-solver spike.

Issue: https://github.com/pinin4fjords/nf-metro/issues/351

For each fixture in `examples/`:

1. Layout twice: once via the engine, once via the spike's solver.
2. Compute per-graph metrics via tests/layout_validator.py.
3. Classify the spike vs engine into one of five buckets:

   Better             - spike has strictly fewer ERROR violations
                        across the board (kink/overlap/etc), no new
                        violation category introduced.
   Equivalent         - same ERROR count category-by-category. Some WARNING
                        deltas allowed.
   Acceptable worse   - 1 new ERROR introduced, max one new trunk-Y kink,
                        no station overlaps, no line-crosses-non-consumer.
   Unacceptable worse - 2 new ERROR violations (the project quality bar
                        threshold for "worse than two trunk-Y kinks").
   Fatal              - >2 new ERROR violations, or any station overlap,
                        or any new line-crosses-non-consumer.

Pass criteria for the gallery (per #351):
- Better + Equivalent + Acceptable worse >= 12 of 15 fixtures
- Unacceptable worse + Fatal == 0

Run:
    PYTHONPATH=$PWD/src:$PWD python scratch/constraint_spike_v3_compare.py
    PYTHONPATH=$PWD/src:$PWD python scratch/constraint_spike_v3_compare.py \
        --fixtures rnaseq_sections variantbenchmarking
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from collections import Counter
from copy import deepcopy
from pathlib import Path

from nf_metro.layout import compute_layout
from nf_metro.parser import parse_metro_mermaid
from tests.layout_validator import Severity, validate_layout

from scratch.constraint_spike_v3_model import solve as solve_spike

GALLERY = [
    "differentialabundance",
    "differentialabundance_default",
    "epitopeprediction",
    "genomeassembly",
    "genomeassembly_staggered",
    "hlatyping",
    "rnaseq_auto",
    "rnaseq_sections",
    "rnaseq_sections_manual",
    "simple_pipeline",
    "variant_calling",
    "variant_calling_tuned",
    "variantbenchmarking",
    "variantbenchmarking_auto",
    "variantprioritization",
]

# Five-fixture subset for the mid-spike go/no-go.
SUBSET = [
    "simple_pipeline",
    "rnaseq_sections",
    "variantbenchmarking",
    "genomeassembly",
    "epitopeprediction",
]


def _layout_engine(text: str) -> dict:
    g = parse_metro_mermaid(text)
    t0 = time.perf_counter()
    compute_layout(g, validate=False)
    dt = time.perf_counter() - t0
    violations = validate_layout(g)
    return {
        "graph": g,
        "time_s": dt,
        "violations": violations,
        "errors": [v for v in violations if v.severity == Severity.ERROR],
        "warnings": [v for v in violations if v.severity == Severity.WARNING],
    }


def _layout_spike(text: str) -> dict:
    g = parse_metro_mermaid(text)
    t0 = time.perf_counter()
    try:
        diag = solve_spike(g)
    except Exception as exc:
        return {
            "graph": None,
            "time_s": time.perf_counter() - t0,
            "violations": [],
            "errors": [],
            "warnings": [],
            "error_message": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    dt = time.perf_counter() - t0
    try:
        violations = validate_layout(g)
    except Exception as exc:
        return {
            "graph": g,
            "time_s": dt,
            "violations": [],
            "errors": [],
            "warnings": [],
            "error_message": f"validator: {type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    return {
        "graph": g,
        "time_s": dt,
        "violations": violations,
        "errors": [v for v in violations if v.severity == Severity.ERROR],
        "warnings": [v for v in violations if v.severity == Severity.WARNING],
        "diag": diag,
    }


def _categorise(eng: dict, spk: dict) -> tuple[str, dict]:
    """Bucket the spike's result against the engine's.

    Returns (bucket, notes) where bucket is one of:
      "better", "equivalent", "acceptable_worse", "unacceptable_worse",
      "fatal", "crashed"
    """
    if spk.get("error_message"):
        return "crashed", {"reason": spk["error_message"]}

    eng_err_counts = Counter(v.check for v in eng["errors"])
    spk_err_counts = Counter(v.check for v in spk["errors"])
    eng_total = sum(eng_err_counts.values())
    spk_total = sum(spk_err_counts.values())

    # New ERROR types and new ERROR count per type
    new_errors = []
    for check, count in spk_err_counts.items():
        delta = count - eng_err_counts.get(check, 0)
        if delta > 0:
            new_errors.append((check, delta))
    new_error_total = sum(d for _, d in new_errors)

    notes = {
        "eng_total_errors": eng_total,
        "spk_total_errors": spk_total,
        "eng_err_counts": dict(eng_err_counts),
        "spk_err_counts": dict(spk_err_counts),
        "new_errors": new_errors,
        "new_error_total": new_error_total,
    }

    # Fatal checks first.
    fatal_checks = {"check_station_containment", "check_section_overlap"}
    for check, delta in new_errors:
        if check in fatal_checks and delta > 0:
            notes["reason"] = f"new {check} violation ({delta})"
            return "fatal", notes

    # No new errors at all and total dropped: better.
    if new_error_total == 0 and spk_total < eng_total:
        return "better", notes
    # No new errors, total equal: equivalent.
    if new_error_total == 0 and spk_total == eng_total:
        return "equivalent", notes
    # No new errors but total > eng_total: can't happen (mathematically the
    # totals balance), but in case it does, treat as equivalent.
    if new_error_total == 0:
        return "equivalent", notes
    # 1 new ERROR: acceptable
    if new_error_total <= 1:
        return "acceptable_worse", notes
    # 2 new ERRORs: unacceptable
    if new_error_total <= 2:
        return "unacceptable_worse", notes
    # >2 new ERRORs: fatal
    return "fatal", notes


def run_one(fixture: str, repo_root: Path) -> dict:
    """Compute, compare, classify one fixture."""
    path = repo_root / "examples" / f"{fixture}.mmd"
    if not path.exists():
        return {"fixture": fixture, "skipped": True, "reason": "not found"}
    text = path.read_text()
    eng = _layout_engine(text)
    spk = _layout_spike(text)
    bucket, notes = _categorise(eng, spk)
    return {
        "fixture": fixture,
        "engine_time_s": eng["time_s"],
        "spike_time_s": spk["time_s"],
        "engine_errors": len(eng["errors"]),
        "spike_errors": len(spk["errors"]),
        "engine_warnings": len(eng["warnings"]),
        "spike_warnings": len(spk["warnings"]),
        "bucket": bucket,
        "notes": notes,
        "spike_error_message": spk.get("error_message"),
        "spike_traceback": spk.get("traceback"),
        "spike_diag": spk.get("diag"),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--fixtures", nargs="*", default=None,
        help="Fixtures to run (default: 5-fixture subset; pass 'all' for full gallery)"
    )
    p.add_argument("--repo-root", default=".", help="Repo root (default: cwd)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if args.fixtures is None:
        fixtures = SUBSET
    elif args.fixtures == ["all"]:
        fixtures = GALLERY
    else:
        fixtures = args.fixtures

    results = []
    for fx in fixtures:
        print(f"\n=== {fx} ===", flush=True)
        try:
            r = run_one(fx, repo_root)
        except Exception as exc:
            print(f"  fixture crashed during evaluation: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            results.append({"fixture": fx, "bucket": "crashed", "notes": {"reason": str(exc)}})
            continue
        if r.get("skipped"):
            print(f"  SKIPPED: {r['reason']}")
            continue
        results.append(r)
        n = r["notes"]
        print(f"  bucket: {r['bucket']:20s}")
        print(f"  engine: {r['engine_errors']} errors, {r['engine_warnings']} warnings ({r['engine_time_s']*1000:.0f} ms)")
        print(f"  spike:  {r['spike_errors']} errors, {r['spike_warnings']} warnings ({r['spike_time_s']*1000:.0f} ms)")
        if n.get("new_errors"):
            print(f"  new errors: {n['new_errors']}")
        if r["spike_error_message"]:
            print(f"  spike error: {r['spike_error_message']}")
            if args.verbose and r["spike_traceback"]:
                print(r["spike_traceback"])

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    by_bucket: dict[str, list[str]] = {}
    for r in results:
        by_bucket.setdefault(r["bucket"], []).append(r["fixture"])
    for bucket in ["better", "equivalent", "acceptable_worse", "unacceptable_worse", "fatal", "crashed"]:
        names = by_bucket.get(bucket, [])
        marker = "OK " if bucket in ("better", "equivalent", "acceptable_worse") else "BAD"
        print(f"  {marker} {bucket:20s}: {len(names):2d}  {names}")
    n_total = len(results)
    n_good = sum(len(by_bucket.get(b, [])) for b in ("better", "equivalent", "acceptable_worse"))
    n_bad = sum(len(by_bucket.get(b, [])) for b in ("unacceptable_worse", "fatal", "crashed"))
    pct = 100 * n_good / n_total if n_total else 0
    print(f"\n  good: {n_good}/{n_total} ({pct:.0f}%)   bad: {n_bad}/{n_total}")
    pass_bar = "PASS" if (n_good * 5 >= n_total * 4 and n_bad == 0) else "FAIL"
    print(f"  >=80% good AND 0 bad: {pass_bar}")

    return 0 if (n_good * 5 >= n_total * 4 and n_bad == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
