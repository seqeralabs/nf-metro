"""Dispatch-table selection tests for inter-section routing.

``_route_inter_section`` chooses a route shape from the ordered
``_INTER_SECTION_RULES`` table: the first rule whose predicate holds wins.  The
order encodes routing precedence, so a predicate edit that silently steals an
edge class from a neighbouring rule would reroute traffic without necessarily
tripping a render diff.  These tests pin the selection directly:

* synthetic ``_InterFacts`` cases assert which rule claims a constructed
  scenario (a canonical example per rule, doubling as a precedence anchor since
  several cases match more than one predicate);
* a corpus pass asserts the rules the fixtures exercise stay reachable.
"""

from __future__ import annotations

import glob
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing import inter_section_handlers as H
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

_ROOT = Path(__file__).resolve().parents[1]


def _port(side: PortSide, *, is_entry: bool) -> SimpleNamespace:
    return SimpleNamespace(side=side, is_entry=is_entry)


def _facts(**overrides: object) -> H._InterFacts:
    """A fall-through ``_InterFacts`` (no rule matches) with field overrides.

    The defaults sit at (0,0)->(100,100): not same-Y, not same-X, no ports, no
    bypass, no merge, source not a junction - so no rule claims it and the
    dispatcher falls through to the standard L-shape.  Each test overrides only
    the fields its target rule keys on.  Only ``_match_inter_section_rule`` is
    exercised (predicates), never a route builder, so duck-typed stand-ins for
    the ctx/edge/stations are enough.
    """
    ctx = SimpleNamespace(
        junction_ids=set(),
        bottom_exit_junctions=set(),
        tb_sections=set(),
        station_offsets={},
    )
    defaults: dict[str, object] = dict(
        edge=SimpleNamespace(source="a", target="b", line_id="L"),
        src=SimpleNamespace(section_id="src_sec"),
        tgt=SimpleNamespace(section_id="tgt_sec"),
        ctx=ctx,
        sx=0.0,
        sy=0.0,
        tx=100.0,
        ty=100.0,
        i=0,
        n=1,
        src_port=None,
        tgt_port=None,
        src_col=0,
        src_row=0,
        tgt_col=0,
        tgt_row=0,
        needs_bypass=False,
        merge_ep=None,
    )
    defaults.update(overrides)
    return H._InterFacts(**defaults)  # type: ignore[arg-type]


def _selected(**overrides: object) -> str:
    rule = H._match_inter_section_rule(_facts(**overrides))
    return rule.name if rule is not None else "<fall-through>"


# Each case constructs a scenario and asserts the rule that claims it.  Several
# cases satisfy more than one predicate; the expected rule is the earliest, so
# the assertion is a precedence lock, not just a reachability check.
_CASES = [
    pytest.param(
        # Also same-Y; perp-exit (rule 1) must win over same-Y straight (rule 2).
        dict(src_port=_port(PortSide.TOP, is_entry=False), sy=0.0, ty=0.0),
        "perp-exit",
        id="perp-exit-beats-same-Y",
    ),
    pytest.param(dict(sy=0.0, ty=0.0), "same-Y straight", id="same-Y"),
    pytest.param(
        dict(
            src_port=_port(PortSide.BOTTOM, is_entry=False),
            ctx=SimpleNamespace(
                junction_ids=set(),
                bottom_exit_junctions=set(),
                tb_sections={"src_sec"},
                station_offsets={"x": 1.0},
            ),
        ),
        "TB bottom exit",
        id="tb-bottom-exit",
    ),
    pytest.param(
        # Also same-X; TOP entry (rule 4) must win over same-X drop (rule 5).
        dict(tgt_port=_port(PortSide.TOP, is_entry=True), tx=0.0),
        "TOP entry L-shape",
        id="top-entry-beats-same-X",
    ),
    pytest.param(dict(tx=0.0), "same-X vertical drop", id="same-X"),
    pytest.param(
        dict(
            edge=SimpleNamespace(source="j", target="b", line_id="L"),
            ctx=SimpleNamespace(
                junction_ids=set(),
                bottom_exit_junctions={"j"},
                tb_sections=set(),
                station_offsets={},
            ),
        ),
        "bottom-exit junction",
        id="bottom-exit-junction",
    ),
    pytest.param(dict(needs_bypass=True), "bypass family", id="bypass"),
    pytest.param(
        dict(
            edge=SimpleNamespace(source="j", target="b", line_id="L"),
            tx=5.0,
            ctx=SimpleNamespace(
                junction_ids={"j"},
                bottom_exit_junctions=set(),
                tb_sections=set(),
                station_offsets={},
            ),
        ),
        "near-vertical same-col junction",
        id="near-vertical-junction",
    ),
    pytest.param(
        dict(tgt_port=_port(PortSide.RIGHT, is_entry=True)),
        "RIGHT entry wrap",
        id="right-entry-wrap",
    ),
    pytest.param(
        dict(tgt_port=_port(PortSide.LEFT, is_entry=True), tx=-100.0, tgt_row=1),
        "LEFT entry wrap family",
        id="left-entry-wrap",
    ),
    pytest.param(
        dict(
            src_port=_port(PortSide.LEFT, is_entry=False),
            tgt_port=_port(PortSide.LEFT, is_entry=True),
            tx=5.0,
            tgt_row=1,
        ),
        "serpentine LEFT exit -> LEFT entry",
        id="serpentine-left-exit-left-entry",
    ),
    pytest.param(
        dict(merge_ep=SimpleNamespace(id="ep", x=0.0, y=0.0, section_id="m")),
        "merge entry family",
        id="merge-entry",
    ),
]


@pytest.mark.parametrize("overrides, expected", _CASES)
def test_rule_selection(overrides: dict[str, object], expected: str) -> None:
    assert _selected(**overrides) == expected


def test_rule_names_unique() -> None:
    names = [r.name for r in H._INTER_SECTION_RULES]
    assert len(names) == len(set(names))


# Rules the topology/example corpus exercises.  The two omitted - "same-X
# vertical drop" and "serpentine LEFT exit -> LEFT entry" - are defensive cases
# no current fixture hits (see test_rule_selection, which locks them
# synthetically).  "RIGHT entry plough -> bypass" needs real section geometry
# its predicate scans, so it is locked by the corpus rather than synthetically.
_CORPUS_COVERED = {
    "perp-exit",
    "same-Y straight",
    "TB bottom exit",
    "TOP entry L-shape",
    "bottom-exit junction",
    "bypass family",
    "near-vertical same-col junction",
    "RIGHT entry wrap",
    "LEFT entry wrap family",
    "merge entry family",
    "RIGHT entry plough -> bypass",
}


def test_corpus_keeps_rules_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every rule the corpus exercises stays reachable through the table.

    Catches a precedence edit that makes a live rule unreachable (shadowed
    wholly by a neighbour) - the gap a synthetic case cannot see.
    """
    counts: Counter[str] = Counter()
    original = H._match_inter_section_rule

    def recording(f: H._InterFacts) -> H._Rule | None:
        rule = original(f)
        counts[rule.name if rule is not None else "<fall-through>"] += 1
        return rule

    monkeypatch.setattr(H, "_match_inter_section_rule", recording)

    fixtures = sorted(
        glob.glob(str(_ROOT / "examples/topologies/*.mmd"))
        + glob.glob(str(_ROOT / "examples/*.mmd"))
    )
    for path in fixtures:
        graph = parse_metro_mermaid(Path(path).read_text())
        compute_layout(graph)
        route_edges(graph, station_offsets=compute_station_offsets(graph))

    missing = sorted(name for name in _CORPUS_COVERED if counts[name] == 0)
    assert not missing, f"rules no longer reachable via the corpus: {missing}"
