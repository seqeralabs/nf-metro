"""Keep the inter-phase state protocol (``layout/phase_state.py``) honest.

The registry declares every ``graph._*`` field a layout stage writes for a
later stage to read. These tests pin it against the three things it must not
drift from:

* the ``MetroGraph`` dataclass fields it names,
* the engine's ``_snap`` stage checkpoints (``CANONICAL_STAGE_ORDER``), and
* ``CONTRACT.md``,

and they fail when a new bare cross-phase ``graph._*`` poke is introduced
without being declared, so the protocol can't grow an undocumented member.
A behavioural test pins the write-before-read enforcement itself.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from nf_metro.layout.phase_state import (
    CANONICAL_STAGE_ORDER,
    PHASE_FIELD_REGISTRY,
    PRE_LAYOUT,
    FieldEnforcement,
    require_phase_field,
)
from nf_metro.layout.phases.guards import (
    _PASS_C_BISECTION_ORDER,
    PhaseInvariantError,
)
from nf_metro.parser.model import MetroGraph

REPO = Path(__file__).resolve().parent.parent
LAYOUT_DIR = REPO / "src" / "nf_metro" / "layout"
ENGINE = LAYOUT_DIR / "engine.py"
CONTRACT = LAYOUT_DIR / "CONTRACT.md"

# Cross-phase ``graph._*`` channels that are deliberately NOT part of the
# inter-phase positioning protocol, so they are not in PHASE_FIELD_REGISTRY:
#   - _stages_completed / _validate_active: the enforcement mechanism itself.
#   - _defer_final_guards: a pass-control flag toggled by compute_layout.
#   - _explicit_directions: parse/auto-layout metadata, read by a guard.
#   - _rail_y: rail-mode router metadata, not section positioning.
#   - _cross_column_perp_bridges: a routing-invariant relaxation flag.
_NON_PROTOCOL_CROSS_PHASE = {
    "_stages_completed",
    "_validate_active",
    "_defer_final_guards",
    "_explicit_directions",
    "_rail_y",
    "_cross_column_perp_bridges",
}

_COMMENT = re.compile(r"#.*$", re.MULTILINE)
_SNAP_ID = re.compile(r'_snap\(graph,\s*"([^"]+)"\)')
_STAGE_ID = re.compile(r"^\d+\.\d+[a-z]?$")
_GRAPH_ATTR = re.compile(r"\bgraph\.([A-Za-z_][A-Za-z0-9_]*)")
_GRAPH_WRITE = re.compile(
    r"\bgraph\.([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:=[^=]|\.(?:append|extend|update|add|clear|pop|setdefault|discard)\b|\[)"
)


def _strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


# --- registry <-> dataclass -------------------------------------------------


def test_registry_keys_match_spec_names():
    for name, spec in PHASE_FIELD_REGISTRY.items():
        assert spec.name == name, f"{name!r} keyed under a different spec.name"


@pytest.mark.parametrize("name", sorted(PHASE_FIELD_REGISTRY))
def test_registered_field_is_a_repr_false_dataclass_field(name):
    """Every declared channel is a real MetroGraph field hidden from repr."""
    fields = {f.name: f for f in dataclasses.fields(MetroGraph)}
    assert name in fields, f"{name!r} is in the registry but not a MetroGraph field"
    assert fields[name].repr is False, (
        f"{name!r} is an inter-phase channel and should be declared repr=False"
    )


# --- stage-id validity and ordering ----------------------------------------


@pytest.mark.parametrize("name", sorted(PHASE_FIELD_REGISTRY))
def test_writer_and_reader_stages_are_known(name):
    spec = PHASE_FIELD_REGISTRY[name]
    assert (
        spec.writer_stage in CANONICAL_STAGE_ORDER or spec.writer_stage == PRE_LAYOUT
    ), f"{name}: writer_stage {spec.writer_stage!r} is not a known stage"
    for r in spec.reader_stages:
        assert r in CANONICAL_STAGE_ORDER, (
            f"{name}: reader_stage {r!r} is not in CANONICAL_STAGE_ORDER"
        )


@pytest.mark.parametrize("name", sorted(PHASE_FIELD_REGISTRY))
def test_require_writer_fields_have_writer_before_readers(name):
    """A gated field's writer must precede every reader in pipeline order.

    Skipped for FALLBACK fields, whose readers may legitimately run before the
    writer (e.g. _struct_height_below_top is read at 6.13 and snapshotted at
    6.15a) precisely because the read tolerates the unwritten value.
    """
    spec = PHASE_FIELD_REGISTRY[name]
    if spec.enforcement is not FieldEnforcement.REQUIRE_WRITER:
        pytest.skip("FALLBACK field: write-before-read ordering not required")
    if spec.writer_stage == PRE_LAYOUT:
        return
    w = CANONICAL_STAGE_ORDER.index(spec.writer_stage)
    for r in spec.reader_stages:
        assert w < CANONICAL_STAGE_ORDER.index(r), (
            f"{name}: writer Stage {spec.writer_stage} does not precede "
            f"reader Stage {r}"
        )


@pytest.mark.parametrize("name", sorted(PHASE_FIELD_REGISTRY))
def test_run_condition_attr_is_a_graph_field(name):
    spec = PHASE_FIELD_REGISTRY[name]
    if spec.run_condition_attr is None:
        return
    field_names = {f.name for f in dataclasses.fields(MetroGraph)}
    assert spec.run_condition_attr in field_names, (
        f"{name}: run_condition_attr {spec.run_condition_attr!r} is not a "
        f"MetroGraph field"
    )


# --- CANONICAL_STAGE_ORDER <-> engine --------------------------------------


def test_canonical_stage_order_matches_engine_snap_calls():
    """The stage list equals the engine's stage-id ``_snap`` checkpoints."""
    text = _strip_comments(ENGINE.read_text())
    snap_ids = _SNAP_ID.findall(text)
    stage_ids = tuple(s for s in snap_ids if _STAGE_ID.match(s))
    assert stage_ids == CANONICAL_STAGE_ORDER, (
        "CANONICAL_STAGE_ORDER drifted from engine _snap() checkpoints:\n"
        f"  engine: {stage_ids}\n  canonical: {CANONICAL_STAGE_ORDER}"
    )


def test_pass_c_bisection_order_is_a_subsequence():
    """The Pass C checkpoint stages are an ordered subset of the full list."""
    pass_c = [p.replace("after Stage ", "") for p in _PASS_C_BISECTION_ORDER]
    it = iter(CANONICAL_STAGE_ORDER)
    for stage in pass_c:
        assert stage in it, (
            f"Pass C checkpoint {stage!r} is out of order or absent from "
            f"CANONICAL_STAGE_ORDER"
        )


# --- no unregistered cross-phase poke --------------------------------------


def _cross_phase_underscore_attrs() -> set[str]:
    """``graph._*`` attrs written in one layout module and read in another."""
    writers: dict[str, set[str]] = {}
    readers: dict[str, set[str]] = {}
    for path in LAYOUT_DIR.rglob("*.py"):
        if path.name == "phase_state.py":
            continue  # the registry/helper module, not a phase
        text = _strip_comments(path.read_text())
        mod = str(path.relative_to(LAYOUT_DIR))
        for m in _GRAPH_WRITE.finditer(text):
            writers.setdefault(m.group(1), set()).add(mod)
        for m in _GRAPH_ATTR.finditer(text):
            readers.setdefault(m.group(1), set()).add(mod)
    cross: set[str] = set()
    for attr, ws in writers.items():
        if not attr.startswith("_"):
            continue
        if readers.get(attr, set()) - ws:  # read somewhere it isn't written
            cross.add(attr)
    return cross


def test_no_unregistered_cross_phase_channel():
    """A new cross-phase ``graph._*`` poke must be registered or allowlisted."""
    cross = _cross_phase_underscore_attrs()
    unaccounted = cross - set(PHASE_FIELD_REGISTRY) - _NON_PROTOCOL_CROSS_PHASE
    assert not unaccounted, (
        "These graph._* fields are written in one layout module and read in "
        "another but are neither in PHASE_FIELD_REGISTRY nor allowlisted as "
        f"non-protocol state: {sorted(unaccounted)}. Declare them in "
        "phase_state.py (preferred) or add to _NON_PROTOCOL_CROSS_PHASE."
    )


# --- CONTRACT.md sync ------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PHASE_FIELD_REGISTRY))
def test_registered_field_documented_in_contract(name):
    assert name in CONTRACT.read_text(), (
        f"{name!r} is in PHASE_FIELD_REGISTRY but not documented in CONTRACT.md"
    )


# --- enforcement behaviour -------------------------------------------------


def _require_writer_field() -> str:
    for name, spec in PHASE_FIELD_REGISTRY.items():
        if (
            spec.enforcement is FieldEnforcement.REQUIRE_WRITER
            and spec.run_condition_attr is None
            and spec.writer_stage != PRE_LAYOUT
        ):
            return name
    raise AssertionError("expected at least one unconditional REQUIRE_WRITER field")


def test_read_before_writer_raises_under_validation():
    name = _require_writer_field()
    graph = MetroGraph()
    graph._validate_active = True
    graph._stages_completed = []  # writer stage has not run
    with pytest.raises(PhaseInvariantError):
        require_phase_field(graph, name)


def test_read_after_writer_does_not_raise():
    name = _require_writer_field()
    spec = PHASE_FIELD_REGISTRY[name]
    graph = MetroGraph()
    graph._validate_active = True
    graph._stages_completed = [spec.writer_stage]
    require_phase_field(graph, name)  # no raise


def test_no_enforcement_when_validate_inactive():
    name = _require_writer_field()
    graph = MetroGraph()
    graph._validate_active = False
    graph._stages_completed = []
    require_phase_field(graph, name)  # no raise


def test_run_condition_skips_check_when_falsy():
    """A conditionally-skipped writer does not trip a false positive."""
    conditional = [
        n
        for n, s in PHASE_FIELD_REGISTRY.items()
        if s.enforcement is FieldEnforcement.REQUIRE_WRITER
        and s.run_condition_attr is not None
    ]
    if not conditional:
        pytest.skip("no conditionally-gated REQUIRE_WRITER field")
    name = conditional[0]
    spec = PHASE_FIELD_REGISTRY[name]
    graph = MetroGraph()
    graph._validate_active = True
    graph._stages_completed = []
    setattr(graph, spec.run_condition_attr, False)
    require_phase_field(graph, name)  # writer not expected to have run -> no raise
    setattr(graph, spec.run_condition_attr, True)
    with pytest.raises(PhaseInvariantError):
        require_phase_field(graph, name)


def test_fallback_field_never_raises():
    fallback = [
        n
        for n, s in PHASE_FIELD_REGISTRY.items()
        if s.enforcement is FieldEnforcement.FALLBACK
    ]
    assert fallback, "expected at least one FALLBACK field"
    graph = MetroGraph()
    graph._validate_active = True
    graph._stages_completed = []
    for name in fallback:
        require_phase_field(graph, name)  # no raise regardless of stage state
