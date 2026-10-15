"""2-panel PNGs (main | PR) for out/worst25.json into worst/."""
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

G = Path(__file__).resolve().parent
S = G.parent
worst = json.load(open(sys.argv[1] if len(sys.argv) > 1 else G / "out" / "worst25.json"))
dst = G / "worst"
tmp = G / "out" / "pngtmp"
tmp.mkdir(exist_ok=True)
for rank, c in enumerate(worst, 1):
    p = Path(c["path"])
    imgs = []
    for tag, tree in (("main", S / "mainref"), ("pr", S / "gate_pr")):
        out = tmp / f"{rank:02d}_{tag}.png"
        env = dict(os.environ, PYTHONPATH=str(tree / "src"))
        r = subprocess.run([sys.executable, "-m", "nf_metro", "render", str(p), "-o", str(out), "--mode", "light"],
                           cwd=str(tree), env=env, capture_output=True, text=True)
        if r.returncode != 0:
            print("render failed", tag, p, r.stderr[-300:])
        imgs.append(Image.open(out).convert("RGB"))
    h = max(i.height for i in imgs) + 40
    w = sum(i.width for i in imgs) + 20
    canvas = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(canvas)
    x = 0
    for (tag, im) in zip(("main 0b1562440", "PR 05a55ee5e"), imgs):
        canvas.paste(im, (x, 40))
        d.text((x + 10, 10), f"{tag}  {p.name}", fill="black")
        x += im.width + 20
    d.line([(imgs[0].width + 10, 0), (imgs[0].width + 10, h)], fill="red", width=2)
    name = f"{rank:02d}_{c['corpus']}_{p.stem}.png"
    canvas.save(dst / name)
    print(name, c["score"], c["delta"])
