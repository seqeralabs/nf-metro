# Inter-section dispatch table

Inter-section edges (port/junction to port/junction) are routed by
`_route_inter_section` in
[`routing/inter_section_handlers.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/routing/inter_section_handlers.py).
It chooses the route's shape from a **declarative table**, `_INTER_SECTION_RULES`,
rather than a hand-written if-ladder: one `_InterFacts` snapshot of the edge is
matched against an ordered list of named rules, and the first whose predicate
holds owns the route.

Every rule's handler builds its route from a centreline through the
[bundle builder](routing.md) (`build_concentric_bundle` /
`build_tapered_bundle`), so no handler assembles per-line points or corner
radii by hand. The runtime curve guard (below) is a backstop, not the
mechanism that keeps the routes correct.

## The fact space

`_InterFacts` resolves the geometry and topology each rule keys on:

- **Relative position** — the source and target grid columns and rows
  (`src_col/row`, `tgt_col/row`), and the derived `same_y`, `same_x`,
  `same_col`, `cross_row`, and `needs_bypass` (a multi-column hop with an
  intervening section in the source *or* target row).
- **Exit side** — whether the source is a LEFT/RIGHT exit port, a TOP/BOTTOM
  perpendicular exit (`is_perp_exit`), a TB BOTTOM exit (`is_tb_bottom_exit`),
  or a junction.
- **Entry side** — `entry_side` is the target entry port's side
  (LEFT/RIGHT/TOP/BOTTOM) or `None` when the target is a junction; `merge_ep`
  is the resolved entry-port station when the target is a merge junction.

## The rules, in order

The table is ordered: earlier rules shadow later ones, so the order encodes the
precedence between overlapping cases. The first matching rule wins; if none
match, the **standard L-shape** is the fall-through.

| # | Rule | Fires when | Route |
|---|------|-----------|-------|
| 1 | `perp-exit` | source is a TOP/BOTTOM exit on a horizontal-flow section | `_route_perp_exit` (column-aligned drop, else up-and-over). Before the same-Y rule: an exit and entry sharing an edge Y would graze both boxes on a straight run. |
| 2 | `same-Y straight` | `same_y`, not `needs_bypass`, not a right-entry plough | straight horizontal |
| 3 | `TB bottom exit` | BOTTOM exit on a TB/BT section, with station offsets | `_route_tb_bottom_exit` |
| 4 | `TOP entry L-shape` | `entry_side is TOP` | `_route_top_entry_l_shape`. Before the same-X rule, which would drop straight in with no horizontal lead-in. |
| 5 | `same-X vertical drop` | `same_x` | straight vertical |
| 6 | `bottom-exit junction` | source is a bottom-exit junction | `_route_bottom_exit_junction` |
| 7 | `bypass family` | `needs_bypass` | `_route_bypass_family`: merge trunk/branch, LEFT-entry-one-row-below straight drop, RIGHT-entry wrap (gap-above or around-below), else the U-shaped bypass |
| 8 | `near-vertical same-col junction` | junction dropping almost straight into a same-column entry | `_route_near_vertical_junction` |
| 9 | `RIGHT entry wrap` | `entry_side is RIGHT` and travelling right | `_route_right_entry_wrap` (over the top / around the right side) |
| 10 | `LEFT entry wrap family` | `entry_side is LEFT`, `dx < 0`, `cross_row` | `_route_left_entry_family`: inter-row gap wrap, or the corridor / around-below loop when that gap crosses a section |
| 11 | `serpentine LEFT exit -> LEFT entry` | LEFT exit into a LEFT entry stacked in the same column | `_route_left_exit_left_entry_drop` |
| 12 | `merge entry family` | `merge_ep is not None` | `_route_merge_entry_family`: straight (near-collinear), corridor / around-below (LEFT entry crossing a section), else L-shape into the entry port |
| 13 | `RIGHT entry plough -> bypass` | a higher-row L-shape to a RIGHT entry that would plough an intervening same-row section | `_route_bypass` |
| — | *fall-through* | no rule matched | `_route_l_shape` |

The three rules whose handlers carry their own residual decisions (7, 10, 12)
own that logic inside the named handler, so the top-level table stays a single
declarative pass.

## The curve guard is a backstop

`assert_render_curve_invariants`
([`routing/invariants.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/routing/invariants.py))
runs on every render and the stage-boundary checks run under `validate=True`.
Because every rule above builds its route through the centreline bundle builder
— which makes a flipped, pinched, or collinear bundle impossible by
construction — these checks are a thin safety net. In normal operation they
never fire; a failure means a genuinely new, un-tabled shape reached the
renderer built some other way, and the fix is to route it through the builder
too, not to relax the guard.
