# Fuzz harness used for the #2042 / #2070 work

Evidence branch, not for merge. These scripts generated and compared the fuzz corpora used to measure
"maps that render on main and abort on the change" on the default render path.

- `gen_fresh.py`, `gen_wf2.py`, `gen_wf.py`, `gen_bypass.py`: map generators (realistic = every line connected,
  no duplicate source-target-line edge; `realism.py` checks that).
- `runner.py`, `rworker.py`: render each map in-process (`render_string`) per tree; `compare.py` joins results on
  path and reports new aborts (ok on main, error on the change), fixed aborts and changed renders;
  `reduce.py` shrinks a failing map; `panels.py` builds main | change side-by-side PNGs; `drive.sh` runs the lot.
- Traps: never run python with `-I` (it ignores PYTHONPATH and measures the installed package); render with
  `PYTHONPATH=<tree>/src` and cwd=<tree> and assert `nf_metro.__file__`; an ERROR on both sides is not "identical";
  join on path.
- Paths inside the scripts point at the original scratch layout; adjust before use. Foundation PR 1 of #2070
  is expected to move a cleaned-up version of this into `scripts/`.
