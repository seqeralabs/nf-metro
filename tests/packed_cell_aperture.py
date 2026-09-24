"""A corridor order the packed-cell fixture's column-2 aperture cannot afford.

``packed_cell_right_exit_left_entry_wrap.mmd`` compiles ``PLANNED``: in the
column-2 corridor ``assembled``'s run (segment 1) is drawn 17px left of
``reference``'s run into ``annot`` (segment 3), and ``qc``'s two feeders sit
12px right of ``assembled`` and 20px inside their 1037.5 ceiling.  Directing
``assembled`` 5px right of that ``reference`` run instead drags ``qc``'s
cohort 2.0px past its ceiling, a shortfall the producer maps to one aperture
requirement at column boundary 2 with ``assemble`` on the negative side and
``qc`` on the positive.

No topology under ``examples/topologies/`` draws this order on its own; the
directed separation ``direct_assembled_past_reference`` installs below is
synthetic, added by monkeypatching ``corridor_cohort_integration._problem`` so
a test can reach the aperture-grant path directly without a corpus fixture
that produces the requirement.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from nf_metro.layout.routing import corridor_cohort_integration
from nf_metro.layout.routing.corridor_cohorts import CorridorDirectedSeparation

PACKED_CELL = "examples/topologies/packed_cell_right_exit_left_entry_wrap.mmd"

_ASSEMBLED_RUN = (("assemble__exit_right_2", "polish__entry_left_7", "assembled"), 1)
_REFERENCE_RUN = (("__junction_10", "annot__entry_left_9", "reference"), 3)

#: The requirement the directed order leaves: the 54.0px ``assemble``|``qc``
#: gap plus the 2.0px the ``qc`` cohort is short.
REQUIRED_APERTURE = 56.0


def direct_assembled_past_reference(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Add the directed separation to every problem holding both runs.

    Returns the owner ids of the separations added, so a caller can prove the
    problem it meant to perturb was built.
    """
    added: list[str] = []
    real_problem = corridor_cohort_integration._problem

    def problem(claims, *args, **kwargs):
        built = real_problem(claims, *args, **kwargs)
        claim_ids = {
            (claim.target.edge_key, claim.ledger.segment_rank): claim.claim_id
            for claim in claims
        }
        upper = claim_ids.get(_ASSEMBLED_RUN)
        lower = claim_ids.get(_REFERENCE_RUN)
        if upper is None or lower is None:
            return built
        owner = f"test-order|{lower}|{upper}"
        added.append(owner)
        return replace(
            built,
            directed_separations=(
                *built.directed_separations,
                CorridorDirectedSeparation(owner, lower, upper, 5.0),
            ),
        )

    monkeypatch.setattr(corridor_cohort_integration, "_problem", problem)
    return added
