"""Fresh fuzz generator for the PR #2047 fuzz gate (written from scratch).

Model: a global flow order of sections; each line is a DAG = a main path
through a subsequence of sections (subsequence of stations inside each,
so in-section bypasses arise) plus optional branches that fork off the main
path and either dead-end or rejoin later (cross-section fork/rejoin = merge
into one entry port from two feeders; in-section fork/rejoin = diamond).
All edges go forward in (flow_idx, station_idx), so the graph is a DAG.
Grid placement (when explicit) is a possibly-shuffled layout of the flow
order, giving backward and cross-row inter-section edges.
"""
import random
import sys
from pathlib import Path

WORDS = ["trim", "align", "sort", "index", "dedup", "call", "merge", "filter",
         "qc", "count", "quant", "annotate", "report", "stats", "fastqc",
         "markdup", "bam", "vcf", "gather", "normalize", "pca", "cluster",
         "assemble", "polish", "bin", "classify", "summarise", "export"]
COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0",
          "#f032e6", "#bcbd22", "#008080", "#9a6324"]
DIRS = ["LR", "RL", "TB", "BT"]
SIDES = ["left", "right", "top", "bottom"]


def label(r):
    n = r.choice([1, 1, 1, 2, 2, 3])
    s = "_".join(r.choice(WORDS) for _ in range(n))
    return s.upper() if r.random() < 0.1 else s


def gen(seed, rough=False):
    r = random.Random(seed)
    k = r.choices([2, 3, 4, 5, 6], weights=[2, 3, 3, 2, 1])[0]
    nlines = r.choices([2, 3, 4, 5, 6], weights=[3, 3, 3, 2, 1])[0]
    want_vertical = r.random() < 0.62
    dirs = []
    for i in range(k):
        dirs.append(r.choices(DIRS, weights=[5, 1, 3, 2])[0] if r.random() < 0.55 else "LR")
    if want_vertical and not any(d in ("TB", "BT") for d in dirs):
        dirs[r.randrange(k)] = r.choice(["TB", "BT"])
    if not want_vertical:
        dirs = [d if d in ("LR", "RL") else "LR" for d in dirs]
    nst = [r.randint(2, 5) for _ in range(k)]
    # station key = (sec, idx); order key = (sec, idx)
    edges = []  # (src, tgt, line)
    eset = set()

    def add(a, b, lid):
        if a == b:
            return
        if (a, b, lid) in eset and not rough:
            return
        eset.add((a, b, lid))
        edges.append((a, b, lid))

    lines = [f"l{i}" for i in range(nlines)]
    for li, lid in enumerate(lines):
        # main path: subsequence of sections, length >= 1
        span = r.choice(["full", "full", "prefix", "suffix", "middle"])
        lo, hi = 0, k - 1
        if span == "prefix":
            hi = r.randint(min(1, k - 1), k - 1)
        elif span == "suffix":
            lo = r.randint(0, max(0, k - 2))
        elif span == "middle" and k >= 3:
            lo = r.randint(0, k - 2)
            hi = r.randint(lo + 1, k - 1)
        secs = [s for s in range(lo, hi + 1) if s in (lo, hi) or r.random() < 0.7]
        path = []
        for si, s in enumerate(secs):
            n = nst[s]
            a = 0
            b = n - 1
            # lines that start or end mid-chain
            if si == 0 and r.random() < 0.3:
                a = r.randint(0, n - 1)
            if si == len(secs) - 1 and r.random() < 0.3:
                b = r.randint(a, n - 1)
            if si > 0 and r.random() < 0.2:
                a = r.randint(0, n - 1)
            if si < len(secs) - 1 and r.random() < 0.2:
                b = r.randint(a, n - 1)
            idxs = [j for j in range(a, b + 1) if j in (a, b) or r.random() < 0.75]
            path += [(s, j) for j in idxs]
        if len(path) < 2:
            s = secs[0]
            if nst[s] >= 2:
                path = [(s, 0), (s, 1)]
            else:
                path = [(s, 0), ((s + 1) % k if s + 1 < k else s, 0)]
        path = sorted(set(path))
        for a, b in zip(path, path[1:]):
            add(a, b, lid)
        # branches
        nb = r.choices([0, 1, 2], weights=[5, 4, 1])[0]
        for _ in range(nb):
            if len(path) < 2:
                break
            ui = r.randrange(len(path) - 1)
            u = path[ui]
            kind = r.random()
            if kind < 0.35:
                # in-section diamond: u -> w -> v, w a station not on path in u's section
                s = u[0]
                cands = [(s, j) for j in range(u[1] + 1, nst[s]) if (s, j) not in path]
                later = [p for p in path[ui + 1:] if p > u]
                if cands and later:
                    w = r.choice(cands)
                    vs = [p for p in later if p > w]
                    add(u, w, lid)
                    if vs and r.random() < 0.8:
                        add(w, r.choice(vs[:3]), lid)
            else:
                # cross-section branch through sections off the main path
                off = [s for s in range(u[0] + 1, k) if s not in secs]
                if not off:
                    off = [s for s in range(u[0] + 1, k)]
                if not off:
                    continue
                s = r.choice(off)
                j0 = r.randrange(nst[s])
                w = (s, j0)
                add(u, w, lid)
                cur = w
                if r.random() < 0.5 and j0 + 1 < nst[s]:
                    nxt = (s, r.randint(j0 + 1, nst[s] - 1))
                    add(cur, nxt, lid)
                    cur = nxt
                rejoin = [p for p in path if p[0] > cur[0]]
                if rejoin and r.random() < 0.6:
                    add(cur, r.choice(rejoin[:4]), lid)  # merge from two feeders
        if rough:
            if r.random() < 0.3 and edges:
                e = r.choice(edges)
                edges.append(e)  # duplicate
            if r.random() < 0.3:
                # disconnected fragment of the same line
                s = r.randrange(k)
                if nst[s] >= 2:
                    edges.append(((s, 0), (s, nst[s] - 1), lid))
            if r.random() < 0.2:
                # long skip across sections
                a = (0, r.randrange(nst[0]))
                b = (k - 1, r.randrange(nst[k - 1]))
                if a != b:
                    edges.append((a, b, lid))

    used = sorted({p for e in edges for p in e[:2]})
    used_secs = sorted({p[0] for p in used})
    names = {}
    for s, j in used:
        names[(s, j)] = f"s{s}n{j}"

    # grid
    grid = None
    if r.random() < 0.45 and len(used_secs) >= 2:
        ncols = r.choice([1, 2, 2, 3, 3, 4])
        order = list(used_secs)
        mode = r.random()
        if mode < 0.2:
            # shuffle -> backward edges in grid order
            r.shuffle(order)
        elif mode < 0.55 and len(order) >= 3:
            i, j = r.sample(range(len(order)), 2)
            order[i], order[j] = order[j], order[i]
        serp = r.random() < 0.3
        grid = {}
        for pos, s in enumerate(order):
            row, col = divmod(pos, ncols)
            if serp and row % 2 == 1:
                col = ncols - 1 - col
            grid[s] = (col, row)

    if grid and r.random() < 0.85:
        # a horizontal section feeding a same-row neighbour flows toward it
        for s in used_secs:
            if dirs[s] not in ("LR", "RL"):
                continue
            tgt_cols = {grid[b[0]][0] for a, b, _ in edges
                        if a[0] == s and b[0] != s and grid[b[0]][1] == grid[s][1]}
            if tgt_cols and all(c < grid[s][0] for c in tgt_cols):
                dirs[s] = "RL"
            elif tgt_cols and all(c > grid[s][0] for c in tgt_cols):
                dirs[s] = "LR"

    if not grid:
        dirs = [("LR" if (d == "RL" and r.random() < 0.9) else d) for d in dirs]

    # inter-section in/out lines per section
    ins = {s: [] for s in used_secs}
    outs = {s: [] for s in used_secs}
    for a, b, lid in edges:
        if a[0] != b[0]:
            if lid not in ins[b[0]]:
                ins[b[0]].append(lid)
            if lid not in outs[a[0]]:
                outs[a[0]].append(lid)

    L = []
    L.append(f"%%metro title: Fuzz {seed}")
    if r.random() < 0.3:
        L.append("%%metro style: " + r.choice(["nfcore", "seqera"]))
    used_lines = [l for l in lines if any(e[2] == l for e in edges)]
    for i, lid in enumerate(used_lines):
        L.append(f"%%metro line: {lid} | {label(r).title()} | {COLORS[i % len(COLORS)]}")
    if grid:
        for s in used_secs:
            c, rw = grid[s]
            L.append(f"%%metro grid: sec{s} | {c},{rw}")
    L.append("")
    L.append("graph LR")
    for s in used_secs:
        L.append(f"    subgraph sec{s} [{label(r).title()} {s}]")
        if dirs[s] != "LR":
            L.append(f"        %%metro direction: {dirs[s]}")
        if ins[s] and r.random() < 0.22:
            side = r.choice(SIDES)
            if len(ins[s]) >= 2 and r.random() < 0.25:
                cut = r.randint(1, len(ins[s]) - 1)
                side2 = r.choice([x for x in SIDES if x != side])
                L.append(f"        %%metro entry: {side} | {', '.join(ins[s][:cut])}")
                L.append(f"        %%metro entry: {side2} | {', '.join(ins[s][cut:])}")
            else:
                L.append(f"        %%metro entry: {side} | {', '.join(ins[s])}")
        if outs[s] and r.random() < 0.22:
            side = r.choices(SIDES, weights=[1, 8, 4, 4])[0]
            L.append(f"        %%metro exit: {side} | {', '.join(outs[s])}")
        for p in used:
            if p[0] == s:
                L.append(f"        {names[p]}[{label(r)}]")
        # group intra edges by (a,b)
        intra = {}
        for a, b, lid in edges:
            if a[0] == s and b[0] == s:
                intra.setdefault((a, b), []).append(lid)
        for (a, b), lids in intra.items():
            if rough:
                for lid in lids:
                    L.append(f"        {names[a]} -->|{lid}| {names[b]}")
            else:
                L.append(f"        {names[a]} -->|{','.join(lids)}| {names[b]}")
        L.append("    end")
    inter = {}
    for a, b, lid in edges:
        if a[0] != b[0]:
            inter.setdefault((a, b), []).append(lid)
    for (a, b), lids in inter.items():
        if rough:
            for lid in lids:
                L.append(f"    {names[a]} -->|{lid}| {names[b]}")
        else:
            L.append(f"    {names[a]} -->|{','.join(lids)}| {names[b]}")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    out = Path(sys.argv[1])
    n = int(sys.argv[2])
    base = int(sys.argv[3])
    rough = sys.argv[4] == "rough"
    out.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        seed = base + i
        (out / f"f{seed}.mmd").write_text(gen(seed, rough))
