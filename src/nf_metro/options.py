"""Single source of truth for layout/render options exposed in both planes.

Every tunable that can be set both by a ``%%metro`` directive and by a CLI
flag is declared once here as a :class:`LayoutOption`. The directive
dispatcher (:mod:`nf_metro.parser.directives`) and the CLI
(:mod:`nf_metro.cli`) each build their surface from this registry, so adding
an option - its name, type, constraints, and help - wires up the directive
handler and the CLI flag together rather than in two hand-kept places.

Each option targets a field on :class:`~nf_metro.parser.model.MetroGraph` of
the same name (or :attr:`LayoutOption.attr`): the directive writes that field
during parsing, an explicitly-set CLI flag overwrites it afterwards, and the
layout engine and renderer read it. Precedence is uniform: CLI flag (when set)
> ``%%metro`` directive > built-in default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Returned by :func:`coerce` in place of a value when the payload is unusable.
INVALID = object()

OptionKind = Literal["float", "int", "bool", "choice", "str"]
NumberSign = Literal["any", "nonneg", "positive"]


@dataclass(frozen=True)
class LayoutOption:
    """One tunable settable via ``%%metro`` and the equivalent CLI flag.

    ``kind`` selects parsing and the CLI type: ``float``/``int`` numbers
    (constrained by ``sign``), a ``bool`` flag, a ``choice`` from ``choices``,
    or a free ``str``.
    """

    name: str
    kind: OptionKind
    help: str
    attr: str = ""
    choices: tuple[str, ...] = ()
    sign: NumberSign = "any"
    parse_time: bool = False  # consumed during parsing, not after

    @property
    def target_attr(self) -> str:
        """Graph attribute this option writes (defaults to ``name``)."""
        return self.attr or self.name

    @property
    def cli_flag(self) -> str:
        """Long CLI flag for this option (kebab-cased ``name``)."""
        return "--" + self.name.replace("_", "-")


def coerce(opt: LayoutOption, raw: str) -> tuple[Any, str]:
    """Parse a directive payload for *opt*.

    Returns ``(value, "")`` on success or ``(INVALID, expected)`` on failure,
    where *expected* describes the valid input for a warning message.
    """
    text = raw.strip()
    if opt.kind == "bool":
        token = text.lower()
        if token in ("true", "yes", "1"):
            return True, ""
        if token in ("false", "no", "0", ""):
            return False, ""
        return INVALID, "a boolean (true/false)"
    if opt.kind == "choice":
        token = text.lower()
        if token in opt.choices:
            return token, ""
        return INVALID, " or ".join(repr(c) for c in opt.choices)
    if opt.kind in ("int", "float"):
        caster = int if opt.kind == "int" else float
        try:
            num = caster(text)
        except ValueError:
            return INVALID, "a number"
        if opt.sign == "nonneg" and num < 0:
            return INVALID, "a non-negative number"
        if opt.sign == "positive" and num <= 0:
            return INVALID, "a positive number"
        return num, ""
    return text, ""


# Declaration order is the order CLI flags appear in --help.
LAYOUT_OPTIONS: tuple[LayoutOption, ...] = (
    LayoutOption(
        name="x_spacing",
        kind="float",
        sign="positive",
        help="Horizontal spacing between layers (default: auto - widened from "
        "60 only when wide labels would otherwise collide).",
    ),
    LayoutOption(
        name="y_spacing",
        kind="float",
        sign="positive",
        help="Vertical spacing between tracks (default: auto - derived from the "
        "map's content so captioned icons and dense labels don't collide).",
    ),
    LayoutOption(
        name="section_x_gap",
        kind="float",
        sign="nonneg",
        help="Horizontal gap between sections (default: 50).",
    ),
    LayoutOption(
        name="section_y_gap",
        kind="float",
        sign="nonneg",
        help="Vertical gap between sections (default: 50).",
    ),
    LayoutOption(
        name="fold_threshold",
        kind="int",
        sign="positive",
        parse_time=True,
        help="Max station-columns a section row may reach before the "
        "auto-layout wraps it onto the next row (default 15). Raise it to keep "
        "a long horizontal trunk of sections on a single row.",
    ),
    LayoutOption(
        name="diamond_style",
        kind="choice",
        choices=("straight", "symmetric"),
        help="Fork-join (diamond) layout: 'straight' keeps the top branch on "
        "the main track (default); 'symmetric' fans the branches evenly.",
    ),
    LayoutOption(
        name="line_order",
        kind="choice",
        choices=("definition", "span"),
        help="Line ordering for track assignment: 'definition' (default) "
        "preserves .mmd order, 'span' gives longest-spanning lines inner tracks.",
    ),
    LayoutOption(
        name="center_ports",
        kind="bool",
        help="Centre inter-section ports on the shorter of the two connected "
        "sections, so lines enter/exit at the visual midpoint.",
    ),
    LayoutOption(
        name="compact_offsets",
        kind="bool",
        help="Size each station only for the lines actually passing through it, "
        "rather than reserving a slot for every declared line.",
    ),
    LayoutOption(
        name="label_angle",
        kind="float",
        sign="any",
        help="Angle in degrees for station labels (0 = horizontal). Overrides "
        "the theme default; useful for dense trunks where horizontal labels "
        "collide.",
    ),
    LayoutOption(
        name="font_scale",
        kind="float",
        sign="positive",
        help="Scale every text size and the label-width metrics that drive "
        "layout spacing (1.0 = default).",
    ),
    LayoutOption(
        name="logo_scale",
        kind="float",
        sign="positive",
        help="Scale the logo within the legend block (1.0 = default auto-size).",
    ),
    LayoutOption(
        name="legend_min_height",
        kind="float",
        sign="nonneg",
        help="Minimum legend content height in pixels (useful for single-line "
        "maps where the logo would otherwise be tiny).",
    ),
    LayoutOption(
        name="legend_logo_gap",
        kind="float",
        sign="nonneg",
        help="Horizontal gap in pixels between the logo and the legend entries.",
    ),
    LayoutOption(
        name="width",
        kind="int",
        sign="positive",
        help="Output width in pixels (default: auto from content).",
    ),
    LayoutOption(
        name="height",
        kind="int",
        sign="positive",
        help="Output height in pixels (default: auto from content).",
    ),
    LayoutOption(
        name="animate",
        kind="bool",
        help="Add animated balls traveling along the metro lines.",
    ),
    LayoutOption(
        name="manifest",
        attr="embed_manifest",
        kind="bool",
        help="Embed the machine-readable data manifest (the <metadata> block "
        "and per-station data-metro-* attributes) in the SVG. On by default; "
        "--no-manifest emits the drawn map only.",
    ),
)
