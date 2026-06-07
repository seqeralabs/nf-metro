# Parser

The parser reads a Mermaid `graph LR` definition plus `%%metro`
directives and produces a `MetroGraph`.  The entry point is
`parse_metro_mermaid` in
[`src/nf_metro/parser/mermaid.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/parser/mermaid.py);
the data model is in
[`src/nf_metro/parser/model.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/parser/model.py).

## Input format

`.mmd` files use a subset of Mermaid `graph LR` syntax:

```
%%metro title: Pipeline Name
%%metro line: line_id | Display Name | #hexcolor | style

graph LR
    subgraph section_id [Section Name]
        %%metro entry: left | line1, line2
        %%metro exit: right | line1, line2
        node_id[Label]
        node_id -->|line_id| other_node
    end
    %% Inter-section edges live outside subgraphs
    node_a -->|line_id| node_b
```

- A Mermaid `subgraph` becomes a `Section`.
- A node (`node_id[Label]`) becomes a `Station`.
- An edge (`a -->|line1,line2| b`) becomes one `Edge` per line id; the
  pipe-delimited label lists the lines the edge carries.
- Lines must be declared with `%%metro line:` before use; an undeclared
  line id on an edge raises a parse error.
- A primary graph direction other than `LR` is warned about
  (`_warn_if_non_lr_primary`); per-section flow is controlled by
  `%%metro direction:` and is independent of the header.

## Directive model

Every `%%metro` line is dispatched by `_parse_directive`.  The directives
it recognises:

| Directive | Effect |
| --- | --- |
| `title:` / `style:` | graph title and theme name |
| `line: id \| name \| #color \| style` | declare a `MetroLine` (style is `solid` / `dashed` / `dotted`) |
| `line_order:` | `definition` or `span` line ordering |
| `entry:` / `exit:` (inside a subgraph) | stored as port **hints** on the section |
| `direction:` | section flow `LR` / `RL` / `TB` |
| `grid:` | manual section grid placement |
| `compact_offsets:` / `center_ports:` | bundle layout toggles |
| `line_spread:` | how shared lines relate vertically (`bundle` / `centered` / `rails`), graph-wide or per-section |
| `fold_threshold:` | station count at which long chains wrap into serpentine rows |
| `off_track:` | mark stations to lift above the section's top track |
| `label_angle:` | diagonal station-label angle |
| `legend:` / `legend_min_height:` / `legend_combo:` / `legend_logo_gap:` | legend block |
| `logo:` / `logo_scale:` | logo path and scaling |
| `font_scale:` | global font scaling |
| `group:` | annotative caption spanning stations |
| `marker:` / `marker_legend:` | per-station marker shape/fill styling and its legend caption |
| `file:` / `files:` / `dir:` | terminus file-icon designation |

Note that `entry:` / `exit:` do **not** create `Port` objects at parse
time.  `_parse_port_hint` records them as `entry_hints` / `exit_hints`
(a `(side, [line_ids])` list) on the `Section`; the actual ports are
created later, driven by real inter-section edges.

## Parse-then-resolve flow

`parse_metro_mermaid` scans the input line by line, then runs a
post-parse sequence (only when the graph has sections):

1. `_validate_edge_annotations` - reject malformed edges.
2. `_remove_empty_sections` and `_create_implicit_section` - drop empty
   subgraphs and wrap loose (section-less) stations in an implicit,
   invisible section.
3. `infer_section_layout` (from `layout/auto_layout.py`) - infer missing
   grid positions, section directions, and port sides from the section
   DAG, preserving anything set explicitly by directives.
4. `_insert_terminus_convergence_stations`.
5. `_resolve_sections` - the core rewrite (below).
6. `_insert_bypass_stations`.

Finally the parser applies pending terminus icons and `off_track` marks
that were buffered during the line scan.

### `_resolve_sections`

`_resolve_sections` rewrites inter-section edges into port/junction
chains.  It is split into three helpers:

- `_build_entry_side_mapping` - per-line entry-side lookup from the
  `entry_hints`.  A section gets **one** entry side: if all hints agree,
  that side is used; otherwise they collapse to the natural entry for the
  section direction (LEFT for LR, RIGHT for RL, TOP for TB).
- `_classify_edges` - split edges into internal (both endpoints in one
  section) and inter-section, and populate each section's
  `internal_edges`.
- `_create_ports_and_junctions` - create `Port` objects and rewrite each
  inter-section edge into a chain: `source -> exit_port -> entry_port ->
  target`.  The design rule is **one exit port per source section**
  (all lines leave together for consistent ordering) and **one entry
  port per target section per side**; junction stations handle fan-out
  to multiple target sections.

After ports and junctions exist, `_insert_merge_junctions` adds merge
junctions and `_assign_section_numbers` numbers any unnumbered sections.

The result is a `MetroGraph` whose edges all live within a section or
run port-to-port, ready for the layout stage.
