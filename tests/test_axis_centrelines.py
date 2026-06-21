"""Axis-generic centreline builders and the routing handlers that share them.

The single-corner and diagonal centrelines an LR/RL handler builds are the
transpose of the ones a TB handler builds.  ``geometry.single_corner_centreline``
and ``geometry.diagonal_centreline`` express that transpose once; these tests pin
the transpose contract and assert the LR and TB handlers both delegate to it.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from nf_metro.layout import geometry
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPO = Path(__file__).parent.parent / "examples" / "topologies"

SRC: geometry.Point = (0.0, 0.0)
TGT: geometry.Point = (10.0, 5.0)


def _layout(name: str) -> None:
    graph = parse_metro_mermaid((TOPO / name).read_text())
    compute_layout(graph)


def _spy(monkeypatch: pytest.MonkeyPatch, modpath: str, fname: str) -> list[dict]:
    """Wrap ``modpath.fname`` so each call's kwargs are recorded; return the log."""
    mod = importlib.import_module(modpath)
    real = getattr(mod, fname)
    calls: list[dict] = []

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append(kwargs)
        return result

    monkeypatch.setattr(mod, fname, wrapper)
    return calls


def test_single_corner_centreline_exit_transposes_by_axis() -> None:
    assert geometry.single_corner_centreline("LR", SRC, TGT, flow_first=True) == [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 5.0),
    ]
    assert geometry.single_corner_centreline("TB", SRC, TGT, flow_first=True) == [
        (0.0, 0.0),
        (0.0, 5.0),
        (10.0, 5.0),
    ]


def test_single_corner_centreline_entry_swaps_leg_order() -> None:
    # flow_first=False turns on the lane axis first; for TB that is the X leg,
    # for LR the Y leg -- the exact transpose of the flow_first=True corner.
    assert geometry.single_corner_centreline("TB", SRC, TGT, flow_first=False) == [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 5.0),
    ]
    assert geometry.single_corner_centreline("LR", SRC, TGT, flow_first=False) == [
        (0.0, 0.0),
        (0.0, 5.0),
        (10.0, 5.0),
    ]


def test_diagonal_centreline_transposes_by_axis() -> None:
    assert geometry.diagonal_centreline("LR", SRC, TGT, 3.0, 7.0) == [
        (0.0, 0.0),
        (3.0, 0.0),
        (7.0, 5.0),
        (10.0, 5.0),
    ]
    assert geometry.diagonal_centreline("TB", SRC, TGT, 3.0, 7.0) == [
        (0.0, 0.0),
        (0.0, 3.0),
        (10.0, 7.0),
        (10.0, 5.0),
    ]


def test_rl_shares_lr_orientation() -> None:
    # RL and LR share the X primary axis (RL flips only the primary sign, at
    # placement time), so the centreline builders produce the same shape.
    assert geometry.single_corner_centreline(
        "RL", SRC, TGT, flow_first=True
    ) == geometry.single_corner_centreline("LR", SRC, TGT, flow_first=True)


@pytest.mark.parametrize("axis", ["x", "y"])
def test_axis_point_split_roundtrip(axis: str) -> None:
    assert geometry.axis_split(axis, geometry.axis_point(axis, 3.0, 4.0)) == (3.0, 4.0)


@pytest.mark.parametrize("fixture", ["tb_lr_exit_left.mmd", "tb_lr_exit_right.mmd"])
def test_tb_lr_exit_routes_through_shared_corner_builder(
    monkeypatch: pytest.MonkeyPatch, fixture: str
) -> None:
    calls = _spy(
        monkeypatch, "nf_metro.layout.routing.tb_handlers", "single_corner_centreline"
    )
    _layout(fixture)
    assert any(kw.get("flow_first") is True for kw in calls)


def test_tb_lr_entry_routes_through_shared_corner_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy(
        monkeypatch, "nf_metro.layout.routing.tb_handlers", "single_corner_centreline"
    )
    _layout("tb_right_entry_stack.mmd")
    assert any(kw.get("flow_first") is False for kw in calls)


def test_lr_perp_exit_routes_through_shared_corner_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy(
        monkeypatch,
        "nf_metro.layout.routing.intra_handlers",
        "single_corner_centreline",
    )
    _layout("lr_perp_top_exit_side_entry.mmd")
    assert any(kw.get("flow_first") is True for kw in calls)


def test_tb_diagonal_routes_through_shared_diagonal_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy(
        monkeypatch, "nf_metro.layout.routing.tb_handlers", "diagonal_centreline"
    )
    _layout("tb_internal_diagonal.mmd")
    assert calls


def test_lr_diagonal_routes_through_shared_diagonal_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy(
        monkeypatch, "nf_metro.layout.routing.intra_handlers", "diagonal_centreline"
    )
    _layout("bypass_v_tight.mmd")
    assert calls
