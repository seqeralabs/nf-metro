"""Reduce an .mmd that renders on main and aborts on PR, keeping that property.

usage: python reduce.py <in.mmd> <out.mmd> [etype]
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

G = Path(__file__).resolve().parent
S = G.parent
ENVPY = sys.executable


def spawn(tree):
    env = dict(os.environ, PYTHONPATH=str(tree / "src"))
    return subprocess.Popen([ENVPY, str(G / "rworker.py"), str(tree)], cwd=str(tree), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


MAIN, PR = spawn(S / "mainref"), spawn(S / "gate_pr")


def ask(w, text):
    w.stdin.write(json.dumps({"text": text}) + "\n")
    w.stdin.flush()
    return json.loads(w.stdout.readline())


def pred(lines, etype):
    text = "\n".join(lines) + "\n"
    b = ask(PR, text)
    if b["status"] != "error" or (etype and b["etype"] != etype):
        return False
    return ask(MAIN, text)["status"] == "ok"


def blocks(lines):
    """Index ranges of subgraph blocks."""
    out, start = [], None
    for i, l in enumerate(lines):
        if l.strip().startswith("subgraph"):
            start = i
        elif l.strip() == "end" and start is not None:
            out.append((start, i + 1))
            start = None
    return out


def main():
    src, dst = sys.argv[1], sys.argv[2]
    lines = Path(src).read_text().splitlines()
    etype = sys.argv[3] if len(sys.argv) > 3 else None
    if not pred(lines, etype):
        print("PREDICATE FALSE ON INPUT")
        sys.exit(2)
    changed = True
    while changed:
        changed = False
        for a, b in reversed(blocks(lines)):
            cand = lines[:a] + lines[b:]
            if pred(cand, etype):
                lines, changed = cand, True
        i = len(lines) - 1
        while i >= 0:
            l = lines[i]
            if l.strip() in ("graph LR", "end") or l.strip().startswith("subgraph"):
                i -= 1
                continue
            cand = lines[:i] + lines[i + 1:]
            if pred(cand, etype):
                lines, changed = cand, True
            else:
                m = re.match(r"(\s*\S+ -->\|)([^|]+)(\|.*)", l)
                if m and "," in m.group(2):
                    ids = [x.strip() for x in m.group(2).split(",")]
                    for k in range(len(ids)):
                        rest = ids[:k] + ids[k + 1:]
                        c2 = lines[:i] + [m.group(1) + ",".join(rest) + m.group(3)] + lines[i + 1:]
                        if pred(c2, etype):
                            lines, changed = c2, True
                            break
                m2 = re.match(r"(\s*%%metro (?:entry|exit): \w+ \| )(.*)", l)
                if m2 and "," in m2.group(2):
                    ids = [x.strip() for x in m2.group(2).split(",")]
                    for k in range(len(ids)):
                        rest = ids[:k] + ids[k + 1:]
                        c2 = lines[:i] + [m2.group(1) + ", ".join(rest)] + lines[i + 1:]
                        if pred(c2, etype):
                            lines, changed = c2, True
                            break
            i -= 1
    Path(dst).write_text("\n".join(lines) + "\n")
    print(len(lines), "lines; verified:", pred(lines, etype))


main()
