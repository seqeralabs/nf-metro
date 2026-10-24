"""The corridor-cohort shadow oracle fails exactly the maps its ratchet lists."""

from __future__ import annotations

from pathlib import Path

import cohort_shadow_oracle
import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("relative_path", cohort_shadow_oracle.repository_maps())
def test_shadow_oracle_failures_match_the_ratchet(relative_path: str) -> None:
    """Every repository map the shadow oracle fails is listed, and no other.

    A listed map that starts passing is removed from the list in the same
    change, so the ratchet records exactly the maps still drawn off their plan.
    """
    failure = cohort_shadow_oracle.shadow_failure(ROOT / relative_path)

    if relative_path in cohort_shadow_oracle.read_ratchet():
        assert failure is not None, (
            f"{relative_path} passes the shadow oracle; remove it from "
            f"{cohort_shadow_oracle.RATCHET_PATH.relative_to(ROOT)}"
        )
    else:
        assert failure is None, failure


def test_shadow_ratchet_lists_only_repository_maps() -> None:
    listed = cohort_shadow_oracle.read_ratchet()

    assert list(listed) == sorted(set(listed))
    assert set(listed) <= set(cohort_shadow_oracle.repository_maps())
