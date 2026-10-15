"""Render a list of .mmd files with the nf_metro on PYTHONPATH; emit JSONL.

usage: PYTHONPATH=<tree>/src python runner.py <tree> <listfile> <out.jsonl> <svgdir> <nproc>
"""
import gzip
import hashlib
import json
import os
import signal
import sys
import traceback
from multiprocessing import Pool
from pathlib import Path

TREE = Path(sys.argv[1]).resolve()
SVGDIR = Path(sys.argv[4])


def _alarm(signum, frame):
    raise TimeoutError("render exceeded 90s")


def _init():
    import warnings
    warnings.simplefilter('ignore')
    import nf_metro
    assert str(Path(nf_metro.__file__).resolve()).startswith(str(TREE)), nf_metro.__file__
    signal.signal(signal.SIGALRM, _alarm)


def _segs(pts):
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)
            if (pts[i][0] - pts[i + 1][0]) ** 2 + (pts[i][1] - pts[i + 1][1]) ** 2 > 1e-6]


def _inter(s1, s2):
    (x1, y1), (x2, y2) = s1
    (x3, y3), (x4, y4) = s2
    d = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(d) < 1e-9:
        return None
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / d
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / d
    if 0 < t < 1 and 0 < u < 1:
        px, py = x1 + t * (x2 - x1), y1 + t * (y2 - y1)
        for (ex, ey) in (s1[0], s1[1], s2[0], s2[1]):
            if abs(px - ex) < 0.75 and abs(py - ey) < 0.75:
                return None
        return (px, py)
    return None


def metrics(plan, graph, theme_obj, svg):
    from nf_metro.layout.constants import graph_offset_step
    from nf_metro.render.validate import validate_render
    out = {}
    finds = validate_render(svg, plan=plan)
    kinds = {}
    det = []
    for f in finds:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
        det.append([f.kind, f.line_id, f.station_id])
    out["findings"] = kinds
    out["finding_list"] = sorted(det)
    # crossings between different lines
    by_line = {}
    for lid, pts in plan.offset_polylines():
        by_line.setdefault(lid, []).extend(_segs(list(pts)))
    lids = sorted(by_line)
    pts = set()
    for i, a in enumerate(lids):
        for b in lids[i + 1:]:
            for s1 in by_line[a]:
                for s2 in by_line[b]:
                    p = _inter(s1, s2)
                    if p is not None:
                        pts.add((a, b, round(p[0]), round(p[1])))
    out["crossings"] = len(pts)
    # dead lanes: a visible station whose marker spans more lanes than lines
    step = graph_offset_step(graph, theme_obj.line_width)
    lanes = {}
    for key in plan.station_offsets:
        sid, lid = key
        lanes.setdefault(sid, set()).add(round(plan.station_offsets[key], 2))
    dead = []
    for sid, ls in lanes.items():
        st = plan.graph.stations.get(sid)
        if st is None or st.is_port or len(ls) < 2:
            continue
        if getattr(st, "is_hidden", False):
            continue
        span = max(ls) - min(ls)
        if span > (len(ls) - 1) * step + 0.6:
            dead.append(sid)
    out["dead_lanes"] = len(dead)
    out["dead_list"] = sorted(dead)
    return out


def work(path):
    from nf_metro.api import prepare_graph, render_graph_result, RenderConfig
    from nf_metro.themes import resolve_theme
    p = Path(path)
    rec = {"path": path}
    signal.alarm(90)
    try:
        text = p.read_text()
        graph = prepare_graph(text, source_dir=str(p.parent))
        cfg = RenderConfig(baked_mode=(graph.mode.strip() or None))
        theme_obj = resolve_theme(None, graph, mode=None)
        res = render_graph_result(graph, theme_obj, cfg)
        svg = res.content
        assert isinstance(svg, str) and svg.lstrip().startswith("<"), "not an svg"
        signal.alarm(0)
        rec["status"] = "ok"
        rec["md5"] = hashlib.md5(svg.encode()).hexdigest()
        try:
            rec.update(metrics(res.plan, graph, theme_obj, svg))
        except Exception as e:  # metric failure is recorded, not hidden
            rec["metric_error"] = f"{type(e).__name__}: {e}"
        dst = SVGDIR / (p.name + ".svg.gz")
        with gzip.open(dst, "wt") as fh:
            fh.write(svg)
        rec["svg"] = str(dst)
    except BaseException as e:  # noqa: BLE001
        signal.alarm(0)
        rec["status"] = "error"
        rec["etype"] = type(e).__name__
        rec["err"] = str(e).splitlines()[0][:400] if str(e) else ""
        rec["tb_tail"] = traceback.format_exc().splitlines()[-4:-1]
    return rec


if __name__ == "__main__":
    files = [l.strip() for l in open(sys.argv[2]) if l.strip()]
    SVGDIR.mkdir(parents=True, exist_ok=True)
    nproc = int(sys.argv[5])
    with Pool(nproc, initializer=_init, maxtasksperchild=200) as pool, open(sys.argv[3], "w") as out:
        for rec in pool.imap_unordered(work, files, chunksize=4):
            out.write(json.dumps(rec) + "\n")
            out.flush()
