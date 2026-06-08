# Parser

A walkthrough of how nf-metro turns `.mmd` text into a `MetroGraph`. If
you're adding a node shape, a `%%metro` directive, or a new statement
form - or just trying to understand the grammar - start here.

The entry point is `parse_metro_mermaid` in
[`src/nf_metro/parser/mermaid.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/parser/mermaid.py);
the data model it builds is in
[`src/nf_metro/parser/model.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/parser/model.py).
The parser package is split by job: `grammar.py` (grammar, statement types,
transformer), `directives.py` (directive parsing and dispatch), `resolve.py`
(the post-parse graph rewrites), and `mermaid.py` (the public entry point and
statement-application driver).
Parsing is the first of the three stages (**Parse -> Layout -> Render**);
the [layout pipeline](layout_pipeline.md) takes over from the `MetroGraph`
this stage produces.

## What "parsing" means here

The input is a subset of Mermaid `graph LR` syntax plus `%%metro`
directives. Parsing reads that text and produces a `MetroGraph` of
sections, stations, edges, lines, and ports - with no coordinates yet
(those are the layout stage's job).

The work splits in two:

1. **Front-end** - recognise each line's shape (a node? an edge? a
   directive?) and pull out its pieces. The grammar recognises statement
   *shapes and boundaries*; directive *payloads* and graph *semantics* are
   handled by Python (see [Directives](#directives) and
   [Parse-then-resolve flow](#parse-then-resolve-flow)).
2. **Post-parse** - rewrite the raw graph into the form layout expects:
   resolve inter-section edges into ports and junctions, insert bypass
   and convergence stations, create the implicit section for loose
   nodes. These are plain functions operating on the model, covered in
   [Parse-then-resolve flow](#parse-then-resolve-flow) below.

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
  pipe-delimited label lists the lines the edge carries. An endpoint may
  also be written with an inline shape (`a[A] -->|line1| b[B]`), which
  declares that node's label as well as the edge.
- Lines must be declared with `%%metro line:` before use; an edge with no
  line annotation, or one naming an undeclared line, is rejected as a
  semantic error (see [Leniency and error policy](#leniency-and-error-policy)).
- A primary graph direction other than `LR` is warned about
  (`_warn_if_non_lr_primary`); per-section flow is controlled by
  `%%metro direction:` and is independent of the header.

## What a grammar is, and why we use one

The naive way to read a line-oriented format is to write the
*instructions* for recognising each line: "does this line start with
`graph`? else is it a `subgraph`? else does it contain an arrow? else
try these six node-shape patterns in this exact order...". That works,
but the order of the checks becomes load-bearing, and every new shape or
directive means finding the right slot in the chain.

A **grammar** flips this around: instead of writing the recognising
instructions, you write down *the rules of what a valid line is* and let
a library do the recognising. nf-metro uses [`lark`](https://lark-parser.readthedocs.io/)
for this. The grammar lives as a string in `grammar.py` (`_GRAMMAR`),
and reads roughly:

```lark
node:  NAME SHAPE?
edge:  NAME SHAPE? ARROW EDGELABEL? NAME SHAPE?

SHAPE: /\(\[...\]\)|\[\[...\]\]|\(\(...\)\)|\[...\]|\(...\)|\{...\}/
ARROW: /-->|---|==>/
NAME:  /[a-zA-Z_][a-zA-Z0-9_]*/
```

An edge endpoint may carry an inline shape: `x[X] -->|a| y[Y]` declares
node `x` with label "X", node `y` with label "Y", and the edge between
them, all in one line. The `SHAPE` terminal's inner pattern
(`_SHAPE_INNER`) excludes the arrow sequences so a source shape like
`[X]` stops at the arrow rather than greedily swallowing it.

You describe the shapes; lark works out how to match them. Adding a node
shape is one more alternative in the `SHAPE` rule, not a new regex
slotted into a hand-ordered chain, and the order of the rules stops
mattering for correctness.

It helps to think of it like describing the structure of a sentence
("a sentence is a subject, a verb, then an object") rather than writing
out, character by character, how to scan one.

## From text to `MetroGraph`

Three steps:

1. **Parse.** `_PARSER.parse(text)` turns the whole document into a
   parse tree using the grammar.
2. **Transform.** `_StatementTransformer` walks that tree and flattens
   it into an ordered list of **typed statements** - one small dataclass
   per source line, from a fixed union: `_GraphHeader`, `_Subgraph`,
   `_Directive`, `_Node`, `_Edge`, `_End`, `_Comment`, and `_Junk`. The
   transformer does the normalisation here, so each statement carries
   structured fields rather than raw tokens: `_Subgraph` already splits
   the section id from its display name, `_Directive` splits the body on
   the first colon into `key`/`value`, and `_Edge` carries its line ids
   as a list plus any inline endpoint labels.
3. **Drive.** `parse_metro_mermaid` iterates those statements *in source
   order* and dispatches each by `isinstance`, applying it to the graph
   while tracking which `subgraph` it's currently inside
   (`current_section_id`). A `_Subgraph` opens a section, an `_End`
   closes it, and the nodes/edges/directives in between are attached to
   it.

Because the appliers receive structured fields, the driver stays a thin
dispatch: a `_Node` registers a station, an `_Edge` registers one edge
per line id (declaring any inline-shaped endpoints), a `_Directive` is
routed to a handler, and so on.

Keeping the driver a simple in-order loop is deliberate: source order
matters (e.g. dictionary insertion order of stations affects downstream
layout), so the grammar handles *recognising* lines while the driver
handles *applying* them in sequence.

### One simplification worth knowing

The model records only a node's **id and label** - never which shape it
was drawn as. So all six Mermaid shapes collapse to a single `SHAPE`
terminal plus a tiny "strip the delimiters" helper (`_shape_label`),
instead of six separate regexes that each had to be tried in the right
order.

## Directives

The `%%metro` directive *bodies* are not described by the grammar - they
keep their own handler functions, because a grammar can't express
behaviour like "warn about this and ignore it", which several directives
need. What the grammar gives us is the directive line as a unit; the
transformer splits its body once on the first colon into a **key** and a
**value** (a `%%metro` line with no colon becomes a `_Comment` and is
ignored).

Dispatch on the key happens in `_apply_directive`. Most directives are
graph-wide and live in the `_GLOBAL_DIRECTIVE_HANDLERS` dict, keyed by **exact
name** and mapping to a `(value, graph) -> None` handler. Exact-key
lookup means handler order is irrelevant and a key that is a prefix of
another (`legend` vs `legend_combo`, `logo` vs `logo_scale`) cannot
shadow it. Three families are dispatched separately because they need
more than the value alone: `entry` / `exit` / `direction` need the
enclosing section, and the icon keys `file` / `files` / `dir` need the
key itself to choose the icon type. A key matching none of these is
ignored with a `UserWarning`.

The directives `_apply_directive` recognises:

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
time. `_parse_port_hint` records them as `entry_hints` / `exit_hints`
(a `(side, [line_ids])` list) on the `Section`; the actual ports are
created later, driven by real inter-section edges.

## Leniency and error policy

The split is deliberate: the **grammar/parse layer is lenient** about
syntax it doesn't recognise (it warns rather than crashing), while
**semantic validity is a separate, stricter phase**. A typo in a node
line should not abort a render of an otherwise-fine diagram, but an edge
that names no metro line is a real modelling error.

| Input | Outcome |
| --- | --- |
| Blank line, `%%` comment, or `%%metro` line with no colon | ignored silently |
| Unrecognised non-blank line (the grammar `junk` rule) | dropped, with a `UserWarning` ("Ignored unrecognised line: ...") |
| Unknown `%%metro` directive key | ignored, with a `UserWarning` ("Ignored unknown %%metro directive: ...") |
| Malformed directive *payload* (too few `\|` fields, an unusable enum/number/bool, a section-scoped directive outside a subgraph) | warned about and ignored, uniformly across handlers (`_warn_directive`) |
| Foreign/unsupported syntax (Mermaid `flowchart`) | raises `ValueError` with guidance, via `_check_unsupported_input`, before the grammar runs |
| Edge with no line annotation, or an undeclared line id | raised by `_validate_edge_annotations` after parsing |

Broader graph-semantic checks (beyond edge annotations) live in the
separate `validate` phase, `nf_metro.parser.validate.validate_graph`.

The unrecognised-line case is handled in the grammar by a low-priority
catch-all:

```lark
JUNK.-10: /[^\n]+/
```

`JUNK` matches any line, but its negative priority means it only wins
when nothing more specific does. The transformer turns the match into a
`_Junk` statement, and the driver warns when it applies one.

This `junk` fallback is why the parser is configured as
`Lark(..., parser="earley", lexer="dynamic")` rather than the faster
`lalr`. A line that *begins* like a valid statement but then hits an
unexpected token must be able to fall back to `junk` and be dropped. An
earley parser can explore that fallback; a committing `lalr` parser
cannot backtrack a partly-matched line, so it would turn such a line
into a fatal error instead. **Do not switch the parser to `lalr`**
without accepting that behaviour change. The parse runs once per render,
so earley's extra cost over `lalr` is not worth optimising away.

## Parse-then-resolve flow

After the grammar parse and statement application, `parse_metro_mermaid`
runs a post-parse sequence (only when the graph has sections):

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

Finally the parser applies pending terminus icons, `off_track` marks,
and per-station markers that were buffered during the statement scan.

### `_resolve_sections`

`_resolve_sections` rewrites inter-section edges into port/junction
chains. It is split into three helpers:

- `_build_entry_side_mapping` - per-line entry-side lookup from the
  `entry_hints`. A section gets **one** entry side: if all hints agree,
  that side is used; otherwise they collapse to the natural entry for the
  section direction (LEFT for LR, RIGHT for RL, TOP for TB).
- `_classify_edges` - split edges into internal (both endpoints in one
  section) and inter-section, and populate each section's
  `internal_edges`.
- `_create_ports_and_junctions` - create `Port` objects and rewrite each
  inter-section edge into a chain: `source -> exit_port -> entry_port ->
  target`. The design rule is **one exit port per source section** (all
  lines leave together for consistent ordering) and **one entry port per
  target section per side**; junction stations handle fan-out to multiple
  target sections.

After ports and junctions exist, `_insert_merge_junctions` adds merge
junctions and `_assign_section_numbers` numbers any unnumbered sections.

The result is a `MetroGraph` whose edges all live within a section or
run port-to-port, ready for the layout stage.

## Adding things

- **A node shape** - add one alternative to the `SHAPE` terminal in
  `_GRAMMAR`; if its delimiters are two characters per side, add the
  opener to `two_char_opens` in `_shape_label`.
- **A `%%metro` directive** - write a `(value, graph) -> None` handler
  and add an entry to the `_GLOBAL_DIRECTIVE_HANDLERS` dict keyed by the exact
  directive name. No ordering concerns. (A directive that needs the
  enclosing section or the key itself is dispatched in `_apply_directive`
  instead of the dict.) On an unusable payload, call `_warn_malformed`
  (or `_warn_directive` for a more specific message) and return, rather
  than failing silently - that is the leniency policy above.
- **A new statement form** - add a rule and its terminal to `_GRAMMAR`,
  a typed statement dataclass (added to the `_Statement` union), a method
  to `_StatementTransformer` returning that dataclass, and an `isinstance`
  branch to the driver loop in `parse_metro_mermaid`.

## How equivalence is verified

The grammar must produce exactly the same `MetroGraph` as the input
implies, so changes here are checked by **comparing parsed model objects**
(not rendered SVGs): parse every fixture in `tests/fixtures/`,
`examples/`, and `examples/topologies/` and assert the model matches.
Because identical models render identically, this keeps the gallery
byte-identical without chasing sub-pixel render drift. The grammar's
coverage and behaviour are pinned by `tests/test_parser_grammar.py`.
