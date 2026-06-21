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
