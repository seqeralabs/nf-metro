#!/usr/bin/env python3
"""Print the next runnable step of a family issue.

A family issue lists its steps as ``- [ ] S<n> <text> (base: <branch>[, after: S<m>])``.
A step is runnable when it is unticked, every step it names in ``after`` is ticked,
its base branch exists on ``origin``, and no open PR names ``#<family> S<n>``.

Usage: scripts/next_family_step.py <issue-number> [--repo owner/name] [--all]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

STEP = re.compile(
    r"^- \[(?P<done>[ xX])\] (?P<id>S\d+) (?P<text>.*?) \(base: (?P<base>[^,)]+)"
    r"(?:, after: (?P<after>[^)]+))?\)\s*$"
)


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("issue", type=int)
    ap.add_argument("--repo", default="seqeralabs/nf-metro")
    ap.add_argument("--all", action="store_true", help="list every step with its status")
    a = ap.parse_args()

    body = json.loads(sh("gh", "issue", "view", str(a.issue), "--repo", a.repo, "--json", "body"))["body"]
    steps = [m.groupdict() for m in map(STEP.match, body.splitlines()) if m]
    if not steps:
        print(f"#{a.issue}: no step lines found (expected `- [ ] S1 ... (base: main)`)", file=sys.stderr)
        return 2

    done = {s["id"] for s in steps if s["done"].strip()}
    heads = set(
        line.split("refs/heads/", 1)[1] for line in sh("git", "ls-remote", "--heads", "origin").splitlines() if "refs/heads/" in line
    )
    open_prs = json.loads(sh("gh", "pr", "list", "--repo", a.repo, "--state", "open", "--limit", "200", "--json", "number,title,body,headRefName"))

    def claimed_by(step_id: str) -> int | None:
        token = re.compile(rf"#{a.issue}\b[^\n]{{0,20}}\b{step_id}\b|\b{step_id}\b[^\n]{{0,20}}#{a.issue}\b")
        for pr in open_prs:
            if token.search(pr["title"] or "") or token.search(pr["body"] or ""):
                return pr["number"]
        return None

    first = None
    for s in steps:
        after = [x.strip() for x in (s["after"] or "").split(",") if x.strip()]
        if s["done"].strip():
            status = "done"
        elif any(x not in done for x in after):
            status = f"waits for {', '.join(x for x in after if x not in done)}"
        elif s["base"].strip() not in heads:
            status = f"base `{s['base']}` not on origin"
        elif (pr := claimed_by(s["id"])) is not None:
            status = f"in progress: PR #{pr}"
        else:
            status = "runnable"
            first = first or s
        if a.all:
            print(f"{s['id']:4} {status:28} {s['text'][:90]}")
    if first is None:
        if not a.all:
            print(f"#{a.issue}: no runnable step")
        return 1
    if not a.all:
        print(f"{first['id']}  {first['text']}")
        print(f"base: {first['base']}")
        print(f"run:  /fix-issue {a.issue} {first['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
