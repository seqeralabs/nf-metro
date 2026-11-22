#!/usr/bin/env python3
"""Render the maps a PR adds with the base code, so each gets a "before" panel.

Usage:
    python scripts/render_added_on_base.py BASE_DIR PR_DIR OUT_DIR --base-src DIR

BASE_DIR and PR_DIR are the render directories the diff compares. A render in
PR_DIR with no counterpart in BASE_DIR is a map the PR registers, which the
base build (run against the base gallery.yaml) never rendered. This script
renders each of those maps from the PR's ``.mmd`` with the ``nf_metro`` package
under ``--base-src`` (the base checkout's ``src/``), using the parameters
recorded in PR_DIR/sources.json.

OUT_DIR receives ``<name>.svg`` for every map the base code renders, an
``aborts.json`` mapping ``<name>.svg`` to a one-line summary of the error for every
map it does not, and ``metrics.json``. ``build_render_diff.py --base-added``
turns these into before/after panels.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent


def _summary(exc: BaseException) -> str:
    """The error's type and its most specific first line.

    A guard abort opens with a generic sentence and names the violated guard on
    the first ``[...]`` line after it, so that line is preferred.
    """
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    guard = next((line for line in lines if line.startswith("[")), None)
    first = guard or (lines[0] if lines else "")
    return f"{type(exc).__name__}: {first[:300]}" if first else type(exc).__name__


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("base_dir", type=Path)
    parser.add_argument("pr_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--base-src",
        type=Path,
        required=True,
        help="The base checkout's src/ directory, holding the nf_metro package.",
    )
    args = parser.parse_args()

    # Importing nf_metro here, before build_gallery, pins every later
    # ``nf_metro`` import to the base package: build_gallery prepends this
    # checkout's src/ to sys.path, but a package already in sys.modules wins.
    base_src = args.base_src.resolve()
    sys.path.insert(0, str(base_src))
    import nf_metro

    if not Path(nf_metro.__file__).resolve().is_relative_to(base_src):
        sys.exit(f"nf_metro imported from {nf_metro.__file__}, not under {base_src}")

    sys.path.insert(0, str(SCRIPTS_DIR))
    import build_gallery

    sources = json.loads((args.pr_dir / "sources.json").read_text())
    base_names = {p.name for p in args.base_dir.glob("*.svg")}
    added = sorted(
        p.name for p in args.pr_dir.glob("*.svg") if p.name not in base_names
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    aborts: dict[str, str] = {}
    for name in added:
        info = sources.get(name)
        if info is None:
            print(f"  {name}: no recorded source, skipped")
            continue
        try:
            build_gallery.render_mmd(
                PROJECT_ROOT / info["src"],
                args.out_dir / name,
                debug=info["debug"],
                self_color_scheme=info["self_color_scheme"],
                from_nextflow=info["from_nextflow"],
            )
            print(f"  {name}: renders on base code")
        except Exception as exc:  # noqa: BLE001 - the abort is the datum
            aborts[name] = _summary(exc)
            print(f"  {name}: aborts on base code - {aborts[name]}")

    (args.out_dir / "aborts.json").write_text(
        json.dumps(aborts, indent=2, sort_keys=True) + "\n"
    )
    (args.out_dir / "metrics.json").write_text(
        json.dumps(build_gallery._metrics, indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
