"""Each fixture in ``CURVE_ABORT_FIXTURES`` aborts on its tracked curve defect.

The tests that use these fixtures for something else render them with the
abort softened, so this module is where the abort itself stays live: each case
is a strict xfail, and fixing the defect turns it into an XPASS that fails the
run until the entry is removed.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from conftest import CURVE_ABORT_FIXTURES

from nf_metro.api import render_string

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param(rel, marks=mark, id=Path(rel).stem)
        for rel, mark in CURVE_ABORT_FIXTURES.items()
    ],
)
def test_fixture_renders_without_a_curve_abort(rel: str) -> None:
    path = ROOT / rel
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        render_string(path.read_text(), source_dir=str(path.parent))
