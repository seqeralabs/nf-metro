#!/usr/bin/env python3
"""Probe a single .mmd through the full nf-metro pipeline and report defects.

Runs the same stages a real render does (parse -> layout -> validate -> route),
then emits a structured JSON verdict that separates *authoring* problems (the
.mmd is malformed, so any downstream defect is the author's fault) from
*engine* problems (the .mmd is well-formed yet the engine produces a bad
layout). The latter are the bugs this skill exists to surface.

The verdict has four finding buckets, in escalating "this is an engine bug"
confidence:

  parse_issues   - graph-semantic findings from the parser's own validate_graph
                   (undefined lines, dangling ports, ...). Usually an AUTHORING
                   mistake -> fix the .mmd, don't file a bug.
  layout_crash   - compute_layout(validate=False) raised. A hard engine failure
                   on well-formed input. The strongest obvious-bug signal.
  guard_failure  - compute_layout(validate=True) raised a PhaseInvariantError
                   that the unguarded run did not. An invariant the engine
                   itself declares but violates. Obvious bug.
  validator      - structural Violations from tests/layout_validator.py
                   (overlap, containment, station-as-elbow, kinks, ...).
                   ERROR-severity ones are obvious bugs; WARNING-severity ones
                   are worth an eyeball.

Usage:
    python probe_layout.py INPUT.mmd [--svg OUT.svg] [--png OUT.png]
                                     [--max-station-columns N] [--json]

Exit code is 0 if no ERROR-level engine findings, 1 otherwise, so the skill can
branch on it. Authoring (parse) issues alone do not set a non-zero code - they
mean "go fix your .mmd", not "engine bug".
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

# The validator lives in tests/, which is not an installed package. Add the
# repo's tests dir to the path so `import layout_validator` resolves the same
# module the suite uses, rather than a stale copy.
_REPO_ROOT = Path(__file__).resolve().parents[4]
for candidate in (_REPO_ROOT, Path.cwd()):
    tests_dir = candidate / "tests"
    if (tests_dir / "layout_validator.py").exists():
        sys.path.insert(0, str(tests_dir))
        break

from nf_metro.layout.engine import compute_layout  # noqa: E402
from nf_metro.parser import validate_graph  # noqa: E402
from nf_metro.parser.mermaid import parse_metro_mermaid  # noqa: E402


def _short_tb(exc: BaseException) -> str:
    """Last frame + exception type/message - enough to dedup and locate."""
    tb = traceback.extract_tb(exc.__traceback__)
    where = ""
    if tb:
        frame = tb[-1]
        where = f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
    return (
        f"{type(exc).__name__}: {exc} ({where})"
        if where
        else f"{type(exc).__name__}: {exc}"
    )


def probe(mmd_text: str, max_station_columns: int = 15) -> dict:
    verdict: dict = {
        "parse_issues": [],
        "layout_crash": None,
        "guard_failure": None,
        "validator": [],
        "counts": {},
    }

    # 1. Parse. A raise here is a parser bug; a populated issue list is usually
    #    an authoring mistake. Either way, downstream stages can't be trusted.
    try:
        graph = parse_metro_mermaid(mmd_text, max_station_columns=max_station_columns)
    except Exception as exc:  # noqa: BLE001
        verdict["parse_issues"].append(
            {"severity": "error", "message": f"Parser raised: {_short_tb(exc)}"}
        )
        return verdict

    for issue in validate_graph(graph):
        verdict["parse_issues"].append(
            {"severity": issue.severity, "message": issue.message}
        )

    verdict["counts"] = {
        "sections": len(graph.sections),
        "stations": len(graph.stations),
        "lines": len(graph.lines),
        "edges": len(graph.edges),
    }

    # 2. Unguarded layout. A raise here is a hard engine failure on input the
    #    parser accepted - the strongest obvious-bug signal.
    try:
        compute_layout(graph, validate=False)
    except Exception as exc:  # noqa: BLE001
        verdict["layout_crash"] = _short_tb(exc)
        return verdict

    # 3. Validator structural checks on the laid-out graph.
    try:
        from layout_validator import Severity, validate_layout

        for v in validate_layout(graph):
            verdict["validator"].append(
                {
                    "check": v.check,
                    "severity": "error" if v.severity == Severity.ERROR else "warning",
                    "message": v.message,
                }
            )
    except Exception as exc:  # noqa: BLE001
        verdict["validator"].append(
            {"check": "validator_crash", "severity": "error", "message": _short_tb(exc)}
        )

    # 4. Guarded layout on a FRESH parse (compute_layout mutates). A raise the
    #    unguarded run didn't hit means the engine tripped its own invariant.
    try:
        guarded = parse_metro_mermaid(mmd_text, max_station_columns=max_station_columns)
        compute_layout(guarded, validate=True)
    except Exception as exc:  # noqa: BLE001
        verdict["guard_failure"] = _short_tb(exc)

    return verdict


def _has_engine_error(verdict: dict) -> bool:
    if verdict["layout_crash"] or verdict["guard_failure"]:
        return True
    return any(v["severity"] == "error" for v in verdict["validator"])


def _render(mmd_path: Path, svg_out: Path | None, png_out: Path | None) -> dict:
    out: dict = {}
    if not (svg_out or png_out):
        return out
    svg_target = svg_out or (png_out.with_suffix(".svg") if png_out else None)
    try:
        from click.testing import CliRunner

        from nf_metro.cli import cli

        result = CliRunner().invoke(
            cli, ["render", str(mmd_path), "-o", str(svg_target)]
        )
        if result.exit_code != 0:
            out["render_error"] = (result.output or str(result.exception)).strip()
            return out
        out["svg"] = str(svg_target)
    except Exception as exc:  # noqa: BLE001
        out["render_error"] = _short_tb(exc)
        return out

    if png_out:
        try:
            import cairosvg

            cairosvg.svg2png(url=str(svg_target), write_to=str(png_out), scale=2)
            out["png"] = str(png_out)
        except Exception as exc:  # noqa: BLE001
            out["png_error"] = _short_tb(exc)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("--svg", type=Path, default=None)
    ap.add_argument("--png", type=Path, default=None)
    ap.add_argument("--max-station-columns", type=int, default=15)
    ap.add_argument("--json", action="store_true", help="emit raw JSON only")
    args = ap.parse_args()

    verdict = probe(
        args.input.read_text(), max_station_columns=args.max_station_columns
    )
    verdict["render"] = _render(args.input, args.svg, args.png)
    verdict["engine_error"] = _has_engine_error(verdict)

    if args.json:
        print(json.dumps(verdict, indent=2))
    else:
        _print_human(args.input, verdict)

    return 1 if verdict["engine_error"] else 0


def _print_human(path: Path, v: dict) -> None:
    c = v.get("counts", {})
    print(f"== probe {path.name} ==")
    if c:
        print(
            f"   {c.get('sections', '?')} sections, {c.get('stations', '?')} stations, "
            f"{c.get('lines', '?')} lines, {c.get('edges', '?')} edges"
        )
    if v["parse_issues"]:
        print(
            "\n-- parse/authoring issues (fix the .mmd, usually NOT an engine bug) --"
        )
        for i in v["parse_issues"]:
            print(f"   [{i['severity']}] {i['message']}")
    if v["layout_crash"]:
        print("\n-- LAYOUT CRASH (engine bug) --")
        print(f"   {v['layout_crash']}")
    if v["guard_failure"]:
        print("\n-- GUARD FAILURE / invariant violation (engine bug) --")
        print(f"   {v['guard_failure']}")
    errs = [x for x in v["validator"] if x["severity"] == "error"]
    warns = [x for x in v["validator"] if x["severity"] == "warning"]
    if errs:
        print("\n-- validator ERRORS (obvious engine bugs) --")
        for x in errs:
            print(f"   [{x['check']}] {x['message']}")
    if warns:
        print("\n-- validator warnings (eyeball these) --")
        for x in warns:
            print(f"   [{x['check']}] {x['message']}")
    r = v.get("render", {})
    if r.get("png"):
        print(f"\n   rendered PNG: {r['png']}")
    elif r.get("svg"):
        print(f"\n   rendered SVG: {r['svg']}")
    if r.get("render_error"):
        print(f"\n   RENDER ERROR: {r['render_error']}")
    print(f"\n   => engine_error={v['engine_error']}")


if __name__ == "__main__":
    raise SystemExit(main())
