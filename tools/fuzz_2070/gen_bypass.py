"""Realistic maps biased to vertical (mostly BT) sections whose chain a line
both enters at its first station and bypasses to a later one.
usage: gen_bypass.py OUTDIR COUNT SEED PREFIX"""
import random, sys
from pathlib import Path
COLOURS = ("#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4")
NAMES = ["FastQC", "TrimGalore", "STAR", "Salmon", "MultiQC", "Picard", "samtools sort",
         "featureCounts", "DESeq2", "Qualimap", "RSeQC", "BWA", "GATK", "bcftools", "Kraken2",
         "MarkDuplicates", "HISAT2", "StringTie", "preseq", "dupRadar", "QC", "Align", "Index", "Call"]
def gen(rng):
    nl = rng.randint(2, 6); lines = [f"m{i}" for i in range(nl)]
    out = [f"%%metro line: m{i} | Line {i} | {COLOURS[i]}" for i in range(nl)] + ["", "graph LR"]
    nz = rng.randint(2, 4); bz = rng.randrange(1, nz)
    zones = []
    for z in range(nz):
        n = rng.randint(3, 5) if z == bz else rng.randint(1, 4)
        if z == bz:
            d = "BT" if rng.random() < 0.7 else "TB"
        else:
            d = rng.choice([None, None, None, "BT", "TB", "RL"]) if z else None
        zones.append(n)
        out.append(f"    subgraph z{z} [Zone {z}]")
        if d: out.append(f"        %%metro direction: {d}")
        for i in range(n):
            out.append(f"        q{z}x{i}[{rng.choice(NAMES)}]")
        for i in range(n - 1):
            ls = sorted(rng.sample(lines, rng.randint(1, min(3, nl))))
            out.append(f"        q{z}x{i} -->|{','.join(ls)}| q{z}x{i + 1}")
        if n >= 3 and rng.random() < 0.5:
            a = rng.randrange(0, n - 2); b = rng.randrange(a + 2, n)
            out.append(f"        q{z}x{a} -->|{rng.choice(lines)}| q{z}x{b}")
        out.append("    end")
    up = rng.randrange(0, bz); un = zones[up] - 1
    line = rng.choice(lines)
    out.append(f"    q{up}x{un} -->|{line}| q{bz}x0")
    tgt = rng.randrange(1, zones[bz])
    src2 = un if rng.random() < 0.5 else rng.randrange(0, zones[up])
    out.append(f"    q{up}x{src2} -->|{line}| q{bz}x{tgt}")
    if rng.random() < 0.6:
        others = [l for l in lines if l != line]
        if others:
            out.append(f"    q{up}x{un} -->|{','.join(sorted(rng.sample(others, rng.randint(1, len(others)))))}| q{bz}x0")
    for _ in range(rng.randint(0, 3)):
        a = rng.randrange(0, nz - 1); b = rng.randrange(a + 1, nz)
        out.append(f"    q{a}x{zones[a] - 1} -->|{','.join(sorted(rng.sample(lines, rng.randint(1, nl))))}| q{b}x{0 if rng.random() < 0.7 else rng.randrange(zones[b])}")
    if bz < nz - 1:
        out.append(f"    q{bz}x{zones[bz] - 1} -->|{line}| q{nz - 1}x0")
    return "\n".join(out) + "\n"
outdir, count, seed, prefix = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
outdir.mkdir(parents=True, exist_ok=True)
rng = random.Random(seed)
for i in range(count):
    (outdir / f"{prefix}_{i:05d}.mmd").write_text(gen(rng))
