"""Completeness checks for the Lifecycle annotations in CONTRACT.md.

Every stage block in the CONTRACT stage table carries a ``**Lifecycle:**``
line tagging the stage ``invariant`` (the property it establishes still
holds at the final layout boundary) or ``transient`` (a later stage
deliberately overrides it). These tests pin that contract so a newly
documented stage cannot be added without a Lifecycle tag, and so a
``transient`` tag always names the superseding stage.

The tag answers the objective "holds at the final boundary?" question
(issue #462). It is intentionally distinct from the orthogonal "is this
safe to lift into a run-anytime ``maintain()`` registry?" question
explored in #365; that distinction is carried inline by an optional
``liftable:`` qualifier, which these tests do not require.
"""

import re
from pathlib import Path

import pytest

CONTRACT = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "nf_metro"
    / "layout"
    / "CONTRACT.md"
)

_STAGE_HEADING = re.compile(r"^### Stage (\S+?):", re.MULTILINE)
# Match the Lifecycle bullet and any wrapped continuation lines (indented
# two or more spaces), so a superseding-stage reference that wraps onto a
# later line is still captured.
_LIFECYCLE = re.compile(
    r"^- \*\*Lifecycle:\*\*\s+(invariant|transient)\b((?:.*\n(?:  .*\n)*)|.*)",
    re.MULTILINE,
)


def _stage_blocks():
    """Yield (stage_tag, block_text) for each ### Stage entry in the table."""
    text = CONTRACT.read_text()
    matches = list(_STAGE_HEADING.finditer(text))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield m.group(1), text[start:end]


STAGE_BLOCKS = list(_stage_blocks())
STAGE_IDS = [tag for tag, _ in STAGE_BLOCKS]


def test_contract_has_stage_table():
    """Guard against the parser silently matching nothing."""
    assert len(STAGE_BLOCKS) >= 20, (
        f"expected the full stage table, found {len(STAGE_BLOCKS)} stages"
    )
    # Spot-check the table spans Pass-A construction through final Pass-C.
    assert "1.1" in STAGE_IDS
    assert "6.16" in STAGE_IDS


@pytest.mark.parametrize("tag,block", STAGE_BLOCKS, ids=STAGE_IDS)
def test_every_stage_has_lifecycle_tag(tag, block):
    """Each stage block declares exactly one valid Lifecycle tag."""
    found = _LIFECYCLE.findall(block)
    assert len(found) == 1, (
        f"Stage {tag} must have exactly one "
        f"'- **Lifecycle:** invariant|transient ...' line, found {len(found)}"
    )


@pytest.mark.parametrize("tag,block", STAGE_BLOCKS, ids=STAGE_IDS)
def test_transient_stages_name_superseding_stage(tag, block):
    """A transient tag must point at the stage that overrides it."""
    found = _LIFECYCLE.findall(block)
    if not found:
        pytest.skip(f"Stage {tag} has no Lifecycle tag (caught elsewhere)")
    kind, rest = found[0]
    if kind != "transient":
        pytest.skip(f"Stage {tag} is invariant")
    assert "Stage" in rest, (
        f"Stage {tag} is transient but its Lifecycle line names no "
        f"superseding stage: {rest!r}"
    )
