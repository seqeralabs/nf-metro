"""Dispatch-table selection tests for inter-section routing.

``_route_inter_section`` chooses a route shape from the pairwise-disjoint
``_INTER_SECTION_RULES`` table. Canonical claim precedence is converted into
exclusive predicates when the table is built, so table order cannot reroute
traffic. These tests pin the selection directly:

* synthetic ``_InterFacts`` cases assert which rule claims a constructed
  scenario, including overlapping source claims that must resolve to one rule;
* a corpus pass asserts the rules the fixtures exercise stay reachable.
"""

from __future__ import annotations

import ast
import glob
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing import inter_section_handlers as H
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide, UnresolvedEndpointError

_ROOT = Path(__file__).resolve().parents[1]
_INTER_HANDLER_RAW_QUERY_LIMIT = 29
# Includes offsets.py's lookups: station offsets are computed before any
# per-edge _InterFacts exists, so that phase resolves grid cells directly.
_ROUTING_RAW_QUERY_LIMIT = 58


def _route_corpus(before_fixture: Callable[[str], None] | None = None) -> None:
    fixtures = sorted(
        glob.glob(str(_ROOT / "examples/topologies/*.mmd"))
        + glob.glob(str(_ROOT / "examples/*.mmd"))
    )
    for path in fixtures:
        fixture = str(Path(path).relative_to(_ROOT))
        if before_fixture is not None:
            before_fixture(fixture)
        graph = parse_metro_mermaid(Path(path).read_text())
        compute_layout(graph)
        route_edges(graph, station_offsets=compute_station_offsets(graph))


def test_inter_facts_owns_raw_section_queries() -> None:
    raw_queries = {
        "_resolve_section_col",
        "_resolve_section_row",
        "_resolve_section_colrow",
        "_h_segment_crosses_other_section",
        "_v_segment_crosses_other_section",
    }
    counts: Counter[str] = Counter()
    routing_dir = _ROOT / "src/nf_metro/layout/routing"
    for path in routing_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        counts[path.name] = sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in raw_queries
            for node in ast.walk(tree)
        )

    assert counts["inter_section_handlers.py"] <= _INTER_HANDLER_RAW_QUERY_LIMIT, (
        f"inter-section raw-query ratchet exceeded: {dict(counts)}"
    )
    assert sum(counts.values()) <= _ROUTING_RAW_QUERY_LIMIT, (
        f"routing raw-query ratchet exceeded: {dict(counts)}"
    )


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
        fanout_junctions=set(),
        bottom_exit_junctions=set(),
        tb_sections=set(),
        station_offsets={},
        merge=SimpleNamespace(trunk_source={}, branch_edges=set()),
        graph=SimpleNamespace(sections={}),
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
        cellmate_blocks_source_row=False,
        cellmate_blocks_target_row=False,
        merge_ep=None,
    )
    defaults.update(overrides)
    return H._InterFacts(**defaults)  # type: ignore[arg-type]


def _selected(**overrides: object) -> str:
    rule = H._match_inter_section_rule(_facts(**overrides))
    return rule.name if rule is not None else "<fall-through>"


# Each case constructs a scenario and asserts the exclusive rule that owns it.
# Several cases exercise overlapping source claims whose canonical ownership is
# fixed when the disjoint table is built.
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
                fanout_junctions=set(),
                bottom_exit_junctions=set(),
                tb_sections={"src_sec"},
                station_offsets={"x": 1.0},
                merge=SimpleNamespace(trunk_source={}, branch_edges=set()),
                graph=SimpleNamespace(
                    sections={"src_sec": SimpleNamespace(direction="TB", bbox_w=0.0)}
                ),
            ),
        ),
        "TB bottom exit",
        id="tb-bottom-exit",
    ),
    pytest.param(
        # A TB bottom-exit drop whose column has a section stacked between source
        # and target diverts around it; this rule must win over the plain drop.
        dict(
            src_port=_port(PortSide.BOTTOM, is_entry=False),
            sx=0.0,
            sy=0.0,
            tx=0.0,
            ty=100.0,
            ctx=SimpleNamespace(
                junction_ids=set(),
                fanout_junctions=set(),
                bottom_exit_junctions=set(),
                tb_sections={"src_sec"},
                station_offsets={"x": 1.0},
                merge=SimpleNamespace(trunk_source={}, branch_edges=set()),
                graph=SimpleNamespace(
                    sections={
                        "src_sec": SimpleNamespace(direction="TB", bbox_w=0.0),
                        "mid": SimpleNamespace(
                            id="mid",
                            bbox_x=-10.0,
                            bbox_w=20.0,
                            bbox_y=40.0,
                            bbox_h=20.0,
                        ),
                    }
                ),
            ),
        ),
        "TB bottom exit around stack",
        id="tb-bottom-exit-around-stack",
    ),
    pytest.param(
        # A BT section's trailing exit is its TOP (the rotation image of TB's
        # BOTTOM), so the same rule claims it.
        dict(
            src_port=_port(PortSide.TOP, is_entry=False),
            ctx=SimpleNamespace(
                junction_ids=set(),
                fanout_junctions=set(),
                bottom_exit_junctions=set(),
                tb_sections={"src_sec"},
                station_offsets={"x": 1.0},
                merge=SimpleNamespace(trunk_source={}, branch_edges=set()),
                graph=SimpleNamespace(
                    sections={"src_sec": SimpleNamespace(direction="BT", bbox_w=0.0)}
                ),
            ),
        ),
        "TB bottom exit",
        id="bt-top-exit",
    ),
    pytest.param(
        # Also same-X; TOP entry (rule 4) must win over same-X drop (rule 5).
        dict(tgt_port=_port(PortSide.TOP, is_entry=True), tx=0.0),
        "TOP entry L-shape",
        id="top-entry-beats-same-X",
    ),
    pytest.param(dict(tx=0.0), "same-X vertical drop", id="same-X"),
    pytest.param(
        # A stacked RIGHT-exit -> RIGHT-entry shares the column's right-edge X
        # (same_x), but the RIGHT-entry wrap must claim it over the same-X drop
        # so both ports curve out and a co-terminating feed shares the channel.
        dict(
            src_port=_port(PortSide.RIGHT, is_entry=False),
            tgt_port=_port(PortSide.RIGHT, is_entry=True),
            tx=0.0,
            tgt_row=1,
        ),
        "RIGHT entry wrap",
        id="stacked-right-ports-beats-same-X",
    ),
    pytest.param(
        dict(
            edge=SimpleNamespace(source="j", target="b", line_id="L"),
            ctx=SimpleNamespace(
                junction_ids=set(),
                fanout_junctions=set(),
                bottom_exit_junctions={"j"},
                tb_sections=set(),
                station_offsets={},
                merge=SimpleNamespace(trunk_source={}, branch_edges=set()),
            ),
        ),
        "bottom-exit junction",
        id="bottom-exit-junction",
    ),
    pytest.param(
        # The trunk feeder of a merge routes to the entry port even when it
        # also needs a bypass; "merge trunk" must win over "bypass family".
        dict(
            edge=SimpleNamespace(source="t", target="m", line_id="L"),
            needs_bypass=True,
            ctx=SimpleNamespace(
                junction_ids=set(),
                fanout_junctions=set(),
                bottom_exit_junctions=set(),
                tb_sections=set(),
                station_offsets={},
                merge=SimpleNamespace(trunk_source={"m": "t"}, branch_edges=set()),
            ),
        ),
        "merge trunk",
        id="merge-trunk-beats-bypass",
    ),
    pytest.param(
        # A non-trunk feeder of a merge descends onto the trunk channel as a
        # branch even when it would otherwise route as a plain merge-entry
        # feed; "merge branch" must win over "merge entry family".
        dict(
            edge=SimpleNamespace(source="b", target="m", line_id="L"),
            merge_ep=SimpleNamespace(id="ep", x=0.0, y=0.0, section_id="m"),
            ctx=SimpleNamespace(
                junction_ids=set(),
                fanout_junctions=set(),
                bottom_exit_junctions=set(),
                tb_sections=set(),
                station_offsets={},
                merge=SimpleNamespace(
                    trunk_source={"m": "t"},
                    branch_edges={("b", "m", "L")},
                ),
            ),
        ),
        "merge branch",
        id="merge-branch-beats-merge-entry",
    ),
    pytest.param(dict(needs_bypass=True), "bypass family", id="bypass"),
    pytest.param(
        dict(
            edge=SimpleNamespace(source="j", target="b", line_id="L"),
            tx=5.0,
            ctx=SimpleNamespace(
                junction_ids={"j"},
                fanout_junctions=set(),
                bottom_exit_junctions=set(),
                tb_sections=set(),
                station_offsets={},
                merge=SimpleNamespace(trunk_source={}, branch_edges=set()),
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
        "merge entry straight",
        id="merge-entry",
    ),
]


@pytest.mark.parametrize("overrides, expected", _CASES)
def test_rule_selection(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    expected: str,
) -> None:
    if expected == "bottom-exit junction":
        monkeypatch.setattr(
            H,
            "_bottom_exit_junction_route_kind",
            lambda _facts: H._BottomExitJunctionRoute.PLAIN,
        )
    if expected == "merge trunk":
        monkeypatch.setattr(
            H,
            "_merge_trunk_shape",
            lambda _facts: SimpleNamespace(around_below=False),
        )
    if expected == "LEFT entry wrap family":
        monkeypatch.setattr(
            H,
            "_left_entry_route_kind",
            lambda _facts: H._LeftEntryRoute.WRAP,
        )
    if expected == "merge entry straight":
        monkeypatch.setattr(
            H,
            "_merge_entry_route_kind",
            lambda _facts: H._MergeEntryRoute.STRAIGHT,
        )
    assert _selected(**overrides) == expected


def test_merge_entry_around_below_route_is_in_table() -> None:
    assert H._route_merge_entry_around_below in {
        rule.route for rule in H._INTER_SECTION_RULES
    }


# ``_would_route_around_section_below`` must claim a sibling only when the
# dispatch table routes it through the named merge-entry around-below leaf.
_SIBLING_CASES = [
    pytest.param(
        RouteFamilyId.MERGE_ENTRY_AROUND_BELOW,
        True,
        id="merge-entry-around-below",
    ),
    pytest.param(RouteFamilyId.MERGE_ENTRY, False, id="merge-entry-l-shape"),
    pytest.param(RouteFamilyId.BYPASS_FAMILY, False, id="other-family"),
    pytest.param(None, False, id="no-rule-claims-it"),
]


@pytest.mark.parametrize("family_id, expected", _SIBLING_CASES)
def test_would_route_around_section_below(
    monkeypatch: pytest.MonkeyPatch,
    family_id: RouteFamilyId | None,
    expected: bool,
) -> None:
    edge = SimpleNamespace(source="s", target="t", line_id="L")
    stations = {"s": object(), "t": object()}
    ctx = SimpleNamespace(
        graph=SimpleNamespace(
            edge_endpoints=lambda e: (stations[e.source], stations[e.target])
        )
    )
    sentinel = object()
    matched = None if family_id is None else SimpleNamespace(family_id=family_id)

    monkeypatch.setattr(H, "_build_inter_facts", lambda *a: sentinel)
    monkeypatch.setattr(H, "_match_inter_section_rule", lambda f: matched)

    assert H._would_route_around_section_below(edge, ctx) is expected  # type: ignore[arg-type]


def test_planned_family_dispatch_does_not_rematch_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edge = SimpleNamespace(source="s", target="t", line_id="L")
    station = SimpleNamespace(is_port=True)
    ctx = SimpleNamespace(junction_ids=set(), exit_turns=None)
    facts = object()
    calls: list[object] = []
    family_id = H._INTER_SECTION_RULES[0].family_id
    rule = H._Rule(family_id, "planned", lambda _facts: False, calls.append)

    monkeypatch.setattr(H, "_build_inter_facts", lambda *_args: facts)
    monkeypatch.setattr(
        H,
        "_match_inter_section_rule",
        lambda _facts: (_ for _ in ()).throw(
            AssertionError("rematched planned family")
        ),
    )
    monkeypatch.setattr(H, "_INTER_SECTION_RULES", [rule])

    assert (
        H._route_inter_section(
            edge,
            station,
            station,
            ctx,
            planned_family_id=family_id,
        )
        is None
    )
    assert calls == [facts]


def test_would_route_around_section_below_propagates_missing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dangling endpoint surfaces as the contract error, not a silent abstain."""
    edge = SimpleNamespace(source="s", target="absent", line_id="L")

    def unresolved(_e: object) -> object:
        raise UnresolvedEndpointError("absent")

    def fail(*_a: object) -> object:
        raise AssertionError("must not dispatch when an endpoint is missing")

    ctx = SimpleNamespace(graph=SimpleNamespace(edge_endpoints=unresolved))
    monkeypatch.setattr(H, "_build_inter_facts", fail)
    with pytest.raises(UnresolvedEndpointError):
        H._would_route_around_section_below(edge, ctx)  # type: ignore[arg-type]


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
    "bottom-exit junction right landings",
    "bottom-exit junction via gap",
    "bottom-exit junction",
    "merge trunk around below",
    "bypass family",
    "near-vertical same-col junction",
    "RIGHT entry wrap",
    "LEFT entry wrap family",
    "merge entry corridor",
    "merge entry family",
    "RIGHT entry plough -> bypass",
}


def test_corpus_keeps_rules_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every rule the corpus exercises stays reachable through the table.

    Catches a claim edit that makes a live rule unreachable across the corpus,
    the gap a synthetic case cannot see.
    """
    counts: Counter[str] = Counter()
    original = H._match_inter_section_rule

    def recording(f: H._InterFacts) -> H._Rule | None:
        rule = original(f)
        counts[rule.name if rule is not None else "<fall-through>"] += 1
        return rule

    monkeypatch.setattr(H, "_match_inter_section_rule", recording)

    _route_corpus()

    missing = sorted(name for name in _CORPUS_COVERED if counts[name] == 0)
    assert not missing, f"rules no longer reachable via the corpus: {missing}"


def test_corpus_inter_section_predicates_are_pairwise_disjoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlaps: set[tuple[str, tuple[str, str, str], tuple[str, ...]]] = set()
    predicate_errors: set[tuple[str, tuple[str, str, str], str, str]] = set()
    original = H._match_inter_section_rule
    current_fixture = ""

    def recording(f: H._InterFacts) -> H._Rule | None:
        matches: list[str] = []
        for rule in H._INTER_SECTION_RULES:
            try:
                if rule.when(f):
                    matches.append(rule.name)
            except (AssertionError, ValueError) as error:
                predicate_errors.add(
                    (
                        current_fixture,
                        (f.edge.source, f.edge.target, f.edge.line_id),
                        rule.name,
                        type(error).__name__,
                    )
                )
        if len(matches) > 1:
            overlaps.add(
                (
                    current_fixture,
                    (f.edge.source, f.edge.target, f.edge.line_id),
                    tuple(matches),
                )
            )
        return original(f)

    monkeypatch.setattr(H, "_match_inter_section_rule", recording)

    def set_current_fixture(fixture: str) -> None:
        nonlocal current_fixture
        current_fixture = fixture

    _route_corpus(set_current_fixture)

    failures = [
        "predicate errors:",
        *(
            f"{fixture}: {edge}: {rule}: {error}"
            for fixture, edge, rule, error in sorted(predicate_errors)
        ),
        "predicate overlaps:",
        *(
            f"{fixture}: {edge}: {', '.join(matches)}"
            for fixture, edge, matches in sorted(overlaps)
        ),
    ]
    assert not predicate_errors and not overlaps, "\n".join(failures)
