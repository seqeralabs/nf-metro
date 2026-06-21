"""Structural invariants of the guard + routing-check registries.

The golden-baseline oracle (``test_guard_registry_golden``) pins the *runtime*
guard call sequence.  These tests pin the *classification* registries
themselves: every guard and check is registered, the tiers are well-formed,
and the Tier-A routing-check set provably equals the always-on render
chokepoint, so the tier table in ``docs/dev/guard_tiers.md`` cannot drift from
the code it documents.
"""

from __future__ import annotations

import inspect
import re

from nf_metro.layout.phases import guards
from nf_metro.layout.phases.guards import GUARD_REGISTRY
from nf_metro.layout.routing import invariants
from nf_metro.layout.routing.invariants import (
    CHECK_REGISTRY,
    assert_render_curve_invariants,
)

VALID_TIERS = {"A", "B", "C"}


def _defined(module, prefix: str) -> set[str]:
    return {
        name
        for name, obj in vars(module).items()
        if name.startswith(prefix) and inspect.isfunction(obj)
    }


def test_guard_registry_tiers_are_well_formed() -> None:
    assert all(spec.tier in VALID_TIERS for spec in GUARD_REGISTRY)
    names = [spec.name for spec in GUARD_REGISTRY]
    assert len(names) == len(set(names)), "duplicate guard in registry"


def test_registry_bisection_set_is_the_pass_c_prefix() -> None:
    """The bisection-safe specs are a contiguous prefix of the registry: the
    runner relies on this so a Pass C checkpoint and the final block share one
    ordered list."""
    flags = [spec.bisection_safe for spec in GUARD_REGISTRY]
    first_final = flags.index(False)
    assert all(flags[:first_final]), "bisection-safe specs must come first"
    assert not any(flags[first_final:]), "bisection-safe specs must be contiguous"


def test_derived_bisection_first_valid_matches_registry() -> None:
    """``_BISECTION_FIRST_VALID`` is derived from the registry, so the two must
    agree and only bisection-safe specs may carry a threshold."""
    expected = {
        spec.name: spec.first_valid_stage
        for spec in GUARD_REGISTRY
        if spec.bisection_safe and spec.first_valid_stage is not None
    }
    assert guards._BISECTION_FIRST_VALID == expected
    for spec in GUARD_REGISTRY:
        if spec.first_valid_stage is not None:
            assert spec.bisection_safe
            assert spec.first_valid_stage in guards._PASS_C_BISECTION_ORDER


def test_check_registry_classifies_every_check() -> None:
    """Every ``check_*`` invariant must be classified, so a new check cannot
    escape the tier table."""
    registered = {spec.name for spec in CHECK_REGISTRY}
    defined = _defined(invariants, "check_")
    assert registered == defined, (
        f"unclassified checks: {sorted(defined - registered)}; "
        f"stale registry entries: {sorted(registered - defined)}"
    )
    assert all(spec.tier in VALID_TIERS for spec in CHECK_REGISTRY)


def test_tier_a_checks_are_exactly_the_render_chokepoint() -> None:
    """Tier A means 'already always-on'.  For routing checks that is precisely
    the set called by :func:`assert_render_curve_invariants`, so the two must
    match exactly -- a check moved in or out of the chokepoint must move tier."""
    chokepoint = set(
        re.findall(
            r"\bcheck_[a-z_]+", inspect.getsource(assert_render_curve_invariants)
        )
    )
    tier_a = {spec.name for spec in CHECK_REGISTRY if spec.tier == "A"}
    assert tier_a == chokepoint, (
        f"Tier-A checks {sorted(tier_a)} != chokepoint {sorted(chokepoint)}"
    )


def _all_guard_specs() -> list:
    """Every classified guard: the dispatched ``GUARD_REGISTRY`` plus the
    classification-only ``INLINE_GUARD_REGISTRY`` (guards engine.py invokes at
    a specific stage rather than through the Pass C / final runner)."""
    return [*GUARD_REGISTRY, *guards.INLINE_GUARD_REGISTRY]


def _issue_pins(spec) -> tuple[str, ...]:
    """Normalise a spec's ``issue_pin`` to a tuple of ``#NNN`` tokens."""
    pin = spec.issue_pin
    if not pin:
        return ()
    return (pin,) if isinstance(pin, str) else tuple(pin)


def _guards_citing_an_issue() -> dict[str, set[str]]:
    """Map every ``_guard_*`` whose source cites a ``#NNN`` issue to those
    issue tokens, so a guard born of a specific bug cannot silently drop the
    regression trail."""
    out: dict[str, set[str]] = {}
    for name, obj in vars(guards).items():
        if name.startswith("_guard_") and inspect.isfunction(obj):
            issues = set(re.findall(r"#\d{3,}", inspect.getsource(obj)))
            if issues:
                out[name] = issues
    return out


def test_no_registry_guard_duplicates_an_always_on_check() -> None:
    """A ``validate=True`` guard that merely raises around a check already in
    the always-on render chokepoint is pure duplication: the check runs on
    every render regardless of ``validate``.  The check is the single
    authority; the guard wrapper must not re-register it."""
    chokepoint = set(
        re.findall(
            r"\bcheck_[a-z_]+", inspect.getsource(assert_render_curve_invariants)
        )
    )
    offenders = {}
    for spec in GUARD_REGISTRY:
        refs = set(re.findall(r"\bcheck_[a-z_]+", inspect.getsource(spec.fn)))
        dup = refs & chokepoint
        if dup:
            offenders[spec.name] = sorted(dup)
    assert not offenders, (
        "validate-only guards duplicate always-on render-chokepoint checks "
        f"(drop the wrapper; the check already runs on every render): {offenders}"
    )


def test_every_guard_is_classified_in_exactly_one_registry() -> None:
    """Every defined ``_guard_*`` lives in exactly one of the two guard
    registries, so no guard escapes tier / issue-pin classification."""
    defined = _defined(guards, "_guard_")
    names = [spec.name for spec in _all_guard_specs()]
    duplicated = {n for n in names if names.count(n) > 1}
    assert not duplicated, f"guards in more than one registry: {sorted(duplicated)}"
    unclassified = defined - set(names)
    assert not unclassified, f"unclassified guards: {sorted(unclassified)}"
    stale = set(names) - defined
    assert not stale, f"registry names with no guard: {sorted(stale)}"


def test_issue_pinned_guards_record_their_issue_as_data() -> None:
    """Every guard whose source cites an issue carries that issue in its
    spec's ``issue_pin``, so consolidation cannot lose the regression trail."""
    by_name = {spec.name: spec for spec in _all_guard_specs()}
    missing = {}
    for name, issues in _guards_citing_an_issue().items():
        spec = by_name.get(name)
        pinned = set(_issue_pins(spec)) if spec else set()
        absent = issues - pinned
        if absent:
            missing[name] = sorted(absent)
    assert not missing, f"guards citing an issue but not pinning it as data: {missing}"


def test_issue_pinned_guards_document_why_they_are_narrow() -> None:
    """A guard kept pinned to a past issue must populate ``narrow_reason``
    saying why it stays narrow rather than expressing a general property, so
    the field is an enforced contract rather than optional documentation."""
    undocumented = [
        spec.name
        for spec in _all_guard_specs()
        if _issue_pins(spec) and not spec.narrow_reason
    ]
    assert not undocumented, (
        f"issue-pinned guards with no narrow_reason: {sorted(undocumented)}"
    )
