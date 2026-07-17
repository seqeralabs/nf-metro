"""The parse/layout-authoring errors ``render_string`` can raise share one base.

An embedder calling :func:`nf_metro.render_string` directly (rather than
through the CLI, which converts these into a ``click.ClickException``) needs
to know what it can catch. :class:`~nf_metro.NfMetroError` is the common base
for the errors that mean "this input was rejected", documented precisely in
:func:`nf_metro.api.prepare_graph`'s docstring and in the embedding guide.
This module locks that contract: every specific error is-a
:class:`NfMetroError`, and ``render_string`` raises the documented type for
each representative class of bad input (a plain parse error, which is
deliberately *not* an :class:`NfMetroError`, plus a same-row backward feed
and a mixed-entry-direction section). The cyclic-graph case is covered
alongside it in ``tests/test_api.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro import NfMetroError, render_string
from nf_metro.layout import (
    BackwardFlowError,
    FoldThresholdError,
    MixedEntryDirectionError,
    PhaseInvariantError,
)
from nf_metro.layout.phases.guards import LayoutInvariantError
from nf_metro.parser import (
    CyclicGraphError,
    UnresolvedEndpointError,
    UnresolvedPortSectionError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
INVALID = REPO_ROOT / "tests" / "fixtures" / "invalid"
NEXTFLOW_FLOWCHART = REPO_ROOT / "tests" / "fixtures" / "nextflow"

# PhaseInvariantError/LayoutInvariantError report an engine self-check, not a
# bad input, so they deliberately don't share ValueError's "invalid input"
# base -- kept apart from the ValueError-based authoring errors below.
NOT_VALUE_ERRORS = (PhaseInvariantError, LayoutInvariantError)
AUTHORING_ERROR_TYPES = [
    CyclicGraphError,
    UnresolvedEndpointError,
    UnresolvedPortSectionError,
    BackwardFlowError,
    MixedEntryDirectionError,
    FoldThresholdError,
    *NOT_VALUE_ERRORS,
]


@pytest.mark.parametrize("error_type", AUTHORING_ERROR_TYPES, ids=lambda t: t.__name__)
def test_authoring_error_is_nf_metro_error(error_type: type[Exception]) -> None:
    assert issubclass(error_type, NfMetroError)


@pytest.mark.parametrize(
    "error_type",
    [t for t in AUTHORING_ERROR_TYPES if t not in NOT_VALUE_ERRORS],
    ids=lambda t: t.__name__,
)
def test_authoring_error_keeps_its_value_error_base(
    error_type: type[Exception],
) -> None:
    """Reparenting under NfMetroError must not drop the pre-existing base a
    caller may already be catching (e.g. a bare ``except ValueError``)."""
    assert issubclass(error_type, ValueError)


def test_phase_invariant_error_is_not_a_value_error() -> None:
    assert not issubclass(PhaseInvariantError, ValueError)


def test_render_string_raises_plain_value_error_for_malformed_mmd() -> None:
    """A genuine grammar/parse failure is a plain ValueError, not routed
    through NfMetroError -- the parser raises many of these ad hoc, so this
    stays outside the reparented hierarchy (see the ``prepare_graph``
    docstring)."""
    src = (NEXTFLOW_FLOWCHART / "flat_pipeline.mmd").read_text()
    with pytest.raises(ValueError) as excinfo:
        render_string(src)
    assert not isinstance(excinfo.value, NfMetroError)


@pytest.mark.parametrize(
    "fixture,expected_type",
    [
        pytest.param(
            INVALID / "merge_trunk_rightward_source.mmd",
            BackwardFlowError,
            id="backward-flow",
        ),
        pytest.param(
            INVALID / "mixed_entry_opposing.mmd",
            MixedEntryDirectionError,
            id="mixed-entry",
        ),
    ],
)
def test_render_string_raises_documented_authoring_error(
    fixture: Path, expected_type: type[Exception]
) -> None:
    """render_string raises the documented type for each class of rejected
    input, and that type is always catchable as one NfMetroError -- the
    entire point of the base class."""
    with pytest.raises(expected_type) as excinfo:
        render_string(fixture.read_text())
    assert isinstance(excinfo.value, NfMetroError)
