"""Render-geometry guards that read the rendered SVG as their own oracle.

The layout guards in :mod:`nf_metro.layout.phases.guards` and the routing
invariants validate geometry *before* the render-time regimes run -- the
per-line offsets applied by :func:`~nf_metro.render.svg.apply_route_offsets`,
the multi-line label Y-shifts, and the wrapped-label lift off foreign trunks.
The picture the user actually sees only exists in the emitted SVG.

:func:`validate_render` closes that gap from the other side: it parses the
finished artifact back into node markers (from the embedded manifest), route
polylines (from the drawn ``<path data-line-id>`` ink), and label ink boxes
(from the drawn ``<text>`` ink), then runs render-geometry checks on what was
drawn.  Because the predicate is the project's authoritative one
(:func:`~nf_metro.layout.labels.segment_strikes_label`) applied to the parsed
ink, a finding means the rendered image is wrong, not that a re-derivation
diverged.

The checks are decoupled from the engine: they need only the SVG string, so
they validate a produced file in CI or standalone without re-running layout,
and they see the render-only geometry the pre-render guards cannot.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from nf_metro.layout.constants import LABEL_FONT_SIZE, LABEL_LINE_HEIGHT
from nf_metro.layout.labels import (
    LabelPlacement,
    font_scale_context,
    segment_strikes_label,
)
from nf_metro.manifest import read_manifest

# The defect family of a finding; one per render-geometry check.
LABEL_STRIKE = "label-strike"


class RenderFinding(NamedTuple):
    """One render-geometry defect read back out of the drawn SVG.

    Captured from the rendered ink (after the offset and label-lift regimes),
    so a finding is a real visual defect in the user's output.  ``segment`` is
    the drawn line segment ``((x1, y1), (x2, y2))`` that triggered it.
    """

    kind: str
    line_id: str
    station_id: str
    message: str
    segment: tuple[tuple[float, float], tuple[float, float]]


class _Label(NamedTuple):
    placement: LabelPlacement
    font_size: float


# A single route renders as one element per metro line; ``metro-direction-*``
# chevrons (the optional directional markers) also carry ``data-line-id`` but
# are not the route, so the route class substring gates them out.
_ROUTE_CLASS = "metro-line-"
_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_ELEMENT = re.compile(r"<(path|line)\b([^>]*?)/?>", re.DOTALL)
_TEXT = re.compile(r"<text\b([^>]*?)>(.*?)</text>", re.DOTALL)
_TSPAN = re.compile(r"<tspan\b[^>]*>(.*?)</tspan>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def _attr(attrs: str, name: str, default: str | None = None) -> str | None:
    match = re.search(rf'\b{name}="([^"]*)"', attrs)
    return match.group(1) if match else default


def parse_route_polylines(
    svg: str,
) -> list[tuple[str, list[list[tuple[float, float]]]]]:
    """Reconstruct each drawn route's polylines from the SVG ink.

    Returns ``(line_id, subpaths)`` per drawn route element, where each subpath
    is a list of ``(x, y)`` vertices.  A route splits into several subpaths at
    every ``M`` after the first, so a bridged edge's hop gap is a real break
    rather than a phantom segment spanning it.  Each smoothing ``Q`` is
    collapsed back to its corner (the control point), which is the exact
    pre-smoothing vertex, so the polyline equals the logical routed path.
    """
    routes: list[tuple[str, list[list[tuple[float, float]]]]] = []
    for tag, attrs in _ELEMENT.findall(svg):
        cls = _attr(attrs, "class") or ""
        if _ROUTE_CLASS not in cls:
            continue
        line_id = _attr(attrs, "data-line-id") or ""
        if tag == "line":
            x1, y1, x2, y2 = (
                float(_attr(attrs, k) or 0.0) for k in ("x1", "y1", "x2", "y2")
            )
            routes.append((line_id, [[(x1, y1), (x2, y2)]]))
            continue
        d = _attr(attrs, "d") or ""
        subpaths: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for cmd, body in re.findall(r"([MLQ])([^MLQ]*)", d):
            nums = [float(n) for n in _NUM.findall(body)]
            if cmd == "M":
                if current:
                    subpaths.append(current)
                current = [(nums[0], nums[1])]
            elif cmd == "L":
                current.append((nums[0], nums[1]))
            elif cmd == "Q":
                if current:
                    current.pop()
                current.append((nums[0], nums[1]))
        if current:
            subpaths.append(current)
        routes.append((line_id, subpaths))
    return routes


def parse_station_labels(svg: str) -> list[_Label]:
    """Reconstruct station-label ink placements from the drawn ``<text>`` ink.

    Inverts :func:`~nf_metro.render.svg._render_labels`' emission: the
    ``dominant-baseline`` attribute names the side (``auto`` above, ``hanging``
    below, ``central`` centred), the multi-line block's anchor Y is recovered
    from the per-line ``dy`` stacking the renderer applied, and a ``rotate``
    transform restores the diagonal angle.  The resulting
    :class:`~nf_metro.layout.labels.LabelPlacement` feeds the authoritative
    glyph-ink predicates so the box matches what was drawn.
    """
    labels: list[_Label] = []
    for attrs, body in _TEXT.findall(svg):
        station_id = _attr(attrs, "data-station-id")
        cls = _attr(attrs, "class") or ""
        if station_id is None or "station-label" not in cls:
            continue
        spans = _TSPAN.findall(body)
        text = (
            "\n".join(_TAG.sub("", s) for s in spans) if spans else _TAG.sub("", body)
        )
        text = text.strip("\n")
        x = float(_attr(attrs, "x") or 0.0)
        y = float(_attr(attrs, "y") or 0.0)
        font_size = float(_attr(attrs, "font-size") or LABEL_FONT_SIZE)
        anchor = _attr(attrs, "text-anchor") or "start"
        baseline = _attr(attrs, "dominant-baseline") or ""
        rotate = re.search(r"rotate\(([-\d.]+)", attrs)
        angle = float(rotate.group(1)) if rotate else 0.0

        # A rotated label is emitted at its anchor already; only the upright
        # baselines carry the multi-line Y stacking that has to be undone.
        n_lines = text.count("\n") + 1
        line_spacing = font_size * LABEL_LINE_HEIGHT
        above = False
        dominant = ""
        if not angle:
            if baseline == "auto":
                above = True
                y += (n_lines - 1) * line_spacing
            elif baseline == "central":
                dominant = "central"
                y += (n_lines - 1) * line_spacing / 2
            elif baseline and baseline != "hanging":
                dominant = baseline

        labels.append(
            _Label(
                LabelPlacement(
                    station_id=station_id,
                    text=text,
                    x=x,
                    y=y,
                    above=above,
                    angle=angle,
                    text_anchor=anchor,
                    dominant_baseline=dominant,
                ),
                font_size,
            )
        )
    return labels


def _nodes_by_id(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in manifest.get("nodes", [])}


def check_label_strikes(
    svg: str,
    manifest: dict[str, Any],
    routes: list[tuple[str, list[list[tuple[float, float]]]]],
) -> list[RenderFinding]:
    """A drawn route segment striking through a station name's glyph ink.

    A line painting over a label reads as a strike-through, and a pixel oracle
    false-negatives on it because the line covers the text.  A segment is exempt
    when its line is one the labelled station carries (the
    manifest ``groups``, which subsumes the case of the station being that
    edge's own endpoint).  Uses the authoritative glyph-ink footprint at the
    label's drawn font scale, so it fires only on the inked glyphs, not the
    reserved margin around them.
    """
    nodes = _nodes_by_id(manifest)
    findings: list[RenderFinding] = []
    for placement, font_size in parse_station_labels(svg):
        node = nodes.get(placement.station_id)
        if node is None:
            continue
        carried = set(node.get("groups", ()))
        scale = font_size / LABEL_FONT_SIZE
        with font_scale_context(scale):
            for line_id, subpaths in routes:
                if line_id in carried:
                    continue
                for subpath in subpaths:
                    hit = _first_striking_segment(subpath, placement)
                    if hit is not None:
                        findings.append(
                            RenderFinding(
                                LABEL_STRIKE,
                                line_id,
                                placement.station_id,
                                f"line {line_id!r} strikes through the label of "
                                f"{placement.station_id!r} ({placement.text!r})",
                                hit,
                            )
                        )
                        break
    return findings


def _first_striking_segment(
    subpath: list[tuple[float, float]], placement: LabelPlacement
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    for i in range(len(subpath) - 1):
        (x1, y1), (x2, y2) = subpath[i], subpath[i + 1]
        if segment_strikes_label(x1, y1, x2, y2, placement):
            return ((x1, y1), (x2, y2))
    return None


def validate_render(svg: str) -> list[RenderFinding]:
    """Run the render-geometry guards on a rendered SVG and return findings.

    Reads the embedded manifest for node identities and parses the drawn route
    and label ink, then checks for label strikes on the geometry as drawn.
    Returns an empty list for a clean render, or when the SVG carries no
    manifest (nothing addressable to validate).
    """
    manifest = read_manifest(svg)
    if manifest is None:
        return []
    routes = parse_route_polylines(svg)
    return check_label_strikes(svg, manifest, routes)
