"""Join main/PR JSONL per corpus on path; classify; score changed renders."""
import collections
import json
import sys
from pathlib import Path

G = Path(__file__).resolve().parent
OUT = G / "out"
W = {"marker-cross": 3, "dead_lanes": 3, "crossings": 1, "label-strike": 1, "offset-collapse": 2}


def load(p):
    recs = {}
    dups = 0
    for line in open(p):
        r = json.loads(line)
        if r["path"] in recs:
            dups += 1
        recs[r["path"]] = r
    return recs, dups


def m(r):
    f = r.get("findings", {})
    return {"marker-cross": f.get("marker-cross", 0), "label-strike": f.get("label-strike", 0),
            "offset-collapse": f.get("offset-collapse", 0), "crossings": r.get("crossings", 0),
            "dead_lanes": r.get("dead_lanes", 0)}


summary = {}
changed_all = []
for corpus in ["repo", "real", "rough"]:
    lst = [l.strip() for l in open(OUT / f"list_{corpus}.txt") if l.strip()]
    mn, d1 = load(OUT / f"main_{corpus}.jsonl")
    pr, d2 = load(OUT / f"pr_{corpus}.jsonl")
    assert d1 == 0 and d2 == 0, (corpus, d1, d2)
    missing = [p for p in lst if p not in mn or p not in pr]
    s = collections.Counter()
    s["maps"] = len(lst)
    s["missing"] = len(missing)
    new_aborts, fixed, etype_change, changed = [], [], [], []
    for p in lst:
        if p not in mn or p not in pr:
            continue
        a, b = mn[p], pr[p]
        s["ok_main"] += a["status"] == "ok"
        s["ok_pr"] += b["status"] == "ok"
        if a["status"] == "ok" and b["status"] != "ok":
            new_aborts.append((p, b["etype"], b["err"]))
        elif a["status"] != "ok" and b["status"] == "ok":
            fixed.append((p, a["etype"]))
        elif a["status"] != "ok":
            s["both_err"] += 1
            if a["etype"] != b["etype"]:
                etype_change.append((p, a["etype"], b["etype"]))
        else:
            s["both_ok"] += 1
            if "metric_error" in a or "metric_error" in b:
                s["metric_error"] += 1
            if a["md5"] != b["md5"]:
                ma, mb = m(a), m(b)
                delta = {k: mb[k] - ma[k] for k in ma}
                score = sum(W[k] * v for k, v in delta.items())
                newmc = sorted(set(map(tuple, (x for x in b.get("finding_list", []) if x[0] == "marker-cross")))
                               - set(map(tuple, (x for x in a.get("finding_list", []) if x[0] == "marker-cross"))))
                newdead = sorted(set(b.get("dead_list", [])) - set(a.get("dead_list", [])))
                changed.append({"corpus": corpus, "path": p, "score": score, "delta": delta,
                                "main": ma, "pr": mb, "new_marker_cross": newmc, "new_dead": newdead})
    s["new_aborts"] = len(new_aborts)
    s["fixed_aborts"] = len(fixed)
    s["both_err_etype_changed"] = len(etype_change)
    s["changed_renders"] = len(changed)
    s["changed_worse"] = sum(c["score"] > 0 for c in changed)
    s["changed_better"] = sum(c["score"] < 0 for c in changed)
    s["changed_neutral"] = sum(c["score"] == 0 for c in changed)
    for k in W:
        s[f"sum_delta_{k}"] = sum(c["delta"][k] for c in changed)
    summary[corpus] = dict(s)
    json.dump({"new_aborts": new_aborts, "fixed": fixed, "etype_change": etype_change,
               "changed": changed, "missing": missing}, open(OUT / f"cmp_{corpus}.json", "w"), indent=1)
    changed_all += changed

json.dump(summary, open(OUT / "summary.json", "w"), indent=1)
for c, s in summary.items():
    print(c, json.dumps(s))
worst = sorted([c for c in changed_all if c["score"] > 0], key=lambda c: -c["score"])[:25]
json.dump(worst, open(OUT / "worst25.json", "w"), indent=1)
for c in worst:
    print(c["score"], c["corpus"], Path(c["path"]).name, c["delta"], c["new_marker_cross"][:3], c["new_dead"][:3])
