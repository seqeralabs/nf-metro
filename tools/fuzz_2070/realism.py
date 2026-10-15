"""Realism check for .mmd maps.

realistic := no repeated (source, target, line) edge and every line's edges
form one weakly connected piece (no line appearing from nowhere mid-chain).
Also tags shapes: multi-feeder (a line enters one station from 2+ sources),
fork-rejoin (a line forks and its branches meet again), skip (an in-zone edge
jumping over a station).

usage: realism.py file.mmd [...]   -> prints name realistic tags
importable: check(text) -> (realistic: bool, tags: set[str])
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

EDGE = re.compile(r"(\S+) -->\|([^|]+)\| (\S+)")


def check(text):
    seen, dup = set(), False
    per = defaultdict(list)
    for a, ls, b in EDGE.findall(text):
        for lid in ls.split(","):
            dup |= (a, b, lid) in seen
            seen.add((a, b, lid))
            per[lid].append((a, b))
    connected = True
    tags = set()
    for lid, es in per.items():
        adj = defaultdict(set)
        for a, b in es:
            adj[a].add(b)
            adj[b].add(a)
        nodes = list(adj)
        stack, comp = [nodes[0]], {nodes[0]}
        while stack:
            for n in adj[stack.pop()]:
                if n not in comp:
                    comp.add(n)
                    stack.append(n)
        connected &= len(comp) == len(nodes)
        indeg = defaultdict(set)
        outdeg = defaultdict(set)
        for a, b in es:
            indeg[b].add(a)
            outdeg[a].add(b)
        if any(len(v) > 1 for v in indeg.values()):
            tags.add("multi-feeder")
            if any(len(v) > 1 for v in outdeg.values()):
                tags.add("fork-rejoin")
        for a, b in es:
            ma, mb = re.match(r"q(\d+)x(\d+)", a), re.match(r"q(\d+)x(\d+)", b)
            if ma and mb and ma.group(1) == mb.group(1) and int(mb.group(2)) > int(ma.group(2)) + 1:
                tags.add("skip")
    return (not dup and connected), tags


if __name__ == "__main__":
    for f in sys.argv[1:]:
        ok, tags = check(Path(f).read_text())
        print(Path(f).name, "realistic" if ok else "NOT", " ".join(sorted(tags)))
