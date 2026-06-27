"""Parsing and dispatch of ``%%metro`` directives.

Each directive maps a ``key: value`` line onto the :class:`MetroGraph` (or the
enclosing section). Graph-wide directives are routed through a registry; the
section-scoped (entry/exit/direction) and file/files/dir icon directives need
extra context and are handled by :func:`_apply_directive`.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from typing import Literal

from nf_metro.options import INVALID, LAYOUT_OPTIONS, LayoutOption, coerce
from nf_metro.parser.grammar import _split_csv, _unquote
from nf_metro.parser.model import (
    MARKER_FILL_OPEN,
    MARKER_FILL_SOLID,
    MARKER_SHAPE_CIRCLE,
    VALID_ICON_TYPES,
    VALID_LINE_STYLES,
    VALID_MARKER_SHAPES,
    Interchange,
    LineSpread,
    MarkerLegendEntry,
    MarkerStyle,
    MetroGraph,
    MetroLine,
    PortSide,
    StationGroup,
)


def _warn_directive(key: str, message: str) -> None:
    """Warn about a %%metro directive; every directive warning uses this."""
    warnings.warn(f"%%metro {key}: {message}", stacklevel=2)


def _warn_malformed(key: str, value: str, expected: str) -> None:
    """Warn that a directive's payload is unusable and is being ignored."""
    _warn_directive(key, f"ignoring {value!r}; expected {expected}")


def _split_fields(value: str) -> list[str]:
    """Split a ``|``-delimited directive body into stripped fields.

    Empty fields are kept (so positional access by index stays valid); only the
    surrounding whitespace of each field is removed.
    """
    return [field.strip() for field in value.split("|")]


def _dir_title(value: str, graph: MetroGraph) -> None:
    graph.title = value


def _dir_style(value: str, graph: MetroGraph) -> None:
    graph.style = value


def _dir_mode(value: str, graph: MetroGraph) -> None:
    mode = value.strip().lower()
    if mode not in ("light", "dark"):
        _warn_malformed("mode", value, "light/dark")
        return
    graph.mode = mode


def _dir_logo(value: str, graph: MetroGraph) -> None:
    graph.logo_path = value


def _dir_off_track(value: str, graph: MetroGraph) -> None:
    graph._pending_off_track.extend(_split_csv(value))


def _dir_process(value: str, graph: MetroGraph) -> None:
    """Parse ``%%metro process: station_id | regex``.

    Maps a station to the Nextflow process name(s) it represents, for the
    live-progress server and the check-mapping linter. The whole field after
    ``|`` is one regular expression (no comma splitting, so quantifiers like
    ``{1,3}`` are safe); repeat the directive to attach several patterns to one
    station. The regex is matched against the fully-qualified process name, so
    ``FASTQC`` matches ``NFCORE_RNASEQ:RNASEQ:FASTQC``.
    """
    station_id, sep, pattern = value.partition("|")
    station_id, pattern = station_id.strip(), pattern.strip()
    if not sep or not station_id or not pattern:
        _warn_malformed("process", value, "'station_id | regex'")
        return
    try:
        re.compile(pattern)
    except re.error as exc:
        _warn_directive("process", f"invalid regex {pattern!r}: {exc}")
        return
    graph._pending_process.append((station_id, pattern))


def _dir_line(value: str, graph: MetroGraph) -> None:
    parts = _split_fields(value)
    if len(parts) < 3:
        _warn_malformed("line", value, "'id | name | #color' [| style]")
        return
    style = "solid"
    if len(parts) >= 4 and parts[3]:
        raw_style = parts[3].lower()
        if raw_style in VALID_LINE_STYLES:
            style = raw_style
        else:
            _warn_malformed("line style", parts[3], "/".join(VALID_LINE_STYLES))
    graph.add_line(
        MetroLine(
            id=parts[0],
            display_name=parts[1],
            color=parts[2],
            style=style,
        )
    )


def _parse_icon_directive(icon_type: str, value: str, graph: MetroGraph) -> None:
    """Parse %%metro file:/files:/dir: station_id | labels [| name [| options]].

    Records a pending terminus designation; each comma-separated label becomes
    one icon, optionally captioned by the third field and bannered when the
    fourth field's options include ``banner``.
    """
    parts = _split_fields(value)
    if len(parts) < 2:
        _warn_malformed(icon_type, value, "'station_id | labels [| name [| options]]'")
        return
    station_id = parts[0]
    labels = _split_csv(parts[1])
    name = parts[2] if len(parts) >= 3 else ""
    options = {o.lower() for o in _split_csv(parts[3])} if len(parts) >= 4 else set()
    banner = "banner" in options
    graph._pending_terminus.setdefault(station_id, []).extend(
        (label, icon_type, name, banner) for label in labels
    )


def _parse_port_hint(
    value: str,
    graph: MetroGraph,
    section_id: str,
    is_entry: bool,
) -> None:
    """Parse %%metro entry:/exit: and store as a hint on the Section.

    Does NOT create Port objects - those are created later in _resolve_sections
    based on actual inter-section edges.
    """
    key = "entry" if is_entry else "exit"
    parts = _split_fields(value)
    if len(parts) < 2:
        _warn_malformed(key, value, "'side | line1, line2'")
        return

    side_str = parts[0].lower()
    side_map = {
        "left": PortSide.LEFT,
        "right": PortSide.RIGHT,
        "top": PortSide.TOP,
        "bottom": PortSide.BOTTOM,
    }
    side = side_map.get(side_str)
    if side is None:
        _warn_malformed(key, side_str, "a side (left/right/top/bottom)")
        return

    line_ids = _split_csv(parts[1])

    section = graph.sections.get(section_id)
    if section:
        if is_entry:
            section.entry_hints.append((side, line_ids))
            graph._explicit_entry.add(section_id)
        else:
            section.exit_hints.append((side, line_ids))
            graph._explicit_exit.add(section_id)


def _parse_grid_directive(value: str, graph: MetroGraph) -> None:
    """Parse %%metro grid: section_id | col,row[,rowspan[,colspan]] directive."""
    parts = _split_fields(value)
    coords = parts[1].split(",") if len(parts) >= 2 else []
    if len(parts) < 2 or len(coords) < 2:
        _warn_malformed("grid", value, "'section_id | col,row[,rowspan[,colspan]]'")
        return

    section_id = parts[0]
    try:
        col = int(coords[0].strip())
        row = int(coords[1].strip())
        rowspan = int(coords[2].strip()) if len(coords) >= 3 else 1
        colspan = int(coords[3].strip()) if len(coords) >= 4 else 1
    except ValueError:
        _warn_malformed("grid", value, "integer col,row[,rowspan[,colspan]]")
        return
    graph.grid_overrides[section_id] = (col, row, rowspan, colspan)
    graph._explicit_grid.add(section_id)


_LEGEND_KEYWORDS = ("bl", "br", "tl", "tr", "bottom", "right", "none")


def _parse_xy(text: str) -> tuple[float, float] | None:
    """Parse a ``x,y`` pair into floats, or None if it is not a number pair."""
    if "," not in text:
        return None
    x_str, _, y_str = text.partition(",")
    try:
        return (float(x_str.strip()), float(y_str.strip()))
    except ValueError:
        return None


def apply_legend_directive(value: str, graph: MetroGraph) -> None:
    """Parse the %%metro legend: directive positioning the legend+logo block.

    Grammar (the keyword forms stay content-anchored with the content-overlap
    fallback; the qualifier and absolute forms pin the block exactly):

        legend: <keyword>              keyword (bl/br/tl/tr/bottom/right/none)
        legend: <keyword> | canvas     anchor the keyword to the canvas frame
        legend: <keyword> | <dx>,<dy>  nudge the keyword anchor by an offset
        legend: <x>,<y>                absolute top-left coordinates
    """
    # Reset modifiers so a re-declared directive starts clean.
    graph.legend_anchor = "content"
    graph.legend_offset = None
    graph.legend_at = None

    head, sep, qualifier = value.partition("|")
    head = head.strip().lower()
    qualifier = qualifier.strip().lower()

    coords = _parse_xy(head)
    if coords is not None:
        graph.legend_at = coords
        graph.legend_position = "free"
        if sep:
            _warn_directive(
                "legend",
                f"qualifier {qualifier!r} ignored with absolute coordinates",
            )
        return

    if head not in _LEGEND_KEYWORDS:
        _warn_directive(
            "legend",
            f"unknown position {head!r}; expected one of "
            f"{'/'.join(_LEGEND_KEYWORDS)} or 'x,y'. Ignoring",
        )
        return
    graph.legend_position = head

    if not qualifier:
        return
    if qualifier == "canvas":
        graph.legend_anchor = "canvas"
        return
    offset = _parse_xy(qualifier)
    if offset is not None:
        graph.legend_offset = offset
    else:
        _warn_directive(
            "legend",
            f"unknown qualifier {qualifier!r}; expected 'canvas' or 'dx,dy'. Ignoring",
        )


def _parse_group_directive(value: str, graph: MetroGraph) -> None:
    """Parse %%metro group: Label | station1, station2[, ...] [| above|below].

    Stores an annotative caption spanning the listed stations.  The optional
    third field selects whether the caption renders ``below`` (default) or
    ``above`` the spanned stations.  Purely decorative; does not affect layout.
    """
    parts = _split_fields(value)
    if len(parts) < 2:
        _warn_directive(
            "group",
            f"{value!r} needs 'Label | station1, station2'. Ignoring",
        )
        return
    label = _unquote(parts[0])
    station_ids = _split_csv(parts[1])
    if not label or not station_ids:
        _warn_directive(
            "group",
            f"{value!r} needs a label and at least one station. Ignoring",
        )
        return
    position: Literal["above", "below"] = "below"
    if len(parts) >= 3:
        raw_pos = parts[2].lower()
        if raw_pos == "above":
            position = "above"
        elif raw_pos == "below":
            position = "below"
        elif raw_pos:
            _warn_directive(
                "group",
                f"position {raw_pos!r} not recognised; expected "
                "'above' or 'below'. Using 'below'",
            )
    graph.groups.append(
        StationGroup(label=label, station_ids=station_ids, position=position)
    )


def _parse_interchange_directive(value: str, graph: MetroGraph) -> None:
    """Parse %%metro interchange: node_id | rail-1 lines | rail-2 lines [| ...].

    Declares ``node_id`` a cross-track interchange: each pipe-separated group is
    one rail of the interchange, carrying the comma-separated lines listed in
    it.  The node is expanded into one sub-station per rail in
    :func:`resolve._expand_interchanges`, so the lines pass through on their own
    tracks and the step renders as a single connector glyph.  Needs at least two
    rails; a malformed directive is warned about and ignored.
    """
    parts = _split_fields(value)
    if len(parts) < 3:
        _warn_directive(
            "interchange",
            f"{value!r} needs 'node_id | rail-1 lines | rail-2 lines'. Ignoring",
        )
        return
    node_id = parts[0].strip()
    rails = [_split_csv(p) for p in parts[1:]]
    if not node_id or any(not rail for rail in rails):
        _warn_directive(
            "interchange",
            f"{value!r} needs a node id and at least one line per rail. Ignoring",
        )
        return
    graph.interchanges.append(Interchange(node_id=node_id, rails=rails))


def _parse_legend_combo_directive(value: str, graph: MetroGraph) -> None:
    """Parse %%metro legend_combo: lineA, lineB[, ...] | Display Label.

    Stores a (line_ids, label) entry on ``graph.legend_combos``. The named
    lines are rendered as a single combined legend row and (in rail mode)
    share a single rail slot. A combo referencing unknown lines is warned
    about and ignored; unknown members of an otherwise-valid combo are
    dropped (with a warning) and the remaining members kept.
    """
    parts = value.split("|", 1)
    if len(parts) != 2:
        _warn_directive(
            "legend_combo",
            f"invalid {value!r}; expected 'lineA, lineB | Display Label'",
        )
        return
    ids_raw, label = parts[0], parts[1].strip()
    line_ids = _split_csv(ids_raw)
    if len(line_ids) < 2 or not label:
        _warn_directive(
            "legend_combo",
            f"invalid {value!r}; expected at least two line IDs and a non-empty label",
        )
        return
    known = [lid for lid in line_ids if lid in graph.lines]
    unknown = [lid for lid in line_ids if lid not in graph.lines]
    if unknown:
        _warn_directive(
            "legend_combo",
            f"{label!r} references unknown line(s) "
            f"{', '.join(unknown)}; ignoring those",
        )
    if len(known) < 2:
        _warn_directive(
            "legend_combo",
            f"{label!r} has fewer than two known lines; ignoring",
        )
        return
    graph.legend_combos.append((tuple(known), label))


def _parse_marker_style(key: str, spec: str) -> MarkerStyle | None:
    """Parse a ``shape, fill`` marker spec into a MarkerStyle.

    ``shape`` defaults to ``circle`` and ``fill`` to ``solid`` when omitted.
    An unknown shape is rejected (returns None with a warning); any fill
    string other than ``open``/``solid`` is taken as a literal colour.
    """
    parts = [p.strip() for p in spec.split(",")]
    shape = parts[0].lower() if parts and parts[0] else MARKER_SHAPE_CIRCLE
    fill = parts[1] if len(parts) >= 2 and parts[1] else MARKER_FILL_SOLID
    if shape not in VALID_MARKER_SHAPES:
        _warn_directive(
            key,
            f"unknown shape {shape!r}; expected one of "
            f"{'/'.join(VALID_MARKER_SHAPES)}. Ignoring",
        )
        return None
    # Normalise the fill keywords to lowercase; leave literal colours as-is.
    if fill.lower() in (MARKER_FILL_OPEN, MARKER_FILL_SOLID):
        fill = fill.lower()
    return MarkerStyle(shape=shape, fill=fill)


def _parse_marker_directive(value: str, graph: MetroGraph) -> None:
    """Parse ``%%metro marker: node_id | shape, fill``."""
    node_part, sep, style_part = value.partition("|")
    node_id = node_part.strip()
    if not node_id:
        return
    style = _parse_marker_style("marker", style_part.strip() if sep else "")
    if style is not None:
        graph._pending_markers[node_id] = style


def _parse_marker_legend_directive(value: str, graph: MetroGraph) -> None:
    """Parse ``%%metro marker_legend: shape, fill | Caption``."""
    style_part, sep, caption = value.partition("|")
    if not sep:
        _warn_directive(
            "marker_legend",
            "needs a caption: 'shape, fill | Caption'. Ignoring",
        )
        return
    style = _parse_marker_style("marker_legend", style_part.strip())
    caption = caption.strip()
    if style is not None and caption:
        graph.marker_legend.append(MarkerLegendEntry(style=style, caption=caption))


def _parse_line_spread_directive(value: str, graph: MetroGraph) -> None:
    """Parse %%metro line_spread: <mode>[ | section[, section...]].

    Bare ``line_spread: <mode>`` sets the graph-wide default; the piped form
    ``line_spread: <mode> | sectionA, sectionB`` records a per-section override
    that wins over the default for those sections. ``<mode>`` is one of
    ``bundle`` / ``centered`` / ``rails``; an unrecognised mode is warned about
    and ignored.
    """
    mode_raw, _, sids_raw = value.partition("|")
    try:
        mode = LineSpread(mode_raw.strip().lower())
    except ValueError:
        valid = ", ".join(m.value for m in LineSpread)
        _warn_directive(
            "line_spread",
            f"invalid mode {mode_raw.strip()!r}; expected one of {valid}",
        )
        return
    section_ids = _split_csv(sids_raw)
    if section_ids:
        for sid in section_ids:
            graph.line_spread_overrides[sid] = mode
    else:
        graph.line_spread = mode


def _make_layout_option_handler(
    opt: LayoutOption,
) -> Callable[[str, MetroGraph], None]:
    """Build the directive handler for a registry option.

    The handler parses the payload via :func:`coerce`, writing the option's
    graph field on success or warning (and leaving the default) on a malformed
    value.  This is the directive half of the single-definition cascade in
    :mod:`nf_metro.options`; the CLI half lives in :mod:`nf_metro.cli`.
    """

    def handler(value: str, graph: MetroGraph) -> None:
        result, expected = coerce(opt, value)
        if result is INVALID:
            _warn_malformed(opt.name, value, expected)
        else:
            setattr(graph, opt.target_attr, result)

    return handler


# Graph-wide directives, keyed by exact name. The simple scalar/bool/enum
# knobs are generated from the LAYOUT_OPTIONS registry (shared with the CLI);
# the bespoke handlers below carry grammar the generic registry can't express.
# Section-scoped (entry/exit/direction) and icon (file/files/dir) keys are
# dispatched separately in _apply_directive / _apply_scoped_directive.
_GLOBAL_DIRECTIVE_HANDLERS: dict[str, Callable[[str, MetroGraph], None]] = {
    opt.name: _make_layout_option_handler(opt) for opt in LAYOUT_OPTIONS
}
_GLOBAL_DIRECTIVE_HANDLERS.update(
    {
        "title": _dir_title,
        "style": _dir_style,
        "mode": _dir_mode,
        "logo": _dir_logo,
        "line": _dir_line,
        "off_track": _dir_off_track,
        "process": _dir_process,
        "grid": _parse_grid_directive,
        "line_spread": _parse_line_spread_directive,
        "legend_combo": _parse_legend_combo_directive,
        "legend": apply_legend_directive,
        "group": _parse_group_directive,
        "interchange": _parse_interchange_directive,
        "marker_legend": _parse_marker_legend_directive,
        "marker": _parse_marker_directive,
    }
)

# Directives that act on the enclosing subgraph rather than the whole graph.
_SCOPED_DIRECTIVES = ("entry", "exit", "direction")


def _apply_scoped_directive(
    key: str, value: str, graph: MetroGraph, section_id: str | None
) -> None:
    """Apply a section-scoped directive (entry/exit/direction).

    These only mean anything inside a subgraph; outside one they are warned
    about and ignored.
    """
    if section_id is None or section_id not in graph.sections:
        _warn_directive(key, "must appear inside a subgraph; ignoring")
    elif key == "direction":
        direction = value.upper()
        if direction in ("LR", "RL", "TB", "BT"):
            graph.sections[section_id].direction = direction
            graph._explicit_directions.add(section_id)
        else:
            _warn_malformed("direction", value, "LR/RL/TB/BT")
    else:
        _parse_port_hint(value, graph, section_id, is_entry=key == "entry")


def _apply_directive(
    key: str,
    value: str,
    graph: MetroGraph,
    current_section_id: str | None,
) -> None:
    """Apply one ``%%metro`` directive by its exact key.

    Routes to the section-scoped handler, the file/files/dir icon directive, or
    the graph-wide registry. An unknown key is warned about and ignored.
    """
    if key in _SCOPED_DIRECTIVES:
        _apply_scoped_directive(key, value, graph, current_section_id)
    elif key in VALID_ICON_TYPES:
        _parse_icon_directive(key, value, graph)
    elif handler := _GLOBAL_DIRECTIVE_HANDLERS.get(key):
        handler(value, graph)
    else:
        _warn_directive(key, "unknown directive; ignoring")
