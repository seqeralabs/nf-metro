"""Witnesses that two offset-repair passes actually move geometry.

Both passes below fire on the shipped corpus yet leave almost every render
byte-identical, so a coverage sweep reads them as dead weight.  They are not:
neutering either one moves the settled offsets of the fixture named here.  The
witnesses are narrow -- ``_exchange_pair_beyond_exit`` is reached by two shipped
fixtures and by none of the 400 generated fan fixtures -- which is exactly why
the effect needs pinning rather than re-deriving by eye.

Each test compares "with the pass" against "with the pass neutered"; neither
asserts a coordinate, so a legitimate layout change moves both sides together
and only genuine inertness reds the test.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEDS = REPO_ROOT / "tests" / "fixtures" / "hash_seed_determinism"
TOPOLOGIES = REPO_ROOT / "examples" / "topologies"


def _settled_offsets(path: Path) -> dict[tuple[str, str], float]:
    """Every line's settled lane offset, laid out without the closing guards."""
    graph: MetroGraph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    return dict(compute_station_offsets(graph))


def _without(name: str) -> Callable[[Path], dict[tuple[str, str], float]]:
    def measure(path: Path) -> dict[tuple[str, str], float]:
        original = getattr(offsets_module, name)
        setattr(offsets_module, name, lambda *args, **kwargs: None)
        try:
            return _settled_offsets(path)
        finally:
            setattr(offsets_module, name, original)

    return measure


@pytest.mark.parametrize(
    ("pass_name", "path"),
    [
        ("_exchange_pair_beyond_exit", SEEDS / "seed_41.mmd"),
        ("_exchange_pair_beyond_exit", SEEDS / "seed_77.mmd"),
        (
            "_restore_fanout_peel_order",
            TOPOLOGIES / "bottom_entry_fan_side_targets.mmd",
        ),
    ],
    ids=lambda value: (
        value.stem if isinstance(value, Path) else value.removeprefix("_")
    ),
)
def test_offset_repair_pass_changes_its_witness(pass_name: str, path: Path) -> None:
    """Neutering the named pass must move the named fixture's lane offsets."""
    assert _settled_offsets(path) != _without(pass_name)(path), (
        f"{pass_name} left {path.name} unchanged; either the pass has gone inert "
        "or this fixture no longer reaches it"
    )
