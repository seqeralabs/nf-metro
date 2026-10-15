"""Realistic mixed-orientation maps: lines may merge, fork-and-rejoin, and skip.

Every edge runs forward in (zone, station) order.  Each line is a base path
that may skip stations (a bypass edge), plus optional extra sources whose
branches merge into the path and optional fork-and-rejoin detours through
stations off the path.  Each line's edges stay weakly connected, no
(source, target, line) edge repeats, and only visited stations are declared.

usage: gen_wf2.py OUTDIR COUNT SEED [PREFIX] [--vertical-bias F] [--merge-rate F]
"""

import random
import sys
from collections import defaultdict
from pathlib import Path

COLOURS = ("#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4")


def base_path(rng, zones):
    n_zones = len(zones)
    start = rng.randrange(0, n_zones - 1) if rng.random() < 0.4 else 0
    visit = [start] + [z for z in range(start + 1, n_zones) if rng.random() < 0.6]
    if len(visit) == 1:
        visit.append(rng.randrange(start + 1, n_zones))
    path = []
    for k, z in enumerate(visit):
        n_st = zones[z][0]
        first = 0 if rng.random() < 0.7 else rng.randrange(0, n_st)
        last = (
            n_st - 1
            if (k < len(visit) - 1 and rng.random() < 0.6)
            else rng.randrange(first, n_st)
        )
        run = [i for i in range(first, last + 1)]
        if len(run) > 2 and rng.random() < 0.3:
            run.remove(rng.choice(run[1:-1]))
        path.extend((z, i) for i in run)
    return path


def all_stations(zones):
    return [(z, i) for z, (n, _) in enumerate(zones) for i in range(n)]


def branch(rng, zones, lo, hi, avoid, max_mid=2):
    """Stations strictly between lo and hi in order, off the line, ascending."""
    pool = [s for s in all_stations(zones) if lo < s < hi and s not in avoid]
    k = min(len(pool), rng.randint(1, max_mid))
    return sorted(rng.sample(pool, k)) if k else []


def gen(rng, vbias, merge_rate):
    n_lines = rng.randint(3, 5)
    n_zones = rng.randint(3, 5)
    zones = []
    for z in range(n_zones):
        r = rng.random()
        if z == 0 and rng.random() < 0.7:
            direction = None
        elif r < vbias:
            direction = rng.choice(("TB", "BT"))
        elif r < vbias + 0.12:
            direction = "RL"
        else:
            direction = None
        zones.append((rng.randint(2, 4), direction))

    edges = defaultdict(set)
    for li in range(n_lines):
        lid = f"m{li}"
        path = base_path(rng, zones)
        on_line = set(path)
        line_edges = set(zip(path, path[1:]))
        if rng.random() < merge_rate and len(path) >= 2:
            target = rng.choice(path[1:])
            pool = [s for s in all_stations(zones) if s < target and s not in on_line]
            if pool:
                src = rng.choice(pool)
                mids = branch(rng, zones, src, target, on_line | {src}, 1)
                chain = [src, *mids, target]
                line_edges |= set(zip(chain, chain[1:]))
                on_line |= set(chain)
        if rng.random() < merge_rate * 0.6 and len(path) >= 2:
            a, b = sorted(rng.sample(path, 2))
            mids = branch(rng, zones, a, b, on_line)
            if mids:
                chain = [a, *mids, b]
                line_edges |= set(zip(chain, chain[1:]))
                on_line |= set(mids)
        for e in line_edges:
            edges[e].add(lid)

    used = {s for e in edges for s in e}
    out = [f"%%metro line: m{i} | L{i} | {COLOURS[i]}" for i in range(n_lines)]
    out += ["", "graph LR"]
    for z, (n_st, direction) in enumerate(zones):
        stations = [i for i in range(n_st) if (z, i) in used]
        if not stations:
            continue
        out.append(f"    subgraph z{z} [Zone {z}]")
        if direction:
            out.append(f"        %%metro direction: {direction}")
        out += [f"        q{z}x{i}[Q{z}X{i}]" for i in stations]
        for (a, b), lines in sorted(edges.items()):
            if a[0] == b[0] == z:
                out.append(
                    f"        q{a[0]}x{a[1]} -->|{','.join(sorted(lines))}| q{b[0]}x{b[1]}"
                )
        out.append("    end")
    for (a, b), lines in sorted(edges.items()):
        if a[0] != b[0]:
            out.append(f"    q{a[0]}x{a[1]} -->|{','.join(sorted(lines))}| q{b[0]}x{b[1]}")
    return "\n".join(out) + "\n"


def main():
    args = sys.argv[1:]
    outdir, count, seed = Path(args[0]), int(args[1]), int(args[2])
    prefix = args[3] if len(args) > 3 and not args[3].startswith("--") else "w2"
    vbias = float(args[args.index("--vertical-bias") + 1]) if "--vertical-bias" in args else 0.5
    merge_rate = float(args[args.index("--merge-rate") + 1]) if "--merge-rate" in args else 0.35
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    for i in range(count):
        (outdir / f"{prefix}_{i:05d}.mmd").write_text(gen(rng, vbias, merge_rate))


if __name__ == "__main__":
    main()
