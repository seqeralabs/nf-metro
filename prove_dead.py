"""Instrument the bundle builders and render the whole corpus.

Records, per public builder, the min centreline length and the set of
transition_leg values actually seen, plus how many times each guard's raise
arm WOULD fire.  If every centreline is >=2 vertices and every transition_leg
is in range across the whole corpus (incl. implicit-section and rail-mode
fixtures), the four guard arms are dead.
"""

from __future__ import annotations

import glob
import sys

from nf_metro.layout.routing import bundle as B

stats = {
    "concentric_minlen": 10**9,
    "tapered_minlen": 10**9,
    "offset_minlen": 10**9,
    "transition_legs": set(),
    "transition_out_of_range": 0,
    "n_legs_lt_1": 0,
}

_orig_conc = B.build_concentric_bundle
_orig_tap = B.build_tapered_bundle
_orig_off = B.build_offset_bundle


def wrap_conc(members, centerline, *a, **k):
    stats["concentric_minlen"] = min(stats["concentric_minlen"], len(centerline))
    if len(centerline) - 1 < 1:
        stats["n_legs_lt_1"] += 1
    return _orig_conc(members, centerline, *a, **k)


def wrap_tap(members, centerline, transition_leg, *a, **k):
    stats["tapered_minlen"] = min(stats["tapered_minlen"], len(centerline))
    stats["transition_legs"].add(transition_leg)
    n_legs = len(centerline) - 1
    if n_legs < 1:
        stats["n_legs_lt_1"] += 1
    if not 0 <= transition_leg <= n_legs:
        stats["transition_out_of_range"] += 1
    return _orig_tap(members, centerline, transition_leg, *a, **k)


def wrap_off(members, centerline, *a, **k):
    stats["offset_minlen"] = min(stats["offset_minlen"], len(centerline))
    if len(centerline) - 1 < 1:
        stats["n_legs_lt_1"] += 1
    return _orig_off(members, centerline, *a, **k)


B.build_concentric_bundle = wrap_conc
B.build_tapered_bundle = wrap_tap
B.build_offset_bundle = wrap_off

# Patch the names the handler modules already imported.
for modname in (
    "nf_metro.layout.routing.centrelines",
    "nf_metro.layout.routing.rail",
    "nf_metro.layout.routing.intra_handlers",
    "nf_metro.layout.routing.tb_handlers",
    "nf_metro.layout.routing.inter_section_handlers",
):
    mod = sys.modules.get(modname)
    if mod is None:
        __import__(modname)
        mod = sys.modules[modname]
    for attr, fn in (
        ("build_concentric_bundle", wrap_conc),
        ("build_tapered_bundle", wrap_tap),
        ("build_offset_bundle", wrap_off),
    ):
        if hasattr(mod, attr):
            setattr(mod, attr, fn)

from nf_metro.parser import parse_metro_mermaid  # noqa: E402
from nf_metro.layout import compute_layout  # noqa: E402

files = sorted(glob.glob("examples/**/*.mmd", recursive=True))
ok = fail = 0
for f in files:
    try:
        g = parse_metro_mermaid(open(f).read())
        compute_layout(g)
        ok += 1
    except Exception as e:  # noqa: BLE001
        fail += 1
        print(f"FAIL {f}: {type(e).__name__}: {e}")

print(f"\nrendered ok={ok} fail={fail} of {len(files)}")
print("concentric min centreline len:", stats["concentric_minlen"])
print("tapered    min centreline len:", stats["tapered_minlen"])
print("offset     min centreline len:", stats["offset_minlen"])
print("transition_leg values seen   :", sorted(stats["transition_legs"]))
print("n_legs<1 would-fire count     :", stats["n_legs_lt_1"])
print("transition out-of-range count :", stats["transition_out_of_range"])
