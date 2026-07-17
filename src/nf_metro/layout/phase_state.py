"""The inter-phase state protocol: declared ``graph._*`` channels.

The section-layout pipeline (``layout/engine.py``) is a long sequence of
numbered stages that hand intermediate results to each other through private
fields stashed on the :class:`~nf_metro.parser.model.MetroGraph`.  Left as bare
attribute pokes, those fields form an undocumented protocol that only holds
while stage ordering never changes: a reader stage silently sees a stale or
default value if its writer stage is reordered away or skipped.

This module makes that protocol explicit data:

* :data:`CANONICAL_STAGE_ORDER` is the single ordered list of section-layout
  stage ids (mirrored by the engine's ``_snap`` checkpoints).
* :data:`PHASE_FIELD_REGISTRY` declares every inter-phase field with its writer
  stage, the stages that read it, and *why* it exists.
* :func:`require_phase_field`, called just before a read, enforces
  write-before-read for the fields whose reader genuinely depends on the writer
  having run, raising :class:`PhaseInvariantError` under ``validate=True`` --
  generalising the hand-written ``graph._consumers_grid_snapped`` guard.

The registry is kept in sync with the dataclass fields, the engine stage list,
and ``CONTRACT.md`` by ``tests/test_phase_state_registry.py``, so a new bare
poke or a drifted document reds CI rather than rotting silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph

# Lifecycle-phase markers for fields whose writer or reader is not one of the
# numbered section-layout stages.  They stand in for a stage id in a
# ``PhaseFieldSpec`` so a channel that crosses a subsystem boundary (parse ->
# routing, rail-mode layout, the bypass-pass control wrapper) is declarable as
# data alongside the inter-stage channels.

# Populated before Stage 1.1: parse/resolve rewrites or the spacing resolution
# in ``compute_layout``, before the stage pipeline begins.
PRE_LAYOUT = "pre-layout"
# Written or read after the stage pipeline has positioned every station: by
# routing, render, or the closing validate guards.
POST_LAYOUT = "post-layout"
# Owned by the opt-in rail-mode layout (``layout/rail_mode.py``), a
# self-contained path that runs instead of the numbered section pipeline.
RAIL_LAYOUT = "rail-layout"

# Non-stage phases a writer_stage / reader_stage may name in place of a
# CANONICAL_STAGE_ORDER id.
NON_STAGE_PHASES: tuple[str, ...] = (PRE_LAYOUT, POST_LAYOUT, RAIL_LAYOUT)

# Section-layout stage ids in execution order.  Mirrors the ``_snap(graph, ...)``
# checkpoints in ``engine._compute_section_layout`` and its Pass C helpers;
# ``test_phase_state_registry`` asserts the two stay identical.  Note ``6.15a``
# runs before ``6.15``.
CANONICAL_STAGE_ORDER: tuple[str, ...] = (
    "1.1", "1.2", "1.3", "1.4", "1.5",
    "2.1",
    "3.1", "3.2", "3.3", "3.4", "3.5",
    "4.1", "4.2", "4.3", "4.4", "4.5", "4.6", "4.7", "4.8", "4.9", "4.10",
    "5.1", "5.2", "5.3", "5.4", "5.5",
    "6.1", "6.2", "6.3", "6.4", "6.5", "6.6", "6.7", "6.8", "6.9", "6.10",
    "6.11", "6.12", "6.13", "6.14", "6.15a", "6.15", "6.16", "6.17",
)  # fmt: skip


class FieldEnforcement(Enum):
    """How strongly :func:`require_phase_field` polices a declared field.

    ``REQUIRE_WRITER`` -- the reader trusts data the writer stage produces, so
    reading before that stage has run is a bug; raise under ``validate=True``.

    ``FALLBACK`` -- the read site is designed to tolerate the unwritten
    (default/empty/``None``) value, so an early or absent read is harmless.  The
    field is registered for documentation and drift-detection only; no runtime
    check fires.
    """

    REQUIRE_WRITER = "require_writer"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class PhaseFieldSpec:
    """One inter-phase ``graph._*`` field, declared as data.

    ``writer_stage`` / ``reader_stages`` are ids from :data:`CANONICAL_STAGE_ORDER`
    (``writer_stage`` may also be :data:`PRE_LAYOUT`).  ``run_condition_attr``, when
    set, names a truthy ``MetroGraph`` attribute that gates whether the writer
    stage runs at all; a ``REQUIRE_WRITER`` check is skipped when it is falsy, so a
    conditionally-skipped writer does not trip a false positive.
    """

    name: str
    writer_stage: str
    reader_stages: tuple[str, ...]
    enforcement: FieldEnforcement
    why: str
    run_condition_attr: str | None = None


PHASE_FIELD_REGISTRY: dict[str, PhaseFieldSpec] = {
    "_row_y_grid_info": PhaseFieldSpec(
        name="_row_y_grid_info",
        writer_stage="1.2",
        reader_stages=("4.2", "6.3", "6.4"),
        enforcement=FieldEnforcement.REQUIRE_WRITER,
        why=(
            "row-grid metadata from Stage 1.2's _align_row_y_grids; the grid-group "
            "port snap, fan re-centre, and grid snap read it to group same-row "
            "sections onto a shared pitch"
        ),
    ),
    "half_grid_station_ids": PhaseFieldSpec(
        name="half_grid_station_ids",
        writer_stage="6.3",
        reader_stages=("6.4",),
        enforcement=FieldEnforcement.REQUIRE_WRITER,
        why=(
            "2-branch symfan stations Stage 6.3 places at half-pitch; Stage 6.4's "
            "grid snap must skip them or it snaps their intentional half-grid Y to "
            "the full grid"
        ),
        run_condition_attr="center_ports",
    ),
    "symfan_trunk_station_ids": PhaseFieldSpec(
        name="symfan_trunk_station_ids",
        writer_stage="6.3",
        reader_stages=("6.4",),
        enforcement=FieldEnforcement.REQUIRE_WRITER,
        why=(
            "source/trunk stations of a 2-branch symfan Stage 6.3 leaves on the "
            "section's local frame; Stage 6.4's grid snap must skip them or it "
            "drags them onto a rowspan neighbour's fractional row-grid origin"
        ),
        run_condition_attr="center_ports",
    ),
    "_consumers_grid_snapped": PhaseFieldSpec(
        name="_consumers_grid_snapped",
        writer_stage="6.4",
        reader_stages=("6.6",),
        enforcement=FieldEnforcement.REQUIRE_WRITER,
        why=(
            "readiness flag set right after the Stage 6.4 snap so the off-track "
            "reanchor re-pins against final consumer Ys; _reanchor_off_track_to_"
            "consumer carries its own always-on guard on this field"
        ),
    ),
    "_struct_height_below_top": PhaseFieldSpec(
        name="_struct_height_below_top",
        writer_stage="6.15a",
        reader_stages=("6.13",),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "settled structural extents snapshotted after Stage 6.15a; the Stage "
            "6.13 inter-row cascade reads it for fidelity checks and falls back to "
            "live bbox heights when the snapshot is empty (default before 6.15a)"
        ),
    ),
    "_placement_ref_y": PhaseFieldSpec(
        name="_placement_ref_y",
        writer_stage="6.1",
        reader_stages=("6.1", "6.2", "6.11"),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "station Ys frozen by _snapshot_placement_refs before Stages 6.1/6.2 "
            "(re-taken before 6.11); _ref_y reads it and falls back to the live Y "
            "for any station the snapshot does not cover"
        ),
    ),
    "_placement_ref_bbox_top": PhaseFieldSpec(
        name="_placement_ref_bbox_top",
        writer_stage="6.1",
        reader_stages=("6.1", "6.2", "6.11"),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "section bbox tops frozen alongside _placement_ref_y; _ref_bbox_top "
            "reads it and falls back to the live top for any section the snapshot "
            "does not cover"
        ),
    ),
    "_base_y_spacing": PhaseFieldSpec(
        name="_base_y_spacing",
        writer_stage=PRE_LAYOUT,
        reader_stages=("5.2", "6.6"),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "content pitch recorded before the spread loop widens y_spacing, and "
            "only when y_spacing is auto-resolved; single-trunk off-track lift and "
            "rail layout read it with a None/getattr fallback when it is unset"
        ),
    ),
    "_resolved_x_spacing": PhaseFieldSpec(
        name="_resolved_x_spacing",
        writer_stage=PRE_LAYOUT,
        reader_stages=("5.2", "6.6"),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "resolved column pitch recorded before layout; a vertical-flow (TB/BT) "
            "section's off-track band is offset by this cross-axis pitch, read with "
            "a None/getattr fallback to X_SPACING when it is unset"
        ),
    ),
    "_cross_column_perp_bridges": PhaseFieldSpec(
        name="_cross_column_perp_bridges",
        writer_stage="3.4",
        reader_stages=(POST_LAYOUT,),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "section IDs whose perpendicular drop had to be bridged across grid "
            "columns, accumulated by the Stage 3.2 entry-port and Stage 3.4 "
            "exit-port alignment; routing's render-curve invariant reads the set "
            "to relax its hard abort to a warning for those forced-perpendicular "
            "bundles, tolerating the empty default"
        ),
    ),
    "_fold_compressed_sections": PhaseFieldSpec(
        name="_fold_compressed_sections",
        writer_stage=PRE_LAYOUT,
        reader_stages=("1.1", POST_LAYOUT),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "sections a lowered fold threshold relocated onto a return row, "
            "recorded at parse time; the fold-exit-side guard at the Stage 1.1 "
            "checkpoint and the render fold-abort chokepoint read the set and "
            "tolerate its empty default when no fold compression occurred"
        ),
    ),
    "_fold_reoriented_sections": PhaseFieldSpec(
        name="_fold_reoriented_sections",
        writer_stage=PRE_LAYOUT,
        reader_stages=(POST_LAYOUT,),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "sections whose flow direction resolve.py flipped to keep a flow-axis "
            "port on its consumer/producer end; routing's exit-port offset reads "
            "the set to anchor on the feeder-bundle frame, tolerating the empty "
            "default"
        ),
    ),
    "_rail_y": PhaseFieldSpec(
        name="_rail_y",
        writer_stage=RAIL_LAYOUT,
        reader_stages=(POST_LAYOUT,),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "per-section {line_id: rail_y} map produced by the opt-in rail-mode "
            "layout; the rail router, label placement, and rail guards read it "
            "and fall back to the empty default when rail mode is off"
        ),
    ),
    "_defer_final_guards": PhaseFieldSpec(
        name="_defer_final_guards",
        writer_stage=PRE_LAYOUT,
        reader_stages=(POST_LAYOUT,),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "pass-control flag compute_layout sets while the pre-bypass passes "
            "run so the final-geometry guards defer on transient state; the "
            "breeze-through and after-final guards read it, tolerating the False "
            "default"
        ),
    ),
    "_after_final_deferred": PhaseFieldSpec(
        name="_after_final_deferred",
        writer_stage=POST_LAYOUT,
        reader_stages=(POST_LAYOUT,),
        enforcement=FieldEnforcement.FALLBACK,
        why=(
            "flag the after-final checkpoint sets when it is reached but deferred, "
            "so compute_layout runs the checkpoint once on the settled post-bypass "
            "geometry; read by compute_layout, tolerating the False default"
        ),
    ),
}


def require_phase_field(graph: "MetroGraph", name: str) -> None:
    """Assert ``graph.<name>`` is safe to read, enforcing write-before-read.

    Call this immediately before reading a declared field.  For a
    :attr:`FieldEnforcement.REQUIRE_WRITER` field, raises
    :class:`PhaseInvariantError` when the field's writer stage has not yet run in
    the current pass -- but only while ``graph._validate_active`` is set, and only
    when the field's ``run_condition_attr`` (if any) is truthy.  On the production
    (``validate=False``) path this is a registry lookup plus a flag check.
    """
    spec = PHASE_FIELD_REGISTRY[name]
    if (
        spec.enforcement is FieldEnforcement.REQUIRE_WRITER
        and getattr(graph, "_validate_active", False)
        and (
            spec.run_condition_attr is None
            or getattr(graph, spec.run_condition_attr, False)
        )
        and spec.writer_stage not in graph._stages_completed
    ):
        # Local import: PhaseInvariantError lives in phases.guards, which imports
        # heavily from the layout package; importing it at module load would risk
        # a cycle.
        from nf_metro.layout.phases.guards import PhaseInvariantError

        raise PhaseInvariantError(
            f"{name} read before its writer Stage {spec.writer_stage} ran "
            f"(completed stages: {graph._stages_completed}); {spec.why}"
        )
